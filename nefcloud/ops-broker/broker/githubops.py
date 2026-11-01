"""GitHub verbs for the ops broker: what the agent's own GitHub App may not do.

The agent's App (martin-nef-agent) can push branches and open PRs, but has no
`workflows`, `administration` or `actions: write`, so a pushed branch can't
change CI to run with the repo's secrets, and nothing it does can loosen a
repo's settings. These verbs do those things one approved request at a time,
with a third App (the "ops" App) whose key only the token minter holds:

  github-commit-workflows   Commit the request's files under .github/workflows/
                            onto a non-default, unprotected branch, on top of
                            the exact commit the page diffs against.
  github-protect-branch     Protect an unprotected default branch with the
                            standard rule (PRs only; admins may still merge).
  github-rerun-workflow     Re-run the failed jobs of one finished run.
  github-dispatch-workflow  Run a workflow_dispatch workflow on the default branch.
  github-create-repo        Create a repo under the owner's account (USER_VERBS).
  github-human-review       Sign off a PR that an agent flagged as needing a
                            human: the human-review status on its head turns
                            success. Until then sync() keeps it pending, and a
                            required human-review check keeps the PR unmerged.

Every call uses an installation token for one repo with only the permissions
that step needs, asked of the minter sidecar (minter.py) by verb and step and
revoked as soon as the step is done. Checks and the approval page use
read-only tokens; only an approved run gets write. This process never sees
the App's key.

USER_VERBS are actions GitHub only takes with a user's token. Approving one
sends you through GitHub's OAuth flow for the ops App; the broker relays the
code GitHub returns to the minter, which takes the action with your token and
revokes it. This process never sees that token either.
"""
import base64
import difflib
import html
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request

GITHUB_API = "https://api.github.com"

# The verbs this module runs. Their tokens' permissions are minter.TOKENS.
VERBS = ("github-commit-workflows", "github-protect-branch", "github-rerun-workflow", "github-dispatch-workflow",
         "github-create-repo", "github-human-review")
# Run by the minter with the owner's own token (minter.USER_ACTIONS).
USER_VERBS = ("github-create-repo",)
# The one rule github-protect-branch applies: changes reach the branch only
# through a PR with an approval; enforce_admins off, so you can still merge your
# own and the bot's PRs. The same rule as in claude-remote's docs/design.md.
PROTECTION = {
    "required_status_checks": None,
    "enforce_admins": False,
    "required_pull_request_reviews": {"required_approving_review_count": 1, "dismiss_stale_reviews": True},
    "restrictions": None,
}
WORKFLOW_PATH = re.compile(r"\.github/workflows/[A-Za-z0-9._-]+\.ya?ml")
BRANCH = re.compile(r"[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*")
REPO_NAME = re.compile(r"[A-Za-z0-9_.-]+")
RERUNNABLE = {"failure", "cancelled", "timed_out", "startup_failure"}
# The commit status sync() keeps on every open PR head of HUMAN_REVIEW_REPOS,
# made a required check there, pinned to the ops App: only this broker sets it.
HUMAN_REVIEW = "human-review"
# The marker the PR reviewer ends each review with (pr-reviewer job.py STATE_MARK).
REVIEW_MARK = "<!-- pr-reviewer-state "


class GitHubError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code

    @property
    def transient(self):
        """Worth trying again later; for a write, it may also have taken effect."""
        return self.code in (408, 429) or self.code >= 500


class NothingChanged(Exception):
    """A step failed before anything visible on GitHub changed."""


# --- the API -------------------------------------------------------------------

def api(method, path, token=None, body=None):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "nefcloud-ops-broker",
               "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(GITHUB_API + path, method=method, headers=headers,
                                 data=None if body is None else json.dumps(body).encode())
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = resp.read()
            return json.loads(data) if data else {}
    except urllib.error.HTTPError as e:
        try:
            message = json.load(e).get("message", e.reason)
        except ValueError:
            message = e.reason
        raise GitHubError(e.code, message) from None


