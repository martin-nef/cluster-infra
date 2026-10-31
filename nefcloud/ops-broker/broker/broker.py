"""Ops broker: break-glass actions the agent asks for and a human approves.

The agent (or anyone allowed to) creates an OpsRequest in this namespace. The
CRD's schema already limits it to a fixed verb list:

  delete-pod        Pod (one with an owner, so it comes back)
  rollout-restart   Deployment, StatefulSet, DaemonSet
  flux-reconcile    Kustomization, HelmRelease, GitRepository, HelmRepository
  flux-suspend      (same kinds)
  flux-resume       (same kinds)

The broker checks the target exists, marks the request Pending, and sends an
ntfy notification linking to PUBLIC_URL/r/<name>. That page is behind Traefik's
oauth2-proxy (Google) login; the broker runs the action only when someone in
ALLOWED_APPROVERS presses Approve there. Nothing in the notification can
approve anything, so it doesn't matter who else reads the topic: the agent can
reach ntfy.sh too, but it can't log in as you.

Every transition is written to the request's status (the audit trail, kept for
RETENTION_DAYS) and printed as one JSON line. Unanswered requests expire after
REQUEST_TTL seconds; at most MAX_PENDING wait at once. A request approved but
not run when the broker stopped is failed, never run late.

Stdlib only, so it runs on a stock python image from a ConfigMap.
"""
import hashlib
import html
import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PUBLIC_URL = os.environ["PUBLIC_URL"].rstrip("/")
ALLOWED_APPROVERS = {e.strip().lower() for e in os.environ["ALLOWED_APPROVERS"].split(",") if e.strip()}
REQUEST_TTL = int(os.environ.get("REQUEST_TTL", "900"))
MAX_PENDING = int(os.environ.get("MAX_PENDING", "3"))
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "90"))
NTFY_URL = os.environ.get("NTFY_URL", "")  # https://ntfy.sh/<topic>; empty: no notifications

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
K8S_API = "https://kubernetes.default.svc"
NAMESPACE = "ops-broker"
REQUESTS = f"/apis/ops.nefcloud.top/v1alpha1/namespaces/{NAMESPACE}/opsrequests"
POLL = 5  # seconds between scans of the queue
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
FINISHED = {"Succeeded", "Failed", "Denied", "Expired", "Rejected"}

# Serialises every status change, so a request is decided and run at most once.
# The Deployment is Recreate, so this process is the only broker.
lock = threading.Lock()


class K8sError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code


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
    t = s["target"]
    return f"{s['verb']} {t['kind']} {t['namespace']}/{t['name']}"


def audit(event, req, **fields):
    print(json.dumps({"time": stamp(), "event": event, "request": req["metadata"]["name"],
                      "action": describe(req), **fields}), flush=True)


def set_status(req, **status):
    """Merge into the request's status; the local copy follows, for callers."""
    k8s("PATCH", f"{REQUESTS}/{req['metadata']['name']}/status", {"status": status})
    req.setdefault("status", {}).update(status)


def target_path(spec):
    t = spec["target"]
    return KINDS[t["kind"]].format(ns=t["namespace"], name=t["name"])


def check(req):
    """Why the request can't be run, or None. Looks the target up."""
    spec = req.get("spec", {})
    target = spec.get("target", {})
    if target.get("kind") not in VERBS.get(spec.get("verb"), ()):
        return f"verb {spec.get('verb')!r} can't target kind {target.get('kind')!r}"
    try:
        obj = k8s("GET", target_path(spec))
    except K8sError as e:
        return f"target: {e}"
    if spec["verb"] == "delete-pod" and not obj["metadata"].get("ownerReferences"):
        return "pod has no owner, so deleting it would lose it for good; do that by hand"
    return None


def run(spec):
    """Run the action. Returns a message for the status; raises on failure."""
    verb, path = spec["verb"], target_path(spec)
    if verb == "delete-pod":
        k8s("DELETE", path)
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


