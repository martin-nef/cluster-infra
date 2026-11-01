"""Token minter: the only part of the ops broker that holds the ops App's key.

It runs as a sidecar in the broker's pod and is the only container that mounts
the ops-broker-github-app Secret. The broker (broker.py, githubops.py) serves
the approval page, parses agent-written requests and runs the verbs; it never
sees the key, only the short-lived tokens this hands out. A bug in the broker's
HTTP handling or request parsing then gives away one-repo, one-step tokens, not
a key that can mint anything the App can do on every repo.

It listens on a Unix socket in an emptyDir the two containers share: no port,
nothing for the NetworkPolicy to cover. One JSON request per connection, one
JSON reply:

  {"op": "status"}                        -> {"app": {"app_id", "slug"} or null, "oauth": {"client_id"} or null}
  {"op": "manifest"}                      -> {"manifest": {...}}, for the setup page
  {"op": "token", "repo", "verb", "step"} -> {"token": "..."}
  {"op": "setup", "code"}                 -> {"app": {"app_id", "slug"}}
  {"op": "oauth-setup", "client_secret"}  -> {"oauth": {"client_id"}}
  {"op": "user-action", "verb", "code", "github"} -> {"message": "..."}

Errors are {"error": {"code", "message"}}, with GitHub's status code where
there is one. A token request names a verb and a step ("check" or "run"), not
permissions: the permissions come from TOKENS, so the broker can't ask for more
than a verb needs. The broker revokes each token itself when its step ends.

Setup trades the manifest flow's one-time code for the new App's key and
stores it in the Secret. Until the kubelet fills the optional Secret volume in,
the key is held in memory, so the App works straight away.

A user action (USER_ACTIONS) is one GitHub can only take with a user's token,
e.g. creating a repo under the owner's account. The broker relays the code
from GitHub's OAuth redirect; this process trades it for a user access token
with the App's client secret, checks the token is the owner's, takes that one
action and revokes the token. The token never leaves this process. Errors
from a step before the action carry "nothing_changed": true.

Stdlib only, like broker.py: the App's JWT is signed with RSA in pure Python,
because the stock python image has no openssl CLI.
"""
import base64
import hashlib
import json
import os
import re
import socketserver
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "martin-nef")
GITHUB_APP_NAME = os.environ.get("GITHUB_APP_NAME", "martin-nef-ops")
SOCKET = os.environ.get("MINTER_SOCKET", "/run/minter/minter.sock")
# The ops-broker-github-app Secret, mounted here and nowhere else.
KEY_DIR = os.environ.get("MINTER_KEY_DIR", "/etc/github-app")
# The ops-broker-github-oauth Secret: the App's OAuth client, for user actions.
OAUTH_DIR = os.environ.get("MINTER_OAUTH_DIR", "/etc/github-oauth")

GITHUB_API = "https://api.github.com"
GITHUB_WEB = "https://github.com"
SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
K8S_API = "https://kubernetes.default.svc"
NAMESPACE = "ops-broker"
GITHUB_SECRET = "ops-broker-github-app"
OAUTH_SECRET = "ops-broker-github-oauth"
MAX_REQUEST = 64 * 1024

# What the ops App is created with. Each verb's tokens take a subset.
PERMISSIONS = {
    "actions": "write",
    "administration": "write",
    "contents": "write",
    "metadata": "read",
    # github-human-review: read PRs and their reviews, set the human-review status.
    # An App created before these needs them added on GitHub (README).
    "pull_requests": "read",
    "statuses": "write",
    "workflows": "write",
}
# verb -> step -> the token's permissions. "check" covers the scan's checks and
# the approval page; "run" only an approved request; "sync" the broker's own
# pass over open PRs (github-human-review only).
TOKENS = {
    "github-commit-workflows": {"check": {"contents": "read"}, "run": {"contents": "write", "workflows": "write"}},
    "github-protect-branch": {"check": {"administration": "read"}, "run": {"administration": "write"}},
    "github-rerun-workflow": {"check": {"actions": "read"}, "run": {"actions": "write"}},
    "github-dispatch-workflow": {"check": {"actions": "read", "contents": "read"}, "run": {"actions": "write"}},
    # Checks the name is free: metadata on every repo the App is installed on,
    # since the repo it names doesn't exist yet. Runs as a user action instead.
    "github-create-repo": {"check": {"metadata": "read"}},
    # The broker's own pass: list the repos the App is installed on (every
    # repo, metadata only), read one repo's open PRs, reviews and statuses
    # (sync), and set a status there only when one has to change (write).
    "github-human-review": {"check": {"pull_requests": "read"}, "run": {"statuses": "write"},
                            "list": {"metadata": "read"},
                            "sync": {"pull_requests": "read", "statuses": "read"},
                            "write": {"statuses": "write"}},
    "github-require-human-review": {"check": {"administration": "read"}, "run": {"administration": "write"}},
}
# Verbs whose check token isn't scoped to the request's repo (it doesn't exist).
ACCOUNT_CHECKS = {"github-create-repo"}
# Steps whose token covers every repo, asked for with no repo. Only listing
# them: metadata can't read or change anything in a repo.
ACCOUNT_STEPS = {("github-human-review", "list")}
REPO_NAME = re.compile(r"[A-Za-z0-9_.-]+")