class Minter:
    """The token minter sidecar (minter.py), over its Unix socket. It holds the
    App's key; this side only ever gets one-repo, one-step tokens."""

    def __init__(self, path, owner):
        self.path, self.owner = path, owner

    def call(self, op, **args):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(60)  # the minter's own GitHub calls time out after 20s each
                s.connect(self.path)
                s.sendall(json.dumps({"op": op, **args}).encode())
                s.shutdown(socket.SHUT_WR)
                data = b"".join(iter(lambda: s.recv(65536), b""))
            reply = json.loads(data)
        except (OSError, ValueError) as e:
            raise GitHubError(503, f"the token minter didn't answer: {e!r}") from None
        if "error" in reply:
            err = reply["error"]
            if err.get("nothing_changed"):
                raise NothingChanged(f"{err['code']}: {err['message']}")
            raise GitHubError(err["code"], err["message"])
        return reply

    def status(self):
        """{app_id, slug} of the ops App, or None until it's set up."""
        return self.call("status")["app"]

    def oauth(self):
        """{client_id} of the App's OAuth client, or None until it's set up."""
        return self.call("status")["oauth"]

    def oauth_setup(self, client_secret):
        return self.call("oauth-setup", client_secret=client_secret)["oauth"]

    def user_action(self, verb, code, github):
        """Have the minter trade GitHub's OAuth code for your token and take
        the action. NothingChanged if it failed before the action."""
        return self.call("user-action", verb=verb, code=code, github=github)["message"]

    def manifest(self):
        return self.call("manifest")["manifest"]

    def setup(self, code):
        """Hand GitHub's one-time code to the minter, which trades it for the key."""
        return self.call("setup", code=code)["app"]

    def token(self, repo, verb, step):
        """A Token for one repo, with what this verb's step ("check" or "run") needs."""
        return Token(self.call("token", repo=repo, verb=verb, step=step)["token"], self.owner, repo)


class Token:
    """Revoked when the with-block ends, so no write token outlives its step."""

    def __init__(self, token, owner, repo):
        self.token, self.repo = token, f"/repos/{q(owner)}/{q(repo)}"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            api("DELETE", "/installation/token", self.token)
        except (GitHubError, OSError):
            pass  # it expires within the hour anyway

    def __call__(self, method, path="", body=None):
        """A call under this token's repo: tok("GET", "/branches/x")."""
        return api(method, self.repo + path, self.token, body)


def q(s):
    return urllib.parse.quote(str(s), safe="")


# --- the verbs -----------------------------------------------------------------

def describe(owner, spec):
    g = spec.get("github", {})
    where = f"{owner}/{g.get('repo')}"
    verb = spec["verb"]
    if verb == "github-commit-workflows":
        where += f"@{g.get('branch')}"
    elif verb == "github-rerun-workflow":
        where += f" run {g.get('runID')}"
    elif verb == "github-dispatch-workflow":
        where += f" {g.get('workflow')}"
    elif verb == "github-create-repo":
        where += " (private)" if g.get("private", True) else " (public)"
    elif verb == "github-human-review":
        where += f"#{g.get('pr')}"
    return f"{verb} {where}"


def check(minter, spec):
    """Why the request can't be run, or None. Raises GitHubError (transient
    ones mean: try again on the next scan)."""
    g = spec["github"]
    if not REPO_NAME.fullmatch(g.get("repo", "")) or g["repo"] in (".", ".."):
        return f"{g.get('repo')!r} isn't a repo name"
    if spec["verb"] == "github-create-repo":
        if minter.oauth() is None:
            return "creating a repo needs the App's OAuth client, which isn't set up; see /github"
        return None if existing(minter, g) is None else f"{minter.owner}/{g['repo']} already exists"
    with minter.token(g["repo"], spec["verb"], "check") as tok:
        repo = tok("GET")
        return CHECKS[spec["verb"]](tok, repo, g)


def existing(minter, g):
    """The repo the request would create, if it already exists, or None."""
    with minter.token(g["repo"], "github-create-repo", "check") as tok:
        try:
            return tok("GET")
        except GitHubError as e:
            if e.code == 404:
                return None
            raise


def check_commit(tok, repo, g):
    if not BRANCH.fullmatch(g["branch"]) or ".." in g["branch"] or "//" in g["branch"]:
        return f"{g['branch']!r} isn't a branch name this verb takes"
    for f in g["files"]:
        if not WORKFLOW_PATH.fullmatch(f["path"]):
            return f"{f['path']} isn't a workflow file (.github/workflows/<name>.yml)"
        if ("content" in f) == bool(f.get("delete")):
            return f"{f['path']}: give either content or delete: true"
    if g["branch"] == repo["default_branch"]:
        return f"{g['branch']} is the default branch: commit to a branch and open a PR, which you merge"
    branch = tok("GET", f"/branches/{q(g['branch'])}")
    if branch["protected"]:
        return f"{g['branch']} is protected; only unprotected work branches"
    if branch["commit"]["sha"] != g["baseSHA"]:
        return (f"{g['branch']} is at {branch['commit']['sha']}, not baseSHA {g['baseSHA']}; "
                "request again from the branch's head")
    for f in g["files"]:
        if f.get("delete") and file_at(tok, f["path"], g["baseSHA"]) is None:
            return f"{f['path']} doesn't exist at {g['baseSHA'][:12]}, so it can't be deleted"
    return None