def scan():
    """One pass over the queue: admit new requests, expire and recover old ones."""
    with lock:
        items = sorted(list_requests(), key=lambda r: r["metadata"]["creationTimestamp"])
        pending = sum(phase(r) == "Pending" for r in items)
        for req in items:
            p, created = phase(req), parse_time(req["metadata"]["creationTimestamp"])
            if p == "":
                problem = check(req)
                if problem is None and pending >= MAX_PENDING:
                    problem = f"{MAX_PENDING} requests already wait for a decision"
                if problem:
                    set_status(req, phase="Rejected", message=problem, finishedAt=stamp())
                    audit("rejected", req, reason=problem)
                    continue
                set_status(req, phase="Pending", specHash=spec_hash(req),
                           message=f"waiting for approval at {PUBLIC_URL}/r/{req['metadata']['name']}")
                pending += 1
                audit("pending", req, reason=req["spec"]["reason"])
                notify(req)
            elif p == "Pending" and (now() - created).total_seconds() > REQUEST_TTL:
                set_status(req, phase="Expired", message="nobody approved it in time", finishedAt=stamp())
                pending -= 1
                audit("expired", req)
            elif p == "Approved":
                # Only possible if the broker stopped between approving and
                # running. Running it now could be long after the decision.
                set_status(req, phase="Failed", finishedAt=stamp(),
                           message="the broker restarted before running it; not retried, request it again")
                audit("failed", req, error="restarted before running")
            elif p in FINISHED and (now() - created).days > RETENTION_DAYS:
                k8s("DELETE", f"{REQUESTS}/{req['metadata']['name']}")


def decide(name, decision, seen_hash, approver):
    """Approve (and run) or deny one request. Returns (http status, message)."""
    with lock:
        try:
            req = k8s("GET", f"{REQUESTS}/{name}")
        except K8sError as e:
            return (404 if e.code == 404 else 502), str(e)
        if phase(req) != "Pending":
            return 409, f"it is {phase(req) or 'not yet admitted'}, not Pending"
        if (now() - parse_time(req["metadata"]["creationTimestamp"])).total_seconds() > REQUEST_TTL:
            return 409, "it has expired"
        # spec is immutable in the CRD; this also catches a page that went stale.
        if seen_hash != spec_hash(req) or seen_hash != req["status"].get("specHash"):
            return 409, "the request changed since the page was loaded"
        if decision == "deny":
            set_status(req, phase="Denied", decidedBy=approver, decidedAt=stamp(),
                       finishedAt=stamp(), message=f"denied by {approver}")
            audit("denied", req, by=approver)
            return 200, "Denied."
        set_status(req, phase="Approved", decidedBy=approver, decidedAt=stamp(), message="running")
        audit("approved", req, by=approver)
        try:
            result = run(req["spec"])
        except Exception as e:
            set_status(req, phase="Failed", finishedAt=stamp(), message=str(e))
            audit("failed", req, error=str(e))
            return 502, f"Approved, but it failed: {e}"
        set_status(req, phase="Succeeded", finishedAt=stamp(), message=result)
        audit("succeeded", req, result=result)
        return 200, f"Done: {result}."


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
</style></head><body>{body}</body></html>"""


def e(s):
    return html.escape(str(s), quote=True)


def request_page(req):
    s, st = req["spec"], req.get("status", {})
    t = s["target"]
    rows = [("Verb", f"<code>{e(s['verb'])}</code>"),
            ("Target", f"<code>{e(t['kind'])} {e(t['namespace'])}/{e(t['name'])}</code>"),
            ("Phase", e(st.get("phase", "new"))),
            ("Status", e(st.get("message", ""))),
            ("Created", e(req["metadata"]["creationTimestamp"]))]
    if st.get("decidedBy"):
        rows.append(("Decided by", e(st["decidedBy"])))
    body = (f"<h1>{e(req['metadata']['name'])}</h1><table>"
            + "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in rows)
            + "</table><p>The requester's reason (their claim, not checked):</p>"
            + f"<p class=reason>{e(s['reason'])}</p>")
    if st.get("phase") == "Pending":
        h = e(st.get("specHash", ""))
        body += "".join(
            f'<form method=post><input type=hidden name=hash value="{h}">'
            f'<button class={d} name=decision value={d}>{d.capitalize()}</button></form>'
            for d in ("approve", "deny"))
    return body + '<p><a href="/">All requests</a></p>'


def index_page(items):
    items = sorted(items, key=lambda r: r["metadata"]["creationTimestamp"], reverse=True)[:30]
    rows = "".join(
        f'<tr><td><a href="/r/{e(r["metadata"]["name"])}">{e(r["metadata"]["name"])}</a></td>'
        f"<td><code>{e(describe(r))}</code></td><td>{e(phase(r) or 'new')}</td></tr>"
        for r in items)
    return f"<h1>Ops requests</h1><table>{rows or '<tr><td>None yet.</td></tr>'}</table>"


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
        try:
            if self.path == "/":
                return self.reply(200, index_page(list_requests()), html_page=True)
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

    def reply(self, status, content, html_page=False):
        data = (PAGE.format(body=content) if html_page else content + "\n").encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8" if html_page else "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'")
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