class GitHubError(Exception):
    def __init__(self, code, message, nothing_changed=False):
        super().__init__(f"{code}: {message}")
        self.code, self.nothing_changed = code, nothing_changed


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


# --- the APIs ------------------------------------------------------------------

def api(method, path, token=None, body=None, basic=None, base=GITHUB_API):
    """basic: (client_id, client_secret), for the OAuth client's own endpoints."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "nefcloud-ops-minter",
               "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if basic:
        headers["Authorization"] = "Basic " + base64.b64encode(":".join(basic).encode()).decode()
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, method=method, headers=headers,
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


def store_secret(creds, name=GITHUB_SECRET):
    """Create one of this container's Secrets. create can't be limited to a
    name in RBAC, and the pod's two containers share a ServiceAccount, so the
    broker could create Secrets too; only this container can read these (it
    mounts them)."""
    with open(f"{SA_DIR}/token") as f:
        token = f.read().strip()
    body = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": name, "namespace": NAMESPACE, "labels": {"app": "ops-broker"}},
            "stringData": creds}
    req = urllib.request.Request(f"{K8S_API}/api/v1/namespaces/{NAMESPACE}/secrets", method="POST",
                                 data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    context = ssl.create_default_context(cafile=f"{SA_DIR}/ca.crt")
    try:
        with urllib.request.urlopen(req, context=context, timeout=10):
            pass
    except urllib.error.HTTPError as e:
        raise GitHubError(e.code, f"storing {name}: {e.reason}") from None


def q(s):
    return urllib.parse.quote(str(s), safe="")


# --- the App -------------------------------------------------------------------

class App:
    """The ops App, acting on one owner's repos."""

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
        """An installation token for one repo (every repo, if None) with only
        these permissions."""
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
                         {"permissions": permissions, **({"repositories": [repo]} if repo else {})})
        except GitHubError as e:
            if e.code == 422:
                raise GitHubError(422, f"{self.slug} can't get {permissions} on {self.owner}/{repo} "
                                       f"(not installed there, or the repo doesn't exist): {e}") from None
            raise
        return minted["token"]


# The App and OAuth client this process set up, until the Secret volumes show them.
created = None
created_oauth = None
setup_lock = threading.Lock()


def load():
    """The App from the mounted Secret, or the one just set up, or None."""
    try:
        d = {}
        for k in ("app-id", "slug", "private-key.pem"):
            with open(os.path.join(KEY_DIR, k)) as f:
                d[k] = f.read()
        return App(d["app-id"].strip(), d["slug"].strip(), d["private-key.pem"], GITHUB_OWNER)
    except FileNotFoundError:
        return created


def load_oauth():
    """The App's OAuth client, (client_id, client_secret), or None."""
    try:
        with open(os.path.join(OAUTH_DIR, "client-id")) as f, open(os.path.join(OAUTH_DIR, "client-secret")) as g:
            return f.read().strip(), g.read().strip()
    except FileNotFoundError:
        return created_oauth


def callback_url():
    return f"{PUBLIC_URL}/github/authorize"


def manifest():
    """The manifest the setup page posts to GitHub."""
    return {
        "name": GITHUB_APP_NAME,
        "url": f"{PUBLIC_URL}/github",
        "description": "Break-glass GitHub actions for the nefcloud agent, each approved on ops.nefcloud.top.",
        "public": False,
        "redirect_url": f"{PUBLIC_URL}/github/callback",
        # Where GitHub's OAuth flow returns a user action's code.
        "callback_urls": [callback_url()],
        "default_permissions": PERMISSIONS,
        "default_events": [],
    }


