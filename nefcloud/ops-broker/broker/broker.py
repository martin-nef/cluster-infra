"""Ops broker: break-glass actions the agent asks for and a human approves.

The agent (or anyone allowed to) creates an OpsRequest in this namespace. The
CRD's schema already limits it to a fixed verb list:

  delete-pod        Pod (one a ReplicaSet, StatefulSet or DaemonSet recreates)
  rollout-restart   Deployment, StatefulSet, DaemonSet
  flux-reconcile    Kustomization, HelmRelease, GitRepository, HelmRepository
  flux-suspend      (same kinds)
  flux-resume       (same kinds)

and, through a separate GitHub App, the GitHub actions the agent's own App may
not take (githubops.py). The App's key is held by the token minter sidecar
(minter.py), not by this process, which only gets one-repo, one-step tokens:

  github-commit-workflows, github-protect-branch,
  github-rerun-workflow, github-dispatch-workflow

The broker checks the target exists, marks the request Pending, and sends an
ntfy notification linking to PUBLIC_URL/r/<name>. That page is behind Traefik's
oauth2-proxy (Google) login; the broker runs the action only when someone in
ALLOWED_APPROVERS presses Approve there. Nothing in the notification can
approve anything, so it doesn't matter who else reads the topic: the agent can
reach ntfy.sh too, but it can't log in as you.

Every transition is written to the request's status (the audit trail, kept for
RETENTION_DAYS; Rejected and Expired ones, which decided nothing, for
SHORT_RETENTION_DAYS) and printed as one JSON line. Unanswered requests expire
after REQUEST_TTL seconds; at most MAX_PENDING wait at once.

Status writes carry the resourceVersion the broker read, so if two broker pods
overlap (a deleted pod's replacement starts before it has gone), only one can
claim an approval. When the broker can't tell whether an action took effect (a
timeout or 5xx from the API server, or a stop between approving and recording
the result), the request ends Unknown: check the target before asking again.
It is never run late or retried.

Stdlib only, so it runs on a stock python image from a ConfigMap.
"""
import hashlib
import html
import json
import os
import re
import secrets
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# -I leaves the script's directory off sys.path; githubops.py sits beside it.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import githubops  # noqa: E402

PUBLIC_URL = os.environ["PUBLIC_URL"].rstrip("/")
ALLOWED_APPROVERS = {e.strip().lower() for e in os.environ["ALLOWED_APPROVERS"].split(",") if e.strip()}
REQUEST_TTL = int(os.environ.get("REQUEST_TTL", "900"))
MAX_PENDING = int(os.environ.get("MAX_PENDING", "3"))
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "90"))
SHORT_RETENTION_DAYS = int(os.environ.get("SHORT_RETENTION_DAYS", "7"))
NTFY_URL = os.environ.get("NTFY_URL", "")  # https://ntfy.sh/<topic>; empty: no notifications
GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "martin-nef")  # the account whose repos the GitHub verbs touch

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
K8S_API = "https://kubernetes.default.svc"
NAMESPACE = "ops-broker"
REQUESTS = f"/apis/ops.nefcloud.top/v1alpha1/namespaces/{NAMESPACE}/opsrequests"
# The token minter sidecar, the only container that holds the GitHub App's key
# (minter.py). Its socket is in an emptyDir the two containers share.
MINTER = githubops.Minter(os.environ.get("MINTER_SOCKET", "/run/minter/minter.sock"), GITHUB_OWNER)
SETUP_TTL = 600  # seconds a started App setup may take on GitHub
POLL = 5  # seconds between scans of the queue
# An Approved request older than this was left by a broker that stopped mid-run
# (running takes seconds); younger ones may still be running in another pod.
STRANDED_AFTER = 60
NAME = re.compile(r"[a-z0-9]([-.a-z0-9]{0,251}[a-z0-9])?")

