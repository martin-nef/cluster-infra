"""GitHub verbs for the ops broker: what the agent's own GitHub App may not do.

The agent's App (martin-nef-agent) can push branches and open PRs, but has no
`workflows`, `administration` or `actions: write`, so a pushed branch can't
change CI to run with the repo's secrets, and nothing it does can loosen a
repo's settings. These verbs do those things one approved request at a time,
with a third App (the "ops" App) whose key exists only in the broker:

  github-commit-workflows   Commit the request's files under .github/workflows/
                            onto a non-default, unprotected branch, on top of
                            the exact commit the page diffs against.
  github-protect-branch     Protect an unprotected default branch with the
                            standard rule (PRs only; admins may still merge).
  github-rerun-workflow     Re-run the failed jobs of one finished run.
  github-dispatch-workflow  Run a workflow_dispatch workflow on the default branch.

Every call uses an installation token minted for one repo with only the
permissions that step needs, revoked as soon as the step is done. Checks and
the approval page use read-only tokens; only an approved run gets write.

The App is created from the approval page (manifest(), convert()): GitHub
hands the private key to the broker, which stores it in a Secret in its own
namespace. It never touches git, a PC or the agent's pod.

Stdlib only, like broker.py: the App's JWT is signed with RSA in pure Python,
because the stock python image has no openssl CLI.
"""
import base64
import difflib
import hashlib
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

GITHUB_API = "https://api.github.com"

# What the ops App is created with. Each verb's tokens take a subset.
PERMISSIONS = {
    "actions": "write",
    "administration": "write",
    "contents": "write",
    "metadata": "read",
    "workflows": "write",
}
# verb -> (permissions to check it and show it, permissions to run it)
VERBS = {
    "github-commit-workflows": ({"contents": "read"}, {"contents": "write", "workflows": "write"}),
    "github-protect-branch": ({"administration": "read"}, {"administration": "write"}),
    "github-rerun-workflow": ({"actions": "read"}, {"actions": "write"}),
    "github-dispatch-workflow": ({"actions": "read", "contents": "read"}, {"actions": "write"}),
}
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


# --- RS256 in pure Python ------------------------------------------------------

def _tlv(data, i=0):
    """One DER element at data[i:]: (tag, value, index after it)."""
    tag, length, i = data[i], data[i + 1], i + 2
    if length & 0x80:
        n = length & 0x7F
        length, i = int.from_bytes(data[i:i + n], "big"), i + n
    return tag, data[i:i + length], i + length


def _children(data):
    items, i = [], 0
    while i < len(data):
        tag, value, i = _tlv(data, i)
        items.append((tag, value))
    return items


def rsa_key(pem):
    """(n, d) of a PKCS#1 or PKCS#8 PEM RSA private key."""
    body = "".join(line for line in pem.strip().splitlines() if not line.startswith("-----"))
    _, seq, _ = _tlv(base64.b64decode(body))
    items = _children(seq)
    if items[1][0] == 0x30:  # PKCS#8: version, AlgorithmIdentifier, OCTET STRING { PKCS#1 }
        _, seq, _ = _tlv(items[2][1])
        items = _children(seq)
    # RSAPrivateKey: version, n, e, d, p, q, ...
    return int.from_bytes(items[1][1], "big"), int.from_bytes(items[3][1], "big")


# DigestInfo for SHA-256, which PKCS#1 v1.5 signs.
SHA256_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


def rs256(pem, data):
    """An RSASSA-PKCS1-v1_5 SHA-256 signature, as `openssl dgst -sha256 -sign` makes."""
    n, d = rsa_key(pem)
    k = (n.bit_length() + 7) // 8
    t = SHA256_INFO + hashlib.sha256(data).digest()
    em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return pow(int.from_bytes(em, "big"), d, n).to_bytes(k, "big")


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


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