def convert(code, owner):
    """Trade the manifest flow's one-time code for the new App's credentials
    and check GitHub made what was asked for. Returns ({app-id, slug,
    private-key.pem}, {client-id, client-secret})."""
    app = api("POST", f"/app-manifests/{q(code)}/conversions", body={})
    problems = []
    if app["owner"]["login"].lower() != owner.lower():
        problems.append(f"it belongs to {app['owner']['login']}, not {owner}")
    if app["permissions"] != PERMISSIONS:
        problems.append(f"its permissions are {app['permissions']}, not {PERMISSIONS}")
    if problems:
        raise ValueError(f"GitHub created {app['slug']}, but {'; '.join(problems)}. Delete it at "
                         f"{app['html_url']} and start again; nothing was stored.")
    return ({"app-id": str(app["id"]), "slug": app["slug"], "private-key.pem": app["pem"]},
            {"client-id": app["client_id"], "client-secret": app["client_secret"]})


def setup(code):
    global created, created_oauth
    with setup_lock:
        if load() is not None:
            raise GitHubError(409, "the App is already set up")
        try:
            creds, client = convert(code, GITHUB_OWNER)
        except (KeyError, ValueError) as e:
            raise GitHubError(502, str(e)) from None
        try:
            store_secret(creds)
        except (GitHubError, OSError) as e:
            raise GitHubError(502, f"GitHub created {creds['slug']}, but storing its key failed ({e}). "
                                   "Delete the App on GitHub and start again.") from None
        created = App(creds["app-id"], creds["slug"], creds["private-key.pem"], GITHUB_OWNER)
        audit("github-app-created", slug=creds["slug"], id=creds["app-id"])
        try:
            store_secret(client, OAUTH_SECRET)
            created_oauth = client["client-id"], client["client-secret"]
        except (GitHubError, OSError) as e:
            # The App works; only user actions wait for oauth_setup.
            audit("error", error=f"storing the OAuth client: {e}")
        return created


def oauth_setup(client_secret):
    """Store a client secret made on GitHub for an App set up before user
    actions existed. GitHub has no API to make one, so you paste it."""
    global created_oauth
    with setup_lock:
        app = load()
        if app is None:
            raise GitHubError(404, "the ops App isn't set up")
        if load_oauth() is not None:
            raise GitHubError(409, "the OAuth client is already set up")
        client_id = api("GET", "/app", app.jwt())["client_id"]
        # Revoking a made-up token tells a good client secret (404, no such
        # token) from a bad one (401), without changing anything.
        try:
            api("DELETE", f"/applications/{q(client_id)}/token", basic=(client_id, client_secret),
                body={"access_token": "gho_" + "0" * 36})
        except GitHubError as e:
            if e.code != 404:
                raise GitHubError(400 if e.code == 401 else e.code,
                                  f"GitHub didn't accept that client secret for {app.slug}: {e}") from None
        store_secret({"client-id": client_id, "client-secret": client_secret}, OAUTH_SECRET)
        created_oauth = client_id, client_secret
        audit("github-oauth-set-up", slug=app.slug)
        return client_id


# --- user actions --------------------------------------------------------------

def create_repo(token, g):
    repo = api("POST", "/user/repos", token, {
        "name": g["repo"], "private": g.get("private", True), "description": g.get("description", ""),
        # A first commit on the default branch, so it can be protected at once.
        "auto_init": True})
    return f"created {repo['full_name']} ({'private' if repo['private'] else 'public'})"


# verb -> the action, given the user's token and the request's spec.github.
USER_ACTIONS = {"github-create-repo": create_repo}