def check_protect(tok, repo, g):
    if protection(tok, repo["default_branch"]) is not None:
        return f"{repo['default_branch']} is already protected; change its rule on GitHub"
    return None


def check_rerun(tok, repo, g):
    run = tok("GET", f"/actions/runs/{int(g['runID'])}")
    if run["status"] != "completed" or run["conclusion"] not in RERUNNABLE:
        return f"run {g['runID']} is {run['status']}/{run['conclusion']}; only failed, finished runs are re-run"
    return None


def check_dispatch(tok, repo, g):
    workflow = tok("GET", f"/actions/workflows/{q(g['workflow'])}")
    if workflow["state"] != "active":
        return f"{g['workflow']} is {workflow['state']}"
    return None


def check_human_review(tok, repo, g):
    pr = tok("GET", f"/pulls/{int(g['pr'])}")
    if pr["state"] != "open":
        return f"#{g['pr']} is {pr['state']}"
    if pr["head"]["sha"] != g["headSHA"]:
        # Approving signs off exactly what the page showed; a newer head needs a new flag.
        return f"#{g['pr']} is at {pr['head']['sha'][:12]} now, not headSHA {g['headSHA'][:12]}; flag it again"
    return None


CHECKS = {
    "github-human-review": check_human_review,
    "github-commit-workflows": check_commit,
    "github-protect-branch": check_protect,
    "github-rerun-workflow": check_rerun,
    "github-dispatch-workflow": check_dispatch,
}


def file_at(tok, path, ref):
    """A file's text at ref, or None if it isn't there."""
    try:
        f = tok("GET", f"/contents/{urllib.parse.quote(path)}?ref={q(ref)}")
    except GitHubError as e:
        if e.code == 404:
            return None
        raise
    return base64.b64decode(f["content"]).decode()


def protection(tok, branch):
    """The branch's protection rule, or None if it has none."""
    try:
        return tok("GET", f"/branches/{q(branch)}/protection")
    except GitHubError as e:
        if e.code == 404 and "not protected" in str(e).lower():
            return None
        raise


def act(minter, spec, code=None):
    """Run an approved request; returns a message for the status. Raises
    GitHubError when GitHub refused it (transient: it may have taken effect)
    and NothingChanged when it failed before changing anything. code: GitHub's
    OAuth code, for USER_VERBS."""
    g = spec["github"]
    if spec["verb"] in USER_VERBS:
        if not code:
            raise NothingChanged("needs you to sign in to GitHub; approve it from the page")
        return minter.user_action(spec["verb"], code, g)
    try:
        tok = minter.token(g["repo"], spec["verb"], "run")
    except GitHubError as e:
        raise NothingChanged(f"no token: {e}") from None
    with tok:
        return ACTS[spec["verb"]](tok, g)


def act_commit(tok, g):
    try:
        base = tok("GET", f"/git/commits/{g['baseSHA']}")
        # On top of baseSHA's tree, so the commit's diff against its parent is
        # exactly the diff the page showed.
        entries = [{"path": f["path"], "mode": "100644", "type": "blob",
                    **({"sha": None} if f.get("delete") else {"content": f["content"]})} for f in g["files"]]
        tree = tok("POST", "/git/trees", {"base_tree": base["tree"]["sha"], "tree": entries})
        commit = tok("POST", "/git/commits", {"message": g["message"], "tree": tree["sha"], "parents": [g["baseSHA"]]})
    except (GitHubError, OSError) as e:
        # Trees and commits nothing points at are invisible and garbage-collected.
        raise NothingChanged(f"preparing the commit: {e}") from None
    try:
        # Not forced: if the branch moved since the request, this fails and
        # nothing lands.
        tok("PATCH", f"/git/refs/heads/{urllib.parse.quote(g['branch'])}", {"sha": commit["sha"], "force": False})
    except GitHubError as e:
        if e.code == 422:
            raise NothingChanged(f"{g['branch']} moved since the request; nothing committed ({e})") from None
        raise
    return f"committed {commit['sha']} to {g['branch']}"