# Where each target kind lives in the API.
KINDS = {
    "Pod": "/api/v1/namespaces/{ns}/pods/{name}",
    "Deployment": "/apis/apps/v1/namespaces/{ns}/deployments/{name}",
    "StatefulSet": "/apis/apps/v1/namespaces/{ns}/statefulsets/{name}",
    "DaemonSet": "/apis/apps/v1/namespaces/{ns}/daemonsets/{name}",
    "Kustomization": "/apis/kustomize.toolkit.fluxcd.io/v1/namespaces/{ns}/kustomizations/{name}",
    "HelmRelease": "/apis/helm.toolkit.fluxcd.io/v2/namespaces/{ns}/helmreleases/{name}",
    "GitRepository": "/apis/source.toolkit.fluxcd.io/v1/namespaces/{ns}/gitrepositories/{name}",
    "HelmRepository": "/apis/source.toolkit.fluxcd.io/v1/namespaces/{ns}/helmrepositories/{name}",
}
FLUX_KINDS = {"Kustomization", "HelmRelease", "GitRepository", "HelmRepository"}
# The CRD enforces the same; checked again here so a looser CRD can't widen it.
VERBS = {
    "delete-pod": {"Pod"},
    "rollout-restart": {"Deployment", "StatefulSet", "DaemonSet"},
    "flux-reconcile": FLUX_KINDS,
    "flux-suspend": FLUX_KINDS,
    "flux-resume": FLUX_KINDS,
}
GITHUB_VERBS = set(githubops.VERBS)
# Pods these recreate under a controller; Job pods and bare pods may not come back.
POD_OWNERS = {"ReplicaSet", "StatefulSet", "DaemonSet"}
FINISHED = {"Succeeded", "Failed", "Unknown", "Denied", "Expired", "Rejected"}
SHORT_LIVED = {"Expired", "Rejected"}

# Serialises every status change, so a request is decided and run at most once.
# The Deployment is Recreate, so this process is the only broker.
lock = threading.Lock()


class K8sError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code

    @property
    def transient(self):
        """Worth trying again later; for a write, it may also have taken effect."""
        return self.code in (408, 429) or self.code >= 500


class Unknown(Exception):
    """The action may or may not have taken effect."""


def k8s(method, path, body=None, content_type="application/merge-patch+json"):
    # Re-read on every call: the projected token is rotated by the kubelet.
    with open(f"{SA_DIR}/token") as f:
        token = f.read().strip()
    req = urllib.request.Request(
        K8S_API + path,
        data=None if body is None else json.dumps(body).encode(),
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": content_type},
    )
    context = ssl.create_default_context(cafile=f"{SA_DIR}/ca.crt")
    try:
        with urllib.request.urlopen(req, context=context, timeout=10) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            message = json.load(e).get("message", e.reason)
        except ValueError:
            message = e.reason
        raise K8sError(e.code, message) from None


def now():
    return datetime.now(timezone.utc)