def user_action(verb, code, g):
    """Trade an OAuth code for the owner's token, take one action, revoke it."""
    action = USER_ACTIONS.get(verb) if isinstance(verb, str) else None
    if action is None:
        raise GitHubError(400, f"{verb!r} isn't a user action", nothing_changed=True)
    if not isinstance(g, dict) or not isinstance(g.get("repo"), str) or not REPO_NAME.fullmatch(g["repo"]) \
            or g["repo"] in (".", ".."):
        raise GitHubError(400, "user actions need github.repo", nothing_changed=True)
    client = load_oauth()
    if client is None:
        raise GitHubError(404, "the App's OAuth client isn't set up", nothing_changed=True)
    try:
        # Not the API host: GitHub's OAuth endpoints answer 200 with an error field.
        grant = api("POST", "/login/oauth/access_token", base=GITHUB_WEB, body={
            "client_id": client[0], "client_secret": client[1], "code": code, "redirect_uri": callback_url()})
    except (GitHubError, OSError) as e:
        raise GitHubError(502, f"trading the code: {e}", nothing_changed=True) from None
    token = grant.get("access_token")
    if not token:
        raise GitHubError(400, f"GitHub refused the code: {grant.get('error_description') or grant.get('error')}",
                          nothing_changed=True)
    try:
        try:
            login = api("GET", "/user", token)["login"]
        except (GitHubError, OSError) as e:
            raise GitHubError(502, f"checking who signed in: {e}", nothing_changed=True) from None
        if login.lower() != GITHUB_OWNER.lower():
            raise GitHubError(403, f"signed in to GitHub as {login}, not {GITHUB_OWNER}", nothing_changed=True)
        message = action(token, g)
        audit("user-action", verb=verb, repo=g["repo"], user=login)
        return message
    finally:
        try:
            api("DELETE", f"/applications/{q(client[0])}/token", basic=client, body={"access_token": token})
        except (GitHubError, OSError) as e:
            audit("error", error=f"revoking a user token: {e}")  # it expires in 8 hours anyway


def audit(event, **fields):
    print(json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event, **fields}),
          flush=True)


# --- the socket ----------------------------------------------------------------

def handle(req):
    """One request -> its reply."""
    op = req.get("op")
    if op == "status":
        app, client = load(), load_oauth()
        return {"app": app and {"app_id": app.app_id, "slug": app.slug},
                "oauth": client and {"client_id": client[0]}}
    if op == "manifest":
        return {"manifest": manifest()}
    if op == "token":
        repo, verb, step = req.get("repo"), req.get("verb"), req.get("step")
        permissions = TOKENS.get(verb, {}).get(step) if isinstance(verb, str) and isinstance(step, str) else None
        if permissions is None:
            raise GitHubError(400, f"no token for verb {verb!r}, step {step!r}")
        every = (verb, step) in ACCOUNT_STEPS
        if every and repo is not None:
            raise GitHubError(400, f"{verb} {step} is for every repo; ask with no repo")
        if not every and (not isinstance(repo, str) or not REPO_NAME.fullmatch(repo) or repo in (".", "..")):
            raise GitHubError(400, f"{repo!r} isn't a repo name")
        app = load()
        if app is None:
            raise GitHubError(404, "the ops App isn't set up")
        token = app.token(None if every or verb in ACCOUNT_CHECKS else repo, permissions)
        audit("token", repo=repo, verb=verb, step=step)
        return {"token": token}
    if op == "setup":
        if not isinstance(req.get("code"), str):
            raise GitHubError(400, "setup needs a code")
        app = setup(req["code"])
        return {"app": {"app_id": app.app_id, "slug": app.slug}}
    if op == "oauth-setup":
        if not isinstance(req.get("client_secret"), str) or not req["client_secret"].strip():
            raise GitHubError(400, "oauth-setup needs a client_secret")
        return {"oauth": {"client_id": oauth_setup(req["client_secret"].strip())}}
    if op == "user-action":
        if not isinstance(req.get("code"), str):
            raise GitHubError(400, "a user action needs a code", nothing_changed=True)
        return {"message": user_action(req.get("verb"), req["code"], req.get("github"))}
    raise GitHubError(400, f"unknown op {op!r}")


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            data = self.rfile.read(MAX_REQUEST + 1)
            if len(data) > MAX_REQUEST:
                raise GitHubError(413, "request too large")
            try:
                req = json.loads(data)
            except ValueError:
                raise GitHubError(400, "not JSON") from None
            if not isinstance(req, dict):
                raise GitHubError(400, "not a JSON object")
            reply = handle(req)
        except GitHubError as e:
            reply = {"error": {"code": e.code, "message": str(e).partition(": ")[2],
                               **({"nothing_changed": True} if e.nothing_changed else {})}}
        except OSError as e:  # GitHub unreachable, timed out
            reply = {"error": {"code": 503, "message": f"reaching GitHub: {e!r}"}}
        except Exception as e:
            audit("error", error=repr(e))
            reply = {"error": {"code": 500, "message": repr(e)}}
        self.wfile.write(json.dumps(reply).encode())


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def serve(path):
    if os.path.exists(path):
        os.unlink(path)  # left by a previous run of this container
    server = Server(path, Handler)
    os.chmod(path, 0o600)  # both containers run as the same user
    return server


def main():
    server = serve(SOCKET)
    print(f"Listening on {SOCKET}; App {'set up' if load() else 'not set up'}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