class App:
    """The ops App, as stored in its Secret, acting on one owner's repos."""

    def __init__(self, app_id, slug, pem, owner):
        self.app_id, self.slug, self.pem, self.owner = app_id, slug, pem, owner

    def jwt(self):
        now = int(time.time())
        header = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        # iat is backdated to allow for clock drift, as GitHub recommends.
        claims = b64url(json.dumps({"iat": now - 60, "exp": now + 540, "iss": str(self.app_id)}).encode())
        signing_input = f"{header}.{claims}"
        return f"{signing_input}.{b64url(rs256(self.pem, signing_input.encode()))}"

    def token(self, repo, permissions):
        """A Token for one repo with only these permissions."""
        jwt = self.jwt()
        try:
            installation = api("GET", f"/users/{q(self.owner)}/installation", jwt)
        except GitHubError as e:
            if e.code == 404:
                raise GitHubError(404, f"{self.slug} isn't installed on {self.owner}: "
                                       f"https://github.com/apps/{self.slug}/installations/new") from None
            raise
        try:
            minted = api("POST", f"/app/installations/{installation['id']}/access_tokens", jwt,
                         {"repositories": [repo], "permissions": permissions})
        except GitHubError as e:
            if e.code == 422:
                raise GitHubError(422, f"{self.slug} can't get {permissions} on {self.owner}/{repo} "
                                       f"(not installed there, or the repo doesn't exist): {e}") from None
            raise
        return Token(minted["token"], self.owner, repo)


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
    return f"{verb} {where}"


def check(app, spec):
    """Why the request can't be run, or None. Raises GitHubError (transient
    ones mean: try again on the next scan)."""
    g = spec["github"]
    if not REPO_NAME.fullmatch(g.get("repo", "")) or g["repo"] in (".", ".."):
        return f"{g.get('repo')!r} isn't a repo name"
    with app.token(g["repo"], VERBS[spec["verb"]][0]) as tok:
        repo = tok("GET")
        return CHECKS[spec["verb"]](tok, repo, g)


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


CHECKS = {
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


def act(app, spec):
    """Run an approved request; returns a message for the status. Raises
    GitHubError when GitHub refused it (transient: it may have taken effect)
    and NothingChanged when it failed before changing anything."""
    g = spec["github"]
    try:
        tok = app.token(g["repo"], VERBS[spec["verb"]][1])
    except (GitHubError, OSError) as e:
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


ACTS = {
    "github-commit-workflows": act_commit,
    "github-protect-branch": act_protect,
    "github-rerun-workflow": act_rerun,
    "github-dispatch-workflow": act_dispatch,
}


# --- the approval page ---------------------------------------------------------

def e(s):
    return html.escape(str(s), quote=True)


def link(url, text):
    return f'<a href="{e(url)}">{e(text)}</a>'


def details(app, spec):
    """HTML showing exactly what approving would do, read live from GitHub.
    Raises when it can't be shown; the page then offers no Approve."""
    g = spec["github"]
    web = f"https://github.com/{app.owner}/{g['repo']}"
    with app.token(g["repo"], VERBS[spec["verb"]][0]) as tok:
        repo = tok("GET")
        if spec["verb"] == "github-commit-workflows":
            return details_commit(tok, web, g)
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


# --- creating the App ----------------------------------------------------------

def manifest(name, public_url):
    """The manifest the setup page posts to GitHub."""
    return {
        "name": name,
        "url": f"{public_url}/github",
        "description": "Break-glass GitHub actions for the nefcloud agent, each approved on ops.nefcloud.top.",
        "public": False,
        "redirect_url": f"{public_url}/github/callback",
        "default_permissions": PERMISSIONS,
        "default_events": [],
    }


def convert(code, owner):
    """Trade the manifest flow's one-time code for the new App's credentials
    and check GitHub made what was asked for. Returns {app-id, slug, private-key.pem}."""
    app = api("POST", f"/app-manifests/{q(code)}/conversions", body={})
    problems = []
    if app["owner"]["login"].lower() != owner.lower():
        problems.append(f"it belongs to {app['owner']['login']}, not {owner}")
    if app["permissions"] != PERMISSIONS:
        problems.append(f"its permissions are {app['permissions']}, not {PERMISSIONS}")
    if problems:
        raise ValueError(f"GitHub created {app['slug']}, but {'; '.join(problems)}. Delete it at "
                         f"{app['html_url']} and start again; nothing was stored.")
    return {"app-id": str(app["id"]), "slug": app["slug"], "private-key.pem": app["pem"]}