def stamp(t=None):
    return (t or now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def spec_hash(req):
    return hashlib.sha256(json.dumps(req["spec"], sort_keys=True).encode()).hexdigest()[:16]


def phase(req):
    return req.get("status", {}).get("phase", "")


def describe(req):
    s = req["spec"]
    if s["verb"] in GITHUB_VERBS:
        return githubops.describe(GITHUB_OWNER, s)
    t = s["target"]
    return f"{s['verb']} {t['kind']} {t['namespace']}/{t['name']}"


def audit(event, req, **fields):
    print(json.dumps({"time": stamp(), "event": event, "request": req["metadata"]["name"],
                      "action": describe(req), **fields}), flush=True)


def set_status(req, **status):
    """Merge into the request's status if it is unchanged since it was read; a
    409 K8sError otherwise. The local copy follows, for callers."""
    patch = {"metadata": {"resourceVersion": req["metadata"]["resourceVersion"]}, "status": status}
    updated = k8s("PATCH", f"{REQUESTS}/{req['metadata']['name']}/status", patch)
    req["metadata"]["resourceVersion"] = updated["metadata"]["resourceVersion"]
    req.setdefault("status", {}).update(status)


def target_path(spec):
    t = spec["target"]
    return KINDS[t["kind"]].format(ns=t["namespace"], name=t["name"])


def check(req):
    """Look the target up. Returns (why it can't be run or None, the target's
    UID). Raises on transient API errors, so the next scan tries again."""
    spec = req.get("spec", {})
    if spec.get("verb") in GITHUB_VERBS:
        return check_github(spec), None
    target = spec.get("target", {})
    if target.get("kind") not in VERBS.get(spec.get("verb"), ()):
        return f"verb {spec.get('verb')!r} can't target kind {target.get('kind')!r}", None
    try:
        obj = k8s("GET", target_path(spec))
    except K8sError as e:
        if e.transient:
            raise
        return f"target: {e}", None
    if spec["verb"] == "delete-pod":
        owners = [o for o in obj["metadata"].get("ownerReferences", []) if o.get("controller")]
        if not owners or owners[0].get("kind") not in POD_OWNERS:
            return ("pod isn't controlled by a ReplicaSet, StatefulSet or DaemonSet, so it may not "
                    "come back; do that by hand"), None
    return None, obj["metadata"].get("uid")


def check_github(spec):
    if not isinstance(spec.get("github"), dict) or "target" in spec:
        return "GitHub verbs take spec.github, not spec.target"
    try:
        if MINTER.status() is None:
            return f"the GitHub verbs need the ops App, which isn't set up: {PUBLIC_URL}/github"
        return githubops.check(MINTER, spec)
    except githubops.GitHubError as e:
        if e.transient:
            raise
        return f"GitHub: {e}"


def run(req):
    """Run the action. Returns a message for the status. Raises K8sError when
    the API server refused it, Unknown when it may have taken effect."""
    try:
        return act(req["spec"], req["status"].get("targetUID"))
    except (K8sError, githubops.GitHubError) as e:
        if e.transient:
            raise Unknown(str(e)) from None
        raise
    except (OSError, json.JSONDecodeError) as e:  # timeouts, resets: it may have arrived
        raise Unknown(repr(e)) from None


def act(spec, uid):
    if spec["verb"] in GITHUB_VERBS:
        return githubops.act(MINTER, spec)
    verb, path = spec["verb"], target_path(spec)
    if verb == "delete-pod":
        # Only the pod that was checked: a StatefulSet's replacement has the same name.
        try:
            k8s("DELETE", path, {"preconditions": {"uid": uid}}, content_type="application/json")
        except K8sError as e:
            if e.code == 409:
                raise K8sError(409, "the pod was replaced since the request; nothing deleted") from None
            raise
        return "deleted"
    if verb == "rollout-restart":
        k8s("PATCH", path, {"spec": {"template": {"metadata": {"annotations": {
            "kubectl.kubernetes.io/restartedAt": stamp()}}}}})
        return "restart triggered"
    if verb == "flux-reconcile":
        # What `flux reconcile` does; a suspended object ignores it.
        k8s("PATCH", path, {"metadata": {"annotations": {
            "reconcile.fluxcd.io/requestedAt": now().isoformat()}}})
        return "reconcile requested"
    if verb in ("flux-suspend", "flux-resume"):
        k8s("PATCH", path, {"spec": {"suspend": verb == "flux-suspend"}})
        return "suspended" if verb == "flux-suspend" else "resumed"
    raise ValueError(f"unknown verb {verb!r}")


def notify(req):
    if not NTFY_URL:
        return
    server, _, topic = NTFY_URL.rstrip("/").rpartition("/")
    url = f"{PUBLIC_URL}/r/{req['metadata']['name']}"
    body = {
        "topic": topic,
        "title": f"Approve? {describe(req)}",
        "message": req["spec"]["reason"][:300],
        "priority": 4,
        "tags": ["rotating_light"],
        "click": url,
        "actions": [{"action": "view", "label": "Review", "url": url, "clear": True}],
    }
    try:
        urllib.request.urlopen(urllib.request.Request(
            server, data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json"}), timeout=10).close()
    except (urllib.error.URLError, OSError) as e:
        # The page still lists it; a lost notification only costs time.
        print(f"ntfy: {e}", flush=True)


def list_requests():
    return k8s("GET", REQUESTS)["items"]


def age(req):
    return (now() - parse_time(req["metadata"]["creationTimestamp"])).total_seconds()


def tidy(req):
    """Expire, recover or delete one request that is past its time."""
    p = phase(req)
    if p in ("", "Pending") and age(req) > REQUEST_TTL:
        set_status(req, phase="Expired", message="nobody approved it in time", finishedAt=stamp())
        audit("expired", req)
    elif p == "Approved" and (now() - parse_time(req["status"].get("decidedAt") or stamp())).total_seconds() > STRANDED_AFTER:
        # The broker stopped while running it, before recording the outcome.
        # Running it now could be long after the decision, so it never is.
        set_status(req, phase="Unknown", finishedAt=stamp(),
                   message="the broker stopped while running it; check the target before requesting it again")
        audit("unknown", req, error="broker stopped mid-run")
    elif p in FINISHED and age(req) > 86400 * (SHORT_RETENTION_DAYS if p in SHORT_LIVED else RETENTION_DAYS):
        k8s("DELETE", f"{REQUESTS}/{req['metadata']['name']}")


def admit(req, pending):
    """Check a new request and either reject it or make it Pending. Returns
    whether it is now Pending."""
    problem, uid = check(req)
    if problem is None and pending >= MAX_PENDING:
        problem = f"{MAX_PENDING} requests already wait for a decision"
    if problem:
        set_status(req, phase="Rejected", message=problem, finishedAt=stamp())
        audit("rejected", req, reason=problem)
        return False
    set_status(req, phase="Pending", specHash=spec_hash(req), targetUID=uid,
               message=f"waiting for approval at {PUBLIC_URL}/r/{req['metadata']['name']}")
    audit("pending", req, reason=req["spec"]["reason"])
    notify(req)
    return True


def each(items, fn):
    """fn on every item; one request's error (a conflict with another broker
    pod, a transient API error) only skips it until the next scan."""
    for req in items:
        try:
            fn(req)
        except Exception as e:
            print(f"scan {req['metadata']['name']}: {e!r}", flush=True)


def scan():
    """One pass over the queue: expire, recover and delete old requests first,
    so stale ones never take a pending slot, then admit new ones."""
    with lock:
        items = sorted(list_requests(), key=lambda r: r["metadata"]["creationTimestamp"])
        each(items, tidy)
        pending = sum(phase(r) == "Pending" for r in items)

        def admit_new(req):
            nonlocal pending
            if phase(req) == "" and admit(req, pending):
                pending += 1
        each(items, admit_new)


def decide(name, decision, seen_hash, approver):
    """Approve (and run) or deny one request. Returns (http status, message)."""
    with lock:
        try:
            req = k8s("GET", f"{REQUESTS}/{name}")
        except K8sError as e:
            return (404 if e.code == 404 else 502), str(e)
        if phase(req) != "Pending":
            return 409, f"it is {phase(req) or 'not yet admitted'}, not Pending"
        if age(req) > REQUEST_TTL:
            return 409, "it has expired"
        # spec is immutable in the CRD; this also catches a page that went stale.
        if seen_hash != spec_hash(req) or seen_hash != req["status"].get("specHash"):
            return 409, "the request changed since the page was loaded"
        # The claim is conditional on the version just read: if another broker
        # pod decided it in the meantime, this one stops here.
        try:
            if decision == "deny":
                set_status(req, phase="Denied", decidedBy=approver, decidedAt=stamp(),
                           finishedAt=stamp(), message=f"denied by {approver}")
                audit("denied", req, by=approver)
                return 200, "Denied."
            set_status(req, phase="Approved", decidedBy=approver, decidedAt=stamp(), message="running")
        except K8sError as e:
            return (409 if e.code == 409 else 502), f"not recorded: {e}"
        audit("approved", req, by=approver)
        try:
            result = run(req)
        except Unknown as e:
            return finish(req, "Unknown", f"may or may not have taken effect ({e}); check the target",
                          502, "Approved, but the outcome is unknown")
        except Exception as e:
            return finish(req, "Failed", str(e), 502, "Approved, but it failed")
        return finish(req, "Succeeded", result, 200, "Done")


def finish(req, final, message, code, summary):
    """Record the outcome. If even that fails, the request stays Approved and
    the scan marks it Unknown, which is still true."""
    audit(final.lower(), req, result=message)
    try:
        set_status(req, phase=final, finishedAt=stamp(), message=message)
    except Exception as e:
        print(f"recording {req['metadata']['name']} as {final}: {e!r}", flush=True)
    return code, f"{summary}: {message}."


def scan_loop():
    while True:
        try:
            scan()
        except Exception as e:
            print(f"scan: {e!r}", flush=True)
        time.sleep(POLL)


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Ops broker</title>
<style>
body{{font:16px/1.4 system-ui,sans-serif;margin:0 auto;max-width:40rem;padding:1rem;color:#1b1b1b;background:#fafafa}}
@media(prefers-color-scheme:dark){{body{{color:#e8e8e8;background:#161616}}a{{color:#8ab4f8}}}}
code{{font-size:1.05em}} table{{border-collapse:collapse;width:100%}} td{{padding:.3rem .5rem .3rem 0;vertical-align:top}}
.reason{{white-space:pre-wrap;border-left:3px solid #888;padding-left:.6rem}}
form{{display:inline}} button{{font-size:1.1rem;padding:.7rem 1.4rem;margin:.6rem .6rem 0 0;border:0;border-radius:.4rem;color:#fff}}
.approve{{background:#1a7f37}} .deny{{background:#b42318}}
pre{{overflow-x:auto;font-size:.85rem;background:#8881;padding:.5rem}} .add{{color:#1a7f37}} .del{{color:#b42318}} .hunk{{color:#888}}
.warn{{border-left:3px solid #b42318;padding-left:.6rem}}
</style></head><body>{body}</body></html>"""


def e(s):
    return html.escape(str(s), quote=True)


def request_page(req):
    s, st = req["spec"], req.get("status", {})
    github = s["verb"] in GITHUB_VERBS
    if github:
        target = f"<code>{e(GITHUB_OWNER)}/{e(s['github']['repo'])}</code>"
    else:
        t = s["target"]
        target = f"<code>{e(t['kind'])} {e(t['namespace'])}/{e(t['name'])}</code>"
    rows = [("Verb", f"<code>{e(s['verb'])}</code>"),
            ("Target", target),
            ("Phase", e(st.get("phase", "new"))),
            ("Status", e(st.get("message", ""))),
            ("Created", e(req["metadata"]["creationTimestamp"]))]
    if st.get("decidedBy"):
        rows.append(("Decided by", e(st["decidedBy"])))
    body = (f"<h1>{e(req['metadata']['name'])}</h1><table>"
            + "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in rows)
            + "</table><p>The requester's reason (their claim, not checked):</p>"
            + f"<p class=reason>{e(s['reason'])}</p>")
    can_approve = True
    if github and st.get("phase") == "Pending":
        # What approving does, read live, so the page shows what will run: for a
        # commit, the diff against the exact commit it will sit on. Decided
        # requests skip it; their status says what happened.
        try:
            body += "<h2>What it does</h2>" + githubops.details(MINTER, s)
        except Exception as err:
            body += (f"<p class=warn>Couldn't show what this does ({e(err)}), so it can't be approved "
                     "from here. Reload, or deny it.</p>")
            can_approve = False
    if st.get("phase") == "Pending":
        h = e(st.get("specHash", ""))
        body += "".join(
            f'<form method=post><input type=hidden name=hash value="{h}">'
            f'<button class={d} name=decision value={d}>{d.capitalize()}</button></form>'
            for d in ("approve", "deny") if d == "deny" or can_approve)
    return body + '<p><a href="/">All requests</a></p>'


def index_page(items):
    """Every request waiting for a decision, then the 30 newest others."""
    items = sorted(items, key=lambda r: r["metadata"]["creationTimestamp"], reverse=True)
    items = [r for r in items if phase(r) == "Pending"] + [r for r in items if phase(r) != "Pending"][:30]
    rows = "".join(
        f'<tr><td><a href="/r/{e(r["metadata"]["name"])}">{e(r["metadata"]["name"])}</a></td>'
        f"<td><code>{e(describe(r))}</code></td><td>{e(phase(r) or 'new')}</td></tr>"
        for r in items)
    return (f"<h1>Ops requests</h1><table>{rows or '<tr><td>None yet.</td></tr>'}</table>"
            '<p><a href="/github">GitHub App</a></p>')


# Started App setups: state -> (approver, deadline). In memory: the broker is
# the only replica, and a setup lost to a restart is simply started again.
setups = {}


def github_page():
    """The App's state, and a button that creates it if there is none."""
    try:
        app, manifest = MINTER.status(), MINTER.manifest()
    except githubops.GitHubError as err:
        return f"<p>Can't reach the token minter: {e(err)}</p>", False
    if app is not None:
        return (f"<h1>GitHub App</h1><p>Set up: <code>{e(app['slug'])}</code> (id {e(app['app_id'])}), key in "
                "the <code>ops-broker-github-app</code> Secret, which only the token minter can read. "
                f"{githubops.link(f'https://github.com/apps/{app['slug']}/installations/new', 'Install or change repos')}. "
                "To replace it, delete the App on GitHub and the Secret in Headlamp, roll the pod, then reload.</p>"
                '<p><a href="/">All requests</a></p>'), False
    return ("<h1>GitHub App</h1><p>The GitHub verbs need an App whose key only the broker's token minter holds. "
            f"The button asks GitHub to create <code>{e(manifest['name'])}</code>, private to "
            f"<code>{e(GITHUB_OWNER)}</code>, with: <code>{e(json.dumps(manifest['default_permissions']))}</code>. "
            "GitHub hands the key back to the minter, which keeps it in a Secret in its own "
            "namespace: it never reaches git, a PC, the agent, or the broker's own process.</p>"
            '<form method=post action="/github"><button class=approve>Create the App on GitHub</button></form>'
            '<p><a href="/">All requests</a></p>'), True


def start_setup(approver):
    """A page that posts the App manifest to GitHub straight away."""
    manifest = json.dumps(MINTER.manifest())
    now_ = time.time()
    for state in [s for s, (_, deadline) in setups.items() if deadline < now_]:
        del setups[state]
    state = secrets.token_urlsafe(24)
    setups[state] = (approver, now_ + SETUP_TTL)
    target = f"https://github.com/settings/apps/new?state={urllib.parse.quote(state)}"
    return (f'<form method=post action="{e(target)}"><input type=hidden name=manifest value="{e(manifest)}">'
            "<p>GitHub will ask you to confirm the App's name, then send you back here.</p>"
            "<button class=approve>Continue to GitHub</button></form>")


def finish_setup(approver, code, state):
    """Trade GitHub's code for the key and store it. Returns (http status, page)."""
    started = setups.pop(state, None)
    if started is None or started[0] != approver or started[1] < time.time():
        return 400, "<p>This setup wasn't started from this page by you, or took too long. <a href=\"/github\">Start again</a>.</p>"
    # The minter trades the code for the key itself; this process only relays it.
    try:
        app = MINTER.setup(code)
    except githubops.GitHubError as err:
        return 502, f"<p>Not set up: {e(err)}</p>"
    print(json.dumps({"time": stamp(), "event": "github-app-created", "slug": app["slug"],
                      "id": app["app_id"], "by": approver}), flush=True)
    install = githubops.link(f"https://github.com/apps/{app['slug']}/installations/new", "install it")
    return 200, (f"<p>Created <code>{e(app['slug'])}</code>. Now {install} on {e(GITHUB_OWNER)}. All "
                 "repositories is fine: every token it mints is scoped to one repo and one step.</p>")


class Handler(BaseHTTPRequestHandler):
    def approver(self):
        """The signed-in email if it may approve. Set by oauth2-proxy via Traefik,
        the only thing the NetworkPolicy lets in."""
        email = self.headers.get("X-Auth-Request-Email", "").strip().lower()
        return email if email in ALLOWED_APPROVERS else None

    def do_GET(self):
        if self.path == "/healthz":
            return self.reply(200, "ok")
        if not self.approver():
            return self.reply(403, "not an approver")
        url = urllib.parse.urlsplit(self.path)
        try:
            if self.path == "/":
                return self.reply(200, index_page(list_requests()), html_page=True)
            if self.path == "/github":
                return self.reply(200, github_page()[0], html_page=True)
            if url.path == "/github/callback":
                query = urllib.parse.parse_qs(url.query)
                status, page = finish_setup(self.approver(), query.get("code", [""])[0],
                                            query.get("state", [""])[0])
                return self.reply(status, page, html_page=True)
            if self.path.startswith("/r/"):
                return self.reply(200, request_page(k8s("GET", f"{REQUESTS}/{self.request_name()}")), html_page=True)
        except K8sError as err:
            return self.reply(404 if err.code == 404 else 502, str(err))
        self.reply(404, "not found")

    def do_POST(self):
        approver = self.approver()
        if not approver:
            return self.reply(403, "not an approver")
        # Blocks cross-site form posts; the SameSite=lax cookie mostly does too.
        if self.headers.get("Origin") != PUBLIC_URL:
            return self.reply(403, "bad origin")
        if self.path == "/github":
            if not github_page()[1]:
                return self.reply(409, "the App is already set up, or the token minter can't be reached")
            # Only this page may post the manifest on to GitHub.
            try:
                page = start_setup(approver)
            except githubops.GitHubError as err:
                return self.reply(502, str(err))
            return self.reply(200, page, html_page=True, form_action="'self' https://github.com")
        if not self.path.startswith("/r/"):
            return self.reply(404, "not found")
        try:
            name = self.request_name()
        except K8sError as err:
            return self.reply(404, str(err))
        try:
            length = int(self.headers["Content-Length"])
        except (TypeError, ValueError):
            return self.reply(411, "length required")
        if not 0 <= length <= 4096:
            return self.reply(413, "too large")
        form = urllib.parse.parse_qs(self.rfile.read(length).decode())
        decision = form.get("decision", [""])[0]
        if decision not in ("approve", "deny"):
            return self.reply(400, "decision must be approve or deny")
        status, message = decide(name, decision, form.get("hash", [""])[0], approver)
        self.reply(status, f"<p>{e(message)}</p><p><a href=\"{e(self.path)}\">Back</a></p>", html_page=True)

    def request_name(self):
        """The object name from /r/<name>; a 404 for anything that isn't one."""
        name = self.path[len("/r/"):].split("?")[0]
        if not NAME.fullmatch(name):
            raise K8sError(404, "not a request name")
        return name

    def reply(self, status, content, html_page=False, form_action="'self'"):
        data = (PAGE.format(body=content) if html_page else content + "\n").encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8" if html_page else "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", f"default-src 'none'; style-src 'unsafe-inline'; form-action {form_action}")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        pass  # decisions are audited; this would add probe noise


def main():
    threading.Thread(target=scan_loop, daemon=True).start()
    server = ThreadingHTTPServer(("", 8080), Handler)
    print(f"Listening on :8080; approvals at {PUBLIC_URL}; ntfy {'on' if NTFY_URL else 'off'}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