def act_protect(tok, g):
    branch = tok("GET")["default_branch"]
    # The rule is replaced wholesale, so never over one set since the check.
    if protection(tok, branch) is not None:
        raise NothingChanged(f"{branch} got a protection rule since the request; left it alone")
    tok("PUT", f"/branches/{q(branch)}/protection", PROTECTION)
    return f"protected {branch}"


def act_rerun(tok, g):
    tok("POST", f"/actions/runs/{int(g['runID'])}/rerun-failed-jobs")
    return f"re-running the failed jobs of run {g['runID']}"


def act_dispatch(tok, g):
    branch = tok("GET")["default_branch"]
    tok("POST", f"/actions/workflows/{q(g['workflow'])}/dispatches", {"ref": branch, "inputs": g.get("inputs", {})})
    return f"dispatched {g['workflow']} on {branch}"


def act_human_review(tok, g):
    tok("POST", f"/statuses/{g['headSHA']}", status_body("success", "signed off by a human on the ops page"))
    return f"signed off #{g['pr']} at {g['headSHA'][:12]}"


def status_body(state, description):
    return {"state": state, "context": HUMAN_REVIEW, "description": description[:140]}


ACTS = {
    "github-human-review": act_human_review,
    "github-commit-workflows": act_commit,
    "github-protect-branch": act_protect,
    "github-rerun-workflow": act_rerun,
    "github-dispatch-workflow": act_dispatch,
}


# --- human-review statuses ---------------------------------------------------------

def reviewer_flag(reviews, reviewer, head):
    """The reason the PR reviewer's latest review asks for a human, if that
    review is of head and asks; else None. Only reviews the API says are the
    reviewer's count, so nobody else can forge one."""
    mine = [r for r in reviews if (r.get("user") or {}).get("login", "").lower() == reviewer.lower()]
    if not mine:
        return None
    last = max(mine, key=lambda r: r.get("submitted_at") or "")
    body = last.get("body") or ""
    start = body.rfind(REVIEW_MARK)
    if last.get("commit_id") != head or start < 0:
        return None
    try:
        state = json.loads(body[start + len(REVIEW_MARK):].split(" -->", 1)[0])
    except ValueError:
        return None
    reason = state.get("needs_human") if isinstance(state, dict) else None
    return reason.strip()[:500] if isinstance(reason, str) and reason.strip() else None


def pages(tok, path):
    """Every item of a list, page by page: a PR past the 100th still gets its
    status, and a review past the 100th still counts."""
    out, page = [], 1
    while True:
        items = tok("GET", f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}")
        out += items
        if len(items) < 100:
            return out
        page += 1


def sync(minter, repo, desired, reviewer, recheck=None):
    """Set the human-review status on every open PR head in repo to what
    desired(pr number, head sha) says. Returns (the reviewer's flags that have
    no request yet: [(pr number, head sha, reason)], the open PR numbers).
    desired returns (state, description, flagged), flagged meaning a request
    covers this head. recheck(pr number, head sha) is asked again, from fresh
    requests, before a success is turned pending: a sign-off may have landed
    since desired's were read."""
    to_file, numbers = [], set()
    with minter.token(repo, "github-human-review", "sync") as tok:
        for pr in pages(tok, "/pulls?state=open"):
            number, head = pr["number"], pr["head"]["sha"]
            numbers.add(number)
            state, description, flagged = desired(number, head)
            reason = None
            if not flagged:
                reason = reviewer_flag(pages(tok, f"/pulls/{number}/reviews"), reviewer, head)
                if reason:
                    to_file.append((number, head, reason))
                    state, description = "pending", f"human review required: {reason}"
            current = tok("GET", f"/commits/{head}/status")
            mine = [s for s in current.get("statuses", []) if s.get("context") == HUMAN_REVIEW]
            if mine and mine[0].get("state") == "success" and state == "pending" and not reason and recheck:
                state, description, _ = recheck(number, head)
            if not mine or (mine[0].get("state"), mine[0].get("description")) != (state, description[:140]):
                tok("POST", f"/statuses/{head}", status_body(state, description))
    return to_file, numbers


# --- the approval page ---------------------------------------------------------

def e(s):
    return html.escape(str(s), quote=True)


def link(url, text):
    return f'<a href="{e(url)}">{e(text)}</a>'


