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
  github-require-human-review
                            Retired: the broker keeps the ruleset itself
                            (reconcile_ruleset). RETIRED.

Every call uses an installation token for one repo with only the permissions
that step needs (listing the installed repos: metadata on every repo), asked of the minter sidecar (minter.py) by verb and step and
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
# Verbs no longer run, kept so the requests already made still show; admission
# rejects new ones. github-require-human-review: drop it from here and the CRD
# once retention has deleted the last one (by 2027-02-21).
RETIRED = ("github-require-human-review",)
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
# The commit status sync() keeps on every open PR head of every repo the ops App
# is installed on, made a required check by RULESET, pinned to the ops App: only
# this broker sets it.
HUMAN_REVIEW = "human-review"
# GitHub Apps the human-review ruleset doesn't hold (CI that pushes to default
# branches itself), by name for the reader: their App ids.
RULESET_BYPASS_APPS = {"martin-nef-ci": 5855882}


def ruleset(app_id):
    """The one human-review ruleset, the same on every repo: the check is
    required on the default branch, from the ops App only, so no other App or
    user can set it. The broker keeps it so on every covered repo
    (reconcile_ruleset). Repo admins (you, martin-nef, as the owner) and
    RULESET_BYPASS_APPS bypass it. Rulesets on a personal account's repos can't
    name a user, so you're in by your admin role. Beside the branch rule, not
    in it: it adds a check without rewriting it."""
    return {"name": HUMAN_REVIEW, "target": "branch", "enforcement": "active",
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
            "rules": [{"type": "required_status_checks", "parameters": {
                "strict_required_status_checks_policy": False,
                "required_status_checks": [{"context": HUMAN_REVIEW, "integration_id": int(app_id)}]}}],
            "bypass_actors": [{"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}]
            + [{"actor_id": i, "actor_type": "Integration", "bypass_mode": "always"}
               for i in RULESET_BYPASS_APPS.values()]}
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
        """A Token for one repo, with what this verb's step ("check" or "run") needs.
        repo None: every repo the App is installed on (the sync's step only)."""
        return Token(self.call("token", repo=repo, verb=verb, step=step)["token"], self.owner, repo)


class Token:
    """Revoked when the with-block ends, so no write token outlives its step."""

    def __init__(self, token, owner, repo):
        # No repo: a token for every repo, called with whole paths.
        self.token, self.repo = token, f"/repos/{q(owner)}/{q(repo)}" if repo else ""

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


def matches(want, got):
    """Whether got says everything want does: dicts by want's keys (GitHub
    adds its own, like id and _links), lists as the same items in any order."""
    if isinstance(want, dict):
        return isinstance(got, dict) and all(matches(v, got.get(k)) for k, v in want.items())
    if isinstance(want, list):
        return (isinstance(got, list) and len(got) == len(want)
                and all(any(matches(w, x) for x in got) for w in want))
    return want == got


def reconcile_ruleset(tok, app_id, want=True):
    """Make the repo's human-review rulesets what they should be: the one
    standard ruleset (want), or none (an excluded repo, whose PRs get no
    status, so a required check would hold them all). Adds it where it's
    missing, puts it back where it was edited or disabled, and deletes any
    second one of that name. Returns what it changed, or None."""
    have = [r for r in pages(tok, "/rulesets?includes_parents=false") if r.get("name") == HUMAN_REVIEW]
    done = []
    for r in have[1 if want else 0:]:
        tok("DELETE", f"/rulesets/{int(r['id'])}")
        done.append(f"deleted ruleset {r['id']}")
    if want:
        rs = ruleset(app_id)
        if not have:
            made = tok("POST", "/rulesets", rs)
            done.append(f"added ruleset {made.get('id')}")
        elif not matches(rs, tok("GET", f"/rulesets/{int(have[0]['id'])}")):
            tok("PUT", f"/rulesets/{int(have[0]['id'])}", rs)
            done.append(f"restored ruleset {have[0]['id']}")
    return "; ".join(done) or None


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
        done = ACTS[spec["verb"]](tok, g)
    if spec["verb"] == "github-human-review":
        done += approve_pr(minter, g)
    return done


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


def approve_pr(minter, g):
    """After a sign-off, approve the PR on GitHub as the ops App, so the one
    approval GitHub requires is there too: a PR the reviewer flagged gets a
    comment from it, not an Approve, and its author can't approve its own.
    A separate token and step: the sign-off (the status) stands even if this
    fails, e.g. before the App has pull_requests: write. Returns a note."""
    try:
        with minter.token(g["repo"], "github-human-review", "approve") as tok:
            tok("POST", f"/pulls/{int(g['pr'])}/reviews",
                {"commit_id": g["headSHA"], "event": "APPROVE",
                 "body": "Human review signed off on the ops page, for exactly this commit."})
    except GitHubError as e:
        return f"; not approved on GitHub: {e}"
    return "; approved on GitHub"


def status_body(state, description, url=None):
    body = {"state": state, "context": HUMAN_REVIEW, "description": description[:140]}
    if url:
        body["target_url"] = url  # "Details" on the PR's check: the request's ops page
    return body


ACTS = {
    "github-human-review": act_human_review,
    "github-commit-workflows": act_commit,
    "github-protect-branch": act_protect,
    "github-rerun-workflow": act_rerun,
    "github-dispatch-workflow": act_dispatch,
}


# --- human-review statuses ---------------------------------------------------------

def reviewer_flag(reviews, reviewer, head):
    """(reason, ready) if the PR reviewer's latest review is of head and asks
    for a human; else None. ready is False while the author still has required
    changes to make: the PR is held, but nobody is asked yet. Only reviews the
    API says are the reviewer's count, so nobody else can forge one."""
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
    if not isinstance(reason, str) or not reason.strip():
        return None
    # A reviewer from before `ready` asks straight away.
    return reason.strip()[:500], state.get("ready", True) is not False


YAML_FILE = re.compile(r"\.ya?ml$", re.I)
# A manifest's kind, as a document or a list item declares it, and as a
# kustomize patch's target names it ("target: {kind: Role}" on one line too).
KIND = re.compile(r"""(?:^\s*(?:-\s*)?|[{,]\s*)kind:\s*['"]?([A-Za-z0-9]+)""", re.M)
# More YAML files than this in one PR aren't read one by one: the PR is held.
MAX_YAML_FILES = 50


def load_triggers(path):
    """The code-trigger list (code-triggers.json) as {"trusted": set of logins,
    "triggers": [(repos or None, compiled path regex, what)], "kinds":
    [(repos or None, set of kinds, what)]}. Raises on anything unreadable: the
    caller fails closed."""
    with open(path) as f:
        data = json.load(f)
    trusted = {str(t).lower() for t in data["trusted"]}
    rules, kinds = [], []
    for rule in data["triggers"]:
        repos = rule.get("repos")
        if repos is not None and not (isinstance(repos, list) and all(isinstance(r, str) for r in repos)):
            raise ValueError(f"{rule.get('what')}: repos must be a list of names")
        paths, rule_kinds = rule.get("paths", []), rule.get("kinds", [])
        if not (paths or rule_kinds) or not str(rule["what"]).strip():
            raise ValueError("every trigger needs what, and paths or kinds")
        if not all(isinstance(k, str) and re.fullmatch(r"[A-Za-z0-9]+", k) for k in rule_kinds):
            raise ValueError(f"{rule['what']}: kinds must be plain kind names")
        scope = set(repos) if repos is not None else None
        rules += [(scope, re.compile(pattern), str(rule["what"])) for pattern in paths]
        if rule_kinds:
            kinds.append((scope, set(rule_kinds), str(rule["what"])))
    return {"trusted": trusted, "triggers": rules, "kinds": kinds}


def kinds_in(tok, path, ref):
    """The kinds a YAML file declares at ref; empty if it isn't there. Raises
    GitHubError when the file can't be read whole, so the caller holds the PR."""
    try:
        got = tok("GET", f"/contents/{urllib.parse.quote(path)}?ref={ref}")
    except GitHubError as e:
        if e.code == 404:
            return set()
        raise
    if not isinstance(got, dict) or got.get("encoding") != "base64":
        raise GitHubError(413, f"{path} is too large to read")
    return set(KIND.findall(base64.b64decode(got["content"]).decode("utf-8", "replace")))


def code_trigger(tok, repo, pr, rules, humans):
    """(why the PR needs a human, whether to file a flag for it) from the
    code-trigger list, or None. Your own PRs aren't checked: you wrote them.
    rules None (the list couldn't be read) holds every other PR. A PR or
    commit by someone not on the trusted list is held but not filed: nobody
    automated will merge it, so you approve it on GitHub when you look. A path
    trigger on a trusted PR is filed, so you're asked."""
    author = (pr.get("user") or {}).get("login", "")
    if author.lower() in {h.lower() for h in humans}:
        return None
    if rules is None:
        return "the code-trigger list can't be read, so every PR waits for a human", False
    number = pr["number"]
    untrusted = set()
    if author.lower() not in rules["trusted"]:
        untrusted.add(author or "an unknown author")
    for c in pages(tok, f"/pulls/{number}/commits"):
        login = (c.get("author") or {}).get("login")
        if not login or login.lower() not in rules["trusted"]:
            untrusted.add(login or "an author with no GitHub account")
    if untrusted:
        return f"by {', '.join(sorted(untrusted))}, not on the trusted list: approve it on GitHub", False
    hits = {}
    files = pages(tok, f"/pulls/{number}/files")
    for f in files:
        for path in filter(None, (f.get("filename"), f.get("previous_filename"))):
            for repos, pattern, what in rules["triggers"]:
                if (repos is None or repo in repos) and pattern.search(path):
                    hits.setdefault(what, path)
    # What a YAML file declares, before and after: a Role whose rules change,
    # or a Secret whatever the file is called. Only when a kind rule applies here.
    kind_rules = [(kinds, what) for repos, kinds, what in rules.get("kinds", []) if repos is None or repo in repos]
    yaml = [f for f in files if YAML_FILE.search(f.get("filename", "")) or YAML_FILE.search(f.get("previous_filename") or "")]
    if kind_rules and yaml:
        if len(yaml) > MAX_YAML_FILES:
            return f"touches {len(yaml)} YAML files, too many to read for their kinds", True
        head, base = pr["head"]["sha"], (pr.get("base") or {}).get("sha")
        for f in yaml:
            found = set()
            if f.get("status") != "removed":
                found |= kinds_in(tok, f["filename"], head)
            if base and f.get("status") != "added":
                found |= kinds_in(tok, f.get("previous_filename") or f["filename"], base)
            for kinds, what in kind_rules:
                matched = sorted(found & kinds)
                if matched:
                    hits.setdefault(what, f"{f['filename']}: {', '.join(matched)}")
    if hits:
        return "touches " + "; ".join(f"{w} ({p})" for w, p in hits.items()), True
    return None


def human_approval(reviews, humans, head):
    """The login of a human in humans whose latest decisive review (Approve or
    Request changes, not a comment) approves exactly head; else None. That's
    a sign-off made on GitHub instead of the ops page. Only reviews the API
    says are theirs count, and a push makes it stale (another commit_id)."""
    humans = {h.lower() for h in humans}
    latest = {}
    for r in sorted(reviews, key=lambda r: r.get("submitted_at") or ""):
        login = (r.get("user") or {}).get("login", "")
        if login.lower() in humans and r.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[login] = r
    for login, r in sorted(latest.items()):
        if r["state"] == "APPROVED" and r.get("commit_id") == head:
            return login
    return None


def pages(tok, path, key=None):
    """Every item of a list, page by page: a PR past the 100th still gets its
    status, and a review past the 100th still counts. key: the list is under
    this key of each page."""
    out, page = [], 1
    while True:
        items = tok("GET", f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}")
        items = items[key] if key else items
        out += items
        if len(items) < 100:
            return out
        page += 1


def installed(tok):
    """[(name, its open issues and PRs)] of every unarchived repo the App is
    installed on. tok: a metadata token for every repo."""
    return [(r["name"], r.get("open_issues_count", 1))
            for r in pages(tok, "/installation/repositories", "repositories") if not r.get("archived")]


class Lazy:
    """A token minted on its first call, if any, and revoked with the block."""

    def __init__(self, mint):
        self.mint, self.tok = mint, None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self.tok is not None:
            self.tok.__exit__(*exc)

    def __call__(self, *args):
        if self.tok is None:
            self.tok = self.mint()
        return self.tok(*args)


def sync(minter, repo, desired, reviewer, recheck=None, humans=(), link=None, triggers=None):
    """Set the human-review status on every open PR head in repo to what
    desired(pr number, head sha) says, or success where one of humans approved
    the head on GitHub. Returns (the flags that have no request yet: [(pr
    number, head sha, reason, whose: "reviewer" or "code")], the open PR
    numbers, the GitHub approvals: [(pr number, head sha, login)]).
    triggers(tok, pr) is code_trigger's answer for an unflagged head.
    desired returns (state, description, flagged), flagged meaning a request
    covers this head. recheck(pr number, head sha) is asked again, from fresh
    requests, before a success is turned pending: a sign-off may have landed
    since desired's were read. link(pr number, head sha) is the status's
    target_url (the request's ops page), or None. Reads with a read-only token;
    one that can write statuses is minted only if a status has to change."""
    to_file, numbers, approved = [], set(), []
    with minter.token(repo, "github-human-review", "sync") as tok, \
            Lazy(lambda: minter.token(repo, "github-human-review", "write")) as write:
        for pr in pages(tok, "/pulls?state=open"):
            number, head = pr["number"], pr["head"]["sha"]
            numbers.add(number)
            state, description, flagged = desired(number, head)
            reason = None
            # Already signed off, or nothing to look at: no reviews read.
            reviews = pages(tok, f"/pulls/{number}/reviews") if state == "pending" or not flagged else []
            login = human_approval(reviews, humans, head)
            if not flagged:
                flag = reviewer_flag(reviews, reviewer, head)
                trigger = triggers(tok, pr) if triggers else None
                if flag:
                    reason, ready = flag
                    if ready:
                        if not login:  # you've already looked: nothing to file
                            to_file.append((number, head, reason, "reviewer"))
                        state, description = "pending", f"human review required: {reason}"
                    else:
                        # Held, but nobody is pinged until the author has done what's required.
                        state, description = "pending", f"human review after the required changes: {reason}"
                elif trigger:
                    reason, fileable = trigger
                    if fileable and not login:
                        to_file.append((number, head, reason, "code"))
                    state, description = "pending", f"human review required: {reason}"
            if state == "pending" and login:
                # Worded as human_review_state words the request it signs off,
                # so the next pass writes nothing.
                state, description = "success", f"signed off by {login} (GitHub review)"
                if flagged:
                    approved.append((number, head, login))
            current = tok("GET", f"/commits/{head}/status")
            mine = [s for s in current.get("statuses", []) if s.get("context") == HUMAN_REVIEW]
            if mine and mine[0].get("state") == "success" and state == "pending" and not reason and recheck:
                state, description, _ = recheck(number, head)
            url = link(number, head) if link else None
            if not mine or ((mine[0].get("state"), mine[0].get("description"), mine[0].get("target_url") or None)
                            != (state, description[:140], url)):
                write("POST", f"/statuses/{head}", status_body(state, description, url))
    return to_file, numbers, approved


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


def asked(owner, spec):
    """HTML of what a request asked for, from the request alone: for one that
    has been decided, when details() would describe GitHub as it is now."""
    g = spec["github"]
    web = f"https://github.com/{owner}/{g['repo']}"
    verb = spec["verb"]
    if verb == "github-commit-workflows":
        files = "".join(
            f"<p><code>{e(f['path'])}</code>: deleted</p>" if f.get("delete") else
            f"<p><code>{e(f['path'])}</code>:</p><pre>{e(f.get('content', ''))}</pre>" for f in g["files"])
        return (f"<p>A commit on {link(f'{web}/tree/{g['branch']}', g['branch'])}, on top of "
                f"{link(f'{web}/commit/{g['baseSHA']}', g['baseSHA'][:12])}:</p><pre>{e(g['message'])}</pre>{files}")
    if verb == "github-human-review":
        return (f"<p>A sign-off of {link(f'{web}/pull/{int(g['pr'])}', f'{g['repo']}#{int(g['pr'])}')} at "
                f"{link(f'{web}/commit/{g['headSHA']}', g['headSHA'][:12])}.</p>")
    if verb == "github-require-human-review":
        return f"<p>The standard <code>{HUMAN_REVIEW}</code> ruleset on {link(web + '/settings/rules', g['repo'])}.</p>"
    if verb == "github-protect-branch":
        return (f"<p>The standard rule on the default branch of {link(web + '/settings/branches', g['repo'])}:</p>"
                f"<pre>{e(json.dumps(PROTECTION, indent=2))}</pre>")
    if verb == "github-rerun-workflow":
        return f"<p>A re-run of the failed jobs of {link(f'{web}/actions/runs/{int(g['runID'])}', f'run {int(g['runID'])}')}.</p>"
    if verb == "github-dispatch-workflow":
        inputs = "".join(f"<tr><td><code>{e(k)}</code></td><td><code>{e(v)}</code></td></tr>"
                         for k, v in sorted(g.get("inputs", {}).items()))
        return (f"<p>A run of {link(f'{web}/actions/workflows/{g['workflow']}', g['workflow'])} on the default "
                "branch.</p>" + (f"<table>{inputs}</table>" if inputs else "<p>No inputs.</p>"))
    if verb == "github-create-repo":
        return (f"<p>{'A private' if g.get('private', True) else 'A public'} repo {link(web, f'{owner}/{g['repo']}')}"
                + (f", described as <q>{e(g['description'])}</q>" if g.get("description") else "") + ".</p>")
    return f"<p><code>{e(describe(owner, spec))}</code></p>"


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