def details(minter, spec):
    """HTML showing exactly what approving would do, read live from GitHub.
    Raises when it can't be shown; the page then offers no Approve."""
    g = spec["github"]
    web = f"https://github.com/{minter.owner}/{g['repo']}"
    if spec["verb"] == "github-create-repo":
        if minter.oauth() is None:
            raise GitHubError(404, "the App's OAuth client isn't set up; see /github")
        if existing(minter, g) is not None:
            raise GitHubError(409, f"{web} already exists")
        return (f"<p>Creates <b>{'a private' if g.get('private', True) else 'a public'}</b> repo "
                f"<code>{e(minter.owner)}/{e(g['repo'])}</code>"
                + (f", described as <q>{e(g['description'])}</q>" if g.get("description") else "")
                + ", with a README as its first commit. Approving sends you to GitHub to sign in as "
                f"<code>{e(minter.owner)}</code>: the repo is made with your token, which is used for this "
                "one call and revoked.</p>")
    with minter.token(g["repo"], spec["verb"], "check") as tok:
        repo = tok("GET")
        if spec["verb"] == "github-commit-workflows":
            return details_commit(tok, web, g)
        if spec["verb"] == "github-human-review":
            return details_human_review(tok, web, g)
        if spec["verb"] == "github-protect-branch":
            return (f"<p>Adds this rule to {link(web + '/settings/branches', repo['default_branch'])}, "
                    "which has none now: changes only through a PR with one approval. You can still "
                    "merge as an admin.</p>"
                    f"<pre>{e(json.dumps(PROTECTION, indent=2))}</pre>")
        if spec["verb"] == "github-rerun-workflow":
            run = tok("GET", f"/actions/runs/{int(g['runID'])}")
            return (f"<p>Re-runs the failed jobs of {link(run['html_url'], run['display_title'])} "
                    f"({e(run['name'])}, {e(run['event'])} on <code>{e(run['head_branch'])}</code> "
                    f"at <code>{e(run['head_sha'][:12])}</code>, {e(run['conclusion'])}). "
                    "The same commit and workflow, with the same secrets as the first run.</p>")
        workflow = tok("GET", f"/actions/workflows/{q(g['workflow'])}")
        branch = repo["default_branch"]
        inputs = "".join(f"<tr><td><code>{e(k)}</code></td><td><code>{e(v)}</code></td></tr>"
                         for k, v in sorted(g.get("inputs", {}).items()))
        return (f"<p>Runs {link(f'{web}/blob/{branch}/{workflow['path']}', workflow['name'])} on "
                f"<code>{e(branch)}</code>, with its secrets.</p>"
                + (f"<table>{inputs}</table>" if inputs else "<p>No inputs.</p>"))


def details_human_review(tok, web, g):
    pr = tok("GET", f"/pulls/{int(g['pr'])}")
    files = tok("GET", f"/pulls/{int(g['pr'])}/files?per_page=100")
    rows = "".join(f"<tr><td><code>{e(f['filename'])}</code></td><td>+{int(f['additions'])} "
                   f"−{int(f['deletions'])}</td></tr>" for f in files)
    return (f"<p>Signs off {link(pr['html_url'], f'#{pr['number']}: ' + pr['title'])} by "
            f"<code>{e(pr['user']['login'])}</code>, at <code>{e(g['headSHA'][:12])}</code>. Its "
            "<code>human-review</code> check turns green, and its author may merge it once the rest passes. "
            "A later push needs a new sign-off.</p>"
            "<p><b>Read the PR on GitHub before approving.</b> Deny keeps it blocked.</p>"
            f"<table>{rows}</table>")


def details_commit(tok, web, g):
    base = g["baseSHA"]
    out = [f"<p>A commit on {link(f'{web}/tree/{g['branch']}', g['branch'])}, on top of "
           f"{link(f'{web}/commit/{base}', base[:12])}:</p><pre>{e(g['message'])}</pre>",
           "<p><b>Workflows on this branch can run as soon as it lands, with this repo's secrets, "
           "before any review.</b> Read the whole diff.</p>"]
    for f in g["files"]:
        old = file_at(tok, f["path"], base)
        new = None if f.get("delete") else f["content"]
        lines = difflib.unified_diff((old or "").splitlines(keepends=True), (new or "").splitlines(keepends=True),
                                     "/dev/null" if old is None else f"a/{f['path']}",
                                     "/dev/null" if new is None else f"b/{f['path']}")
        out.append("<pre class=diff>" + "".join(diff_line(line) for line in lines) + "</pre>")
    return "".join(out)


def diff_line(line):
    if not line.endswith("\n"):
        line += "\n\\ No newline at end of file\n"
    cls = {"+": "add", "-": "del", "@": "hunk"}.get(line[0], "")
    if line.startswith(("+++", "---")):
        cls = ""
    return f"<span class={cls}>{e(line)}</span>" if cls else e(line)
