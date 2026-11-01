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

and one GitHub only takes as you, so approving it also sends you through
GitHub's sign-in, and the minter acts with your token for that one call:

  github-create-repo

One verb isn't an action but a sign-off (README, "Human review required"):

  github-human-review   An agent flags a PR; nobody merges it until you
                        approve. The broker keeps a human-review status on
                        every open PR head of every repo the ops App is
                        installed on, but HUMAN_REVIEW_EXCLUDE (sync_loop).
  github-require-human-review
                        Makes that check required on a repo, with the one
                        standard ruleset. The broker files it for each
                        covered repo that lacks one.

The broker checks the target exists, marks the request Pending, and sends an
ntfy notification linking to PUBLIC_URL/r/<name>. That page is behind Traefik's
oauth2-proxy (Google) login; the broker runs the action only when someone in
ALLOWED_APPROVERS presses Approve there. Nothing in the notification can
approve anything, so it doesn't matter who else reads the topic: the agent can
reach ntfy.sh too, but it can't log in as you.

Every transition is written to the request's status (the audit trail, kept for
RETENTION_DAYS; Rejected and Expired ones, which decided nothing, for
SHORT_RETENTION_DAYS) and printed as one JSON line.

Requests from agents that run while you sleep (README, "Async requesters"):

- spec.requester is stamped by an admission policy from the API server's view
  of who created it, so the page and the caps can trust it.
- spec.ttlSeconds picks how long it waits, REQUEST_TTL by default and at most
  MAX_TTL.
- Each requester named in PENDING_CAPS has its own pending slots; everyone else
  shares MAX_PENDING. So a night agent can't take the interactive agent's slots.
- During QUIET_HOURS, a request that will still be waiting when they end isn't
  announced; one digest lists them all when they end.
- Approving checks the target again, so a request approved hours later never
  acts on a target that has since been replaced or moved on.

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
import itertools
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
from datetime import datetime, time as clock, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# -I leaves the script's directory off sys.path; githubops.py sits beside it.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import githubops  # noqa: E402

PUBLIC_URL = os.environ["PUBLIC_URL"].rstrip("/")
ALLOWED_APPROVERS = {e.strip().lower() for e in os.environ["ALLOWED_APPROVERS"].split(",") if e.strip()}
REQUEST_TTL = int(os.environ.get("REQUEST_TTL", "900"))
MAX_PENDING = int(os.environ.get("MAX_PENDING", "3"))  # shared by requesters not in PENDING_CAPS
MAX_TTL = int(os.environ.get("MAX_TTL", "43200"))
# "user=n,user=n": requesters (API server usernames) with pending slots of their own.
PENDING_CAPS = {u.strip(): int(n) for u, _, n in
                (c.rpartition("=") for c in os.environ.get("PENDING_CAPS", "").split(",") if c.strip())}
QUIET_HOURS = os.environ.get("QUIET_HOURS", "")  # "22:00-07:00" in QUIET_TZ; empty: never quiet
QUIET_TZ = os.environ.get("QUIET_TZ", "UTC")
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "90"))
SHORT_RETENTION_DAYS = int(os.environ.get("SHORT_RETENTION_DAYS", "7"))
NTFY_URL = os.environ.get("NTFY_URL", "")  # https://ntfy.sh/<topic>; empty: no notifications
GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "martin-nef")  # the account whose repos the GitHub verbs touch
# Every repo the ops App is installed on gets human-review statuses and the
# ruleset that requires them, but these. The reviewer's flags there aren't
# filed either (an agent's own still are).
HUMAN_REVIEW_EXCLUDE = {r.strip() for r in os.environ.get("HUMAN_REVIEW_EXCLUDE", "").split(",") if r.strip()}
REVIEWER_LOGIN = os.environ.get("REVIEWER_LOGIN", "martin-nef-reviewer[bot]")  # its needs_human flags count
# GitHub logins whose Approve of a flagged PR's head signs it off, like approving
# its request on the ops page. Yours; never an agent's.
HUMAN_REVIEWERS = {r.strip() for r in os.environ.get("HUMAN_REVIEWERS", GITHUB_OWNER).split(",") if r.strip()}
SYNC_EVERY = int(os.environ.get("HUMAN_REVIEW_SYNC_SECONDS", "60"))
# Every this many passes, the sync also looks for repos without the ruleset.
RULESET_EVERY = 10
# At most this many github-require-human-review requests wait at once, so the
# broker's pending slots stay free for flags.
RULESET_BATCH = 5
# repo -> its open PR numbers, as the last sync saw them. tidy keeps a
# github-human-review request while its PR may still be open: deleting it
# would make the PR read as never flagged.
OPEN_PRS = {}

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
USER_VERBS = set(githubops.USER_VERBS)
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


def requester(req):
    """Who created it, as the admission policy stamped it. Older requests, and
    any made while the policy was missing, have none and share the pool."""
    return req["spec"].get("requester", "")


def ttl(req):
    """Seconds it may wait for a decision: the requester's choice, capped."""
    return min(int(req["spec"].get("ttlSeconds") or REQUEST_TTL), MAX_TTL)


def expires(req):
    return parse_time(req["metadata"]["creationTimestamp"]) + timedelta(seconds=ttl(req))


def pending_cap(who):
    """(the pool a requester's pending requests count against, its size)."""
    return (who, PENDING_CAPS[who]) if who in PENDING_CAPS else ("", MAX_PENDING)


def quiet_window():
    """(start, end) clock times of the quiet hours, or None."""
    if not QUIET_HOURS:
        return None
    start, _, end = QUIET_HOURS.partition("-")
    return tuple(clock(*map(int, t.strip().split(":"))) for t in (start, end))


try:
    QUIET_ZONE = ZoneInfo(QUIET_TZ)
except ZoneInfoNotFoundError:  # no tzdata: better quiet at the wrong hour than never
    print(f"QUIET_TZ {QUIET_TZ!r} unknown; using UTC", flush=True)
    QUIET_ZONE = timezone.utc


def quiet_until(t=None):
    """When the quiet hours in progress at t end, or None if it isn't quiet."""
    window = quiet_window()
    if window is None:
        return None
    start, end = window
    local = (t or now()).astimezone(QUIET_ZONE)
    today_end = datetime.combine(local.date(), end, QUIET_ZONE)
    if start <= end:
        quiet = start <= local.time() < end
    else:  # over midnight
        quiet = local.time() >= start or local.time() < end
        if local.time() >= start:
            today_end += timedelta(days=1)
    return today_end.astimezone(timezone.utc) if quiet else None


def held(req, t=None):
    """Whether to save its notification for the digest: it's quiet now, and the
    request will still be waiting when it stops being quiet. One that would
    expire before then is announced now, since it's only worth anything now."""
    until = quiet_until(t)
    return until is not None and expires(req) > until


def describe(req):
    s = req["spec"]
    if s["verb"] in GITHUB_VERBS:
        return githubops.describe(GITHUB_OWNER, s)
    t = s["target"]
    return f"{s['verb']} {t['kind']} {t['namespace']}/{t['name']}"


def audit(event, req, **fields):
    print(json.dumps({"time": stamp(), "event": event, "request": req["metadata"]["name"],
                      "action": describe(req), "requester": requester(req), **fields}), flush=True)


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
        if spec.get("verb") == "github-require-human-review":
            repo = spec["github"].get("repo")
            if repo in HUMAN_REVIEW_EXCLUDE:
                # Its PRs get no status, so a required check would hold them all.
                return f"{repo} is in HUMAN_REVIEW_EXCLUDE: the broker sets no human-review status there"
        return githubops.check(MINTER, spec)
    except githubops.GitHubError as e:
        if e.transient:
            raise
        return f"GitHub: {e}"


def run(req, code=None):
    """Run the action. Returns a message for the status. Raises K8sError when
    the API server refused it, Unknown when it may have taken effect."""
    try:
        return act(req["spec"], req["status"].get("targetUID"), code)
    except (K8sError, githubops.GitHubError) as e:
        if e.transient:
            raise Unknown(str(e)) from None
        raise
    except (OSError, json.JSONDecodeError) as e:  # timeouts, resets: it may have arrived
        raise Unknown(repr(e)) from None


def act(spec, uid, code=None):
    if spec["verb"] in GITHUB_VERBS:
        return githubops.act(MINTER, spec, code)
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


# human review

def human_review_requests(items, repo):
    """{pr number: its github-human-review requests, oldest first}."""
    out = {}
    for r in sorted(items, key=lambda r: r["metadata"]["creationTimestamp"]):
        g = r["spec"].get("github") or {}
        if r["spec"].get("verb") == "github-human-review" and g.get("repo") == repo:
            out.setdefault(int(g["pr"]), []).append(r)
    return out


def human_review_state(head, reqs):
    """(status state, description, whether a request covers this head) for a PR
    head, from its human-review requests, oldest first. The newest decides:
    only your sign-off of exactly this head clears it."""
    if not reqs:
        return "success", "not flagged", False
    last = reqs[-1]
    p, sha, reason = phase(last), last["spec"]["github"]["headSHA"], last["spec"]["reason"]
    if sha != head:
        return "pending", "new commits since it was flagged: flag it again", False
    if p == "Succeeded":
        return "success", f"signed off by {last['status'].get('decidedBy', 'a human')}", True
    if p == "Denied":
        return "pending", f"denied by {last['status'].get('decidedBy', 'a human')}: change it and flag it again", True
    if p in ("", "Pending", "Approved"):
        return "pending", f"human review required: {reason}", True
    if p == "Expired":
        # Not covered any more, so the reviewer's flag, if it still stands, is filed again.
        return "pending", "flag expired without a sign-off: flag it again", False
    # Rejected, Failed, Unknown: nobody signed it off. Not filed again by itself,
    # so a request that keeps being rejected (a full queue) can't loop.
    return "pending", f"flag {p.lower()} without a sign-off: flag it again", True


def file_human_review(repo, pr, head, reason):
    """File a request for the reviewer's flag. The admission policy stamps this
    broker as the requester; the reason says whose flag it is."""
    k8s("POST", REQUESTS, {
        "apiVersion": "ops.nefcloud.top/v1alpha1", "kind": "OpsRequest",
        "metadata": {"generateName": "github-human-review-", "namespace": NAMESPACE},
        "spec": {"verb": "github-human-review", "github": {"repo": repo, "pr": pr, "headSHA": head},
                 "reason": f"{REVIEWER_LOGIN}: {reason}"[:2000], "ttlSeconds": MAX_TTL},
    }, content_type="application/json")
    print(json.dumps({"time": stamp(), "event": "filed", "action": f"github-human-review {GITHUB_OWNER}/{repo}#{pr}",
                      "reason": reason}), flush=True)


def signed_off_on_github(repo, pr, head, login):
    """You approved a flagged PR's head on GitHub: its waiting request is
    signed off too, so the agent that asked sees Succeeded, as if you'd
    approved it on the ops page."""
    with lock:
        for req in human_review_requests(list_requests(), repo).get(pr, []):
            if req["spec"]["github"].get("headSHA") != head or phase(req) not in ("", "Pending"):
                continue
            try:
                set_status(req, phase="Succeeded", decidedBy=f"{login} (GitHub review)", decidedAt=stamp(),
                           finishedAt=stamp(), message=f"signed off by approving the PR on GitHub as {login}")
            except K8sError as e:
                print(f"recording {req['metadata']['name']} as signed off on GitHub: {e}", flush=True)
                continue
            audit("succeeded", req, by=f"{login} (GitHub review)")


def file_require(repo):
    k8s("POST", REQUESTS, {
        "apiVersion": "ops.nefcloud.top/v1alpha1", "kind": "OpsRequest",
        "metadata": {"generateName": "github-require-human-review-", "namespace": NAMESPACE},
        "spec": {"verb": "github-require-human-review", "github": {"repo": repo},
                 "reason": (f"{repo} has no human-review ruleset, so a PR an agent flags there can still be "
                            "merged. Approving adds the standard one."), "ttlSeconds": MAX_TTL},
    }, content_type="application/json")
    print(json.dumps({"time": stamp(), "event": "filed",
                      "action": f"github-require-human-review {GITHUB_OWNER}/{repo}"}), flush=True)


def sync_human_review(rulesets=False):
    """One pass over every repo the ops App is installed on, but
    HUMAN_REVIEW_EXCLUDE: every open PR head gets its human-review status, and
    reviewer flags with no request yet get one. rulesets: also file a
    github-require-human-review request for repos without the ruleset."""
    if MINTER.status() is None:
        return
    items = list_requests()
    clean = True
    with MINTER.token(None, "github-human-review", "list") as tok:
        repos = [(r, n) for r, n in githubops.installed(tok) if r not in HUMAN_REVIEW_EXCLUDE]
    for repo, open_count in repos:
        if not open_count:
            OPEN_PRS[repo] = set()  # no open issues or PRs: nothing to set, and no token minted for it
            continue
        reqs = human_review_requests(items, repo)

        def recheck(pr, head, repo=repo):
            return human_review_state(head, human_review_requests(list_requests(), repo).get(pr, []))
        try:
            to_file, OPEN_PRS[repo], approved = githubops.sync(
                MINTER, repo, lambda pr, head: human_review_state(head, reqs.get(pr, [])), REVIEWER_LOGIN, recheck,
                HUMAN_REVIEWERS)
            for pr, head, reason in to_file:
                file_human_review(repo, pr, head, reason)
            for pr, head, login in approved:
                signed_off_on_github(repo, pr, head, login)
        except (githubops.GitHubError, K8sError, OSError) as e:
            clean = False
            print(f"human-review sync {repo}: {e}", flush=True)
    # Only after a pass that set every status it had to: a required check the
    # broker can't set (a missing permission) would hold every PR.
    if rulesets and clean:
        file_rulesets([r for r, _ in repos], items)


def file_rulesets(repos, items):
    """File github-require-human-review for repos without the ruleset, up to
    RULESET_BATCH waiting at once. A repo with any such request already, in
    any phase, isn't asked again until retention deletes it: a Denied one
    means no for RETENTION_DAYS (HUMAN_REVIEW_EXCLUDE says it for good)."""
    asked = {(r["spec"].get("github") or {}).get("repo"): phase(r) for r in items
             if r["spec"].get("verb") == "github-require-human-review"}
    room = RULESET_BATCH - sum(1 for p in asked.values() if p in ("", "Pending"))
    for repo in repos:
        if room <= 0:
            return
        if repo in asked:
            continue
        try:
            # Proof the App can set the check there: the mint fails without statuses: write.
            with MINTER.token(repo, "github-human-review", "write"):
                pass
            with MINTER.token(repo, "github-require-human-review", "check") as tok:
                if githubops.has_ruleset(tok):
                    continue
            file_require(repo)
            room -= 1
        except (githubops.GitHubError, K8sError, OSError) as e:
            print(f"human-review ruleset {repo}: {e}", flush=True)


def sync_loop():
    for n in itertools.count():
        try:
            sync_human_review(rulesets=n % RULESET_EVERY == 0)
        except Exception as e:
            print(f"human-review sync: {e!r}", flush=True)
        time.sleep(SYNC_EVERY)


def notify(req):
    if not NTFY_URL:
        return
    url = f"{PUBLIC_URL}/r/{req['metadata']['name']}"
    send({
        "title": f"Approve? {describe(req)}",
        "message": req["spec"]["reason"][:300],
        "priority": 4,
        "tags": ["rotating_light"],
        "click": url,
        "actions": [{"action": "view", "label": "Review", "url": url, "clear": True}],
    })


def notify_digest(reqs):
    """One notification for the requests held through the quiet hours."""
    lines = [f"{describe(r)} ({requester(r) or 'unknown requester'}), until {stamp(expires(r))}" for r in reqs]
    send({
        "title": f"{len(reqs)} ops request{'s' if len(reqs) != 1 else ''} waiting since the night",
        "message": "\n".join(lines)[:1000],
        "priority": 3,
        "tags": ["sunrise"],
        "click": f"{PUBLIC_URL}/",
        "actions": [{"action": "view", "label": "Review", "url": f"{PUBLIC_URL}/", "clear": True}],
    })


def send(body):
    if not NTFY_URL:
        return
    server, _, topic = NTFY_URL.rstrip("/").rpartition("/")
    body = {"topic": topic, **body}
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
    if p in ("", "Pending") and now() > expires(req):
        set_status(req, phase="Expired", message="nobody approved it in time", finishedAt=stamp())
        audit("expired", req)
    elif p == "Approved" and (now() - parse_time(req["status"].get("decidedAt") or stamp())).total_seconds() > STRANDED_AFTER:
        # The broker stopped while running it, before recording the outcome.
        # Running it now could be long after the decision, so it never is.
        set_status(req, phase="Unknown", finishedAt=stamp(),
                   message="the broker stopped while running it; check the target before requesting it again")
        audit("unknown", req, error="broker stopped mid-run")
    elif p in FINISHED and age(req) > 86400 * (SHORT_RETENTION_DAYS if p in SHORT_LIVED else RETENTION_DAYS):
        if req["spec"].get("verb") == "github-human-review" and not pr_closed(req):
            return  # the flag is the record that the PR needs you; it goes when the PR does
        k8s("DELETE", f"{REQUESTS}/{req['metadata']['name']}")


def pr_closed(req):
    """Whether the last sync saw this flag's repo, and its PR wasn't open. Not
    synced (yet, or the repo was excluded or uninstalled): kept, to be safe."""
    g = req["spec"].get("github") or {}
    seen = OPEN_PRS.get(g.get("repo"))
    return seen is not None and int(g.get("pr") or 0) not in seen


def admit(req, pending):
    """Check a new request and either reject it or make it Pending. pending
    counts the Pending requests per pool (pending_cap). Returns whether it is
    now Pending."""
    problem, uid = check(req)
    pool, cap = pending_cap(requester(req))
    if problem is None and pending.get(pool, 0) >= cap:
        problem = (f"{cap} requests from {pool} already wait for a decision" if pool else
                   f"{cap} requests already wait for a decision")
    if problem:
        set_status(req, phase="Rejected", message=problem, finishedAt=stamp())
        audit("rejected", req, reason=problem)
        return False
    hold = held(req)
    status = {"phase": "Pending", "specHash": spec_hash(req), "targetUID": uid, "expiresAt": stamp(expires(req)),
              "message": f"waiting for approval at {PUBLIC_URL}/r/{req['metadata']['name']}"
                         + (" (announced when the quiet hours end)" if hold else "")}
    if not hold:
        status["notifiedAt"] = stamp()
    set_status(req, **status)
    audit("pending", req, reason=req["spec"]["reason"], held=hold)
    if not hold:
        notify(req)
    return True


def digest(items):
    """Once the quiet hours are over, announce what they held, in one message.
    Each is marked first: a lost digest only costs time, a repeated one is noise."""
    if quiet_until() is not None:
        return
    waiting = [r for r in items if phase(r) == "Pending" and not r["status"].get("notifiedAt")]
    marked = []

    def mark(req):
        set_status(req, notifiedAt=stamp())
        marked.append(req)
    each(waiting, mark)
    if marked:
        audit("digest", marked[0], requests=[r["metadata"]["name"] for r in marked])
        notify_digest(marked)


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
        pending = {}
        for r in items:
            if phase(r) == "Pending":
                pool = pending_cap(requester(r))[0]
                pending[pool] = pending.get(pool, 0) + 1

        def admit_new(req):
            if phase(req) == "" and admit(req, pending):
                pool = pending_cap(requester(req))[0]
                pending[pool] = pending.get(pool, 0) + 1
        each(items, admit_new)
        digest(items)


def decidable(name, seen_hash):
    """(the request, None) if it can still be decided as seen, else (None, (http status, message))."""
    try:
        req = k8s("GET", f"{REQUESTS}/{name}")
    except K8sError as e:
        return None, ((404 if e.code == 404 else 502), str(e))
    if phase(req) != "Pending":
        return None, (409, f"it is {phase(req) or 'not yet admitted'}, not Pending")
    if now() > expires(req):
        return None, (409, "it has expired")
    # spec is immutable in the CRD; this also catches a page that went stale.
    if seen_hash != spec_hash(req) or seen_hash != req["status"].get("specHash"):
        return None, (409, "the request changed since the page was loaded")
    return req, None


def decide(name, decision, seen_hash, approver, code=None):
    """Approve (and run) or deny one request. Returns (http status, message).
    code: GitHub's OAuth code, which a USER_VERBS approval needs."""
    with lock:
        req, refused = decidable(name, seen_hash)
        if refused:
            return refused
        if decision == "approve" and req["spec"]["verb"] in USER_VERBS and not code:
            return 400, "this one needs you to sign in to GitHub; approve it from its page"
        if decision == "approve":
            # The world may have moved on since it was admitted, hours ago for a
            # night agent's request: check again, and never act on a replaced
            # target or a branch that has moved.
            try:
                problem, uid = check(req)
            except (K8sError, githubops.GitHubError) as e:
                return 502, f"couldn't check the target again ({e}); try again"
            if problem is None and uid != req["status"].get("targetUID"):
                problem = "the target was replaced since the request (its UID changed)"
            if problem:
                try:
                    set_status(req, phase="Rejected", finishedAt=stamp(),
                               message=f"not run, the target changed since the request: {problem}")
                except K8sError as e:
                    return (409 if e.code == 409 else 502), f"not recorded: {e}"
                audit("rejected", req, reason=problem, by=approver)
                return 409, f"Not run: the target changed since the request: {problem}"
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
            result = run(req, code)
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
            ("Requester", e(s.get("requester") or "unknown (made without the admission policy)")),
            ("Created", e(req["metadata"]["creationTimestamp"])),
            ("Expires", e(stamp(expires(req))))]
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
        # A USER_VERBS approval goes through GitHub's sign-in first.
        signin = '<input type=hidden name=signin value=github>' if s["verb"] in USER_VERBS else ""
        body += "".join(
            f'<form method=post><input type=hidden name=hash value="{h}">{signin if d == "approve" else ""}'
            f'<button class={d} name=decision value={d}>{d.capitalize()}</button></form>'
            for d in ("approve", "deny") if d == "deny" or can_approve)
    return body + '<p><a href="/">All requests</a></p>'


def index_page(items):
    """Every request waiting for a decision, then the 30 newest others."""
    items = sorted(items, key=lambda r: r["metadata"]["creationTimestamp"], reverse=True)
    items = [r for r in items if phase(r) == "Pending"] + [r for r in items if phase(r) != "Pending"][:30]
    rows = "".join(
        f'<tr><td><a href="/r/{e(r["metadata"]["name"])}">{e(r["metadata"]["name"])}</a></td>'
        f"<td><code>{e(describe(r))}</code></td><td>{e(requester(r).rpartition(':')[2] or '?')}</td>"
        f"<td>{e(phase(r) or 'new')}</td></tr>"
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
                "To replace it, delete the App on GitHub and the Secrets in Headlamp, roll the pod, then reload.</p>"
                + oauth_section(app) + '<p><a href="/">All requests</a></p>'), False
    return ("<h1>GitHub App</h1><p>The GitHub verbs need an App whose key only the broker's token minter holds. "
            f"The button asks GitHub to create <code>{e(manifest['name'])}</code>, private to "
            f"<code>{e(GITHUB_OWNER)}</code>, with: <code>{e(json.dumps(manifest['default_permissions']))}</code>. "
            "GitHub hands the key back to the minter, which keeps it in a Secret in its own "
            "namespace: it never reaches git, a PC, the agent, or the broker's own process.</p>"
            '<form method=post action="/github"><button class=approve>Create the App on GitHub</button></form>'
            '<p><a href="/">All requests</a></p>'), True


def oauth_section(app):
    """The OAuth client's state, and a form to add one to an App set up before
    user actions existed (new setups get it from GitHub with the key)."""
    try:
        oauth = MINTER.oauth()
    except githubops.GitHubError as err:
        return f"<p>Can't reach the token minter: {e(err)}</p>"
    if oauth is not None:
        return (f"<p>OAuth client set up (<code>{e(oauth['client_id'])}</code>), in the "
                "<code>ops-broker-github-oauth</code> Secret: approvals that need you on GitHub work.</p>")
    settings = f"https://github.com/settings/apps/{app['slug']}"
    return ("<h2>Approvals that need you on GitHub</h2><p>Creating a repo needs your own GitHub token, "
            f"so it needs the App's OAuth client too. Once, in {githubops.link(settings, 'the App settings')}: "
            f"under <i>Identifying and authorizing users</i>, add the redirect URI <code>{e(PUBLIC_URL)}/github/authorize</code>, save, then generate a "
            "client secret and paste it here. The minter checks it with GitHub and keeps it in a Secret only it "
            "can read.</p>"
            '<form method=post action="/github/oauth"><input name=client_secret autocomplete=off required>'
            "<button class=approve>Save</button></form>")


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


# Approvals waiting on GitHub's sign-in: state -> (approver, request name, spec hash, deadline).
authorizations = {}


def start_authorize(name, seen_hash, approver):
    """For a USER_VERBS approval: a link to GitHub's sign-in, which comes back
    to finish_authorize with a code. Nothing is claimed or run yet: if you
    stop on GitHub, the request stays Pending and expires. Returns (http status, page)."""
    req, refused = decidable(name, seen_hash)
    if refused:
        return refused[0], f"<p>{e(refused[1])}</p>"
    try:
        oauth = MINTER.oauth()
    except githubops.GitHubError as err:
        return 502, f"<p>Can't reach the token minter: {e(err)}</p>"
    if oauth is None:
        return 409, '<p>The App\'s OAuth client isn\'t set up. <a href="/github">Set it up</a>.</p>'
    now_ = time.time()
    for state in [s for s, a in authorizations.items() if a[3] < now_]:
        del authorizations[state]
    state = secrets.token_urlsafe(24)
    authorizations[state] = (approver, name, seen_hash, now_ + SETUP_TTL)
    url = "https://github.com/login/oauth/authorize?" + urllib.parse.urlencode({
        "client_id": oauth["client_id"], "redirect_uri": f"{PUBLIC_URL}/github/authorize", "state": state,
        "login": GITHUB_OWNER, "allow_signup": "false"})
    return 200, (f"<p>Sign in to GitHub as <code>{e(GITHUB_OWNER)}</code> to {e(describe(req))}. GitHub "
                 "sends you back here and it runs once, with your token, which is then revoked.</p>"
                 f'<p><a href="{e(url)}">Continue to GitHub</a></p>')


def finish_authorize(approver, code, state):
    """GitHub's sign-in came back: approve and run the request with the code."""
    started = authorizations.pop(state, None)
    if started is None or started[0] != approver or started[3] < time.time() or not code:
        return 400, "<p>This sign-in wasn't started from an approval by you, or took too long. Approve it again.</p>"
    _, name, seen_hash, _ = started
    status, message = decide(name, "approve", seen_hash, approver, code=code)
    return status, f'<p>{e(message)}</p><p><a href="/r/{e(name)}">Back</a></p>'


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
            if url.path == "/github/authorize":
                query = urllib.parse.parse_qs(url.query)
                status, page = finish_authorize(self.approver(), query.get("code", [""])[0],
                                                query.get("state", [""])[0])
                return self.reply(status, page, html_page=True)
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
        if self.path == "/github/oauth":
            form = self.form()
            if form is None:
                return self.reply(413, "too large")
            try:
                MINTER.oauth_setup(form.get("client_secret", [""])[0])
            except githubops.GitHubError as err:
                return self.reply(err.code if 400 <= err.code < 600 else 502, str(err))
            print(json.dumps({"time": stamp(), "event": "github-oauth-set-up", "by": approver}), flush=True)
            return self.reply(200, '<p>Done: approvals that need you on GitHub work now.</p><p><a href="/">All requests</a></p>',
                              html_page=True)
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
        form = self.form()
        if form is None:
            return self.reply(413, "too large or no length")
        decision = form.get("decision", [""])[0]
        if decision not in ("approve", "deny"):
            return self.reply(400, "decision must be approve or deny")
        if decision == "approve" and form.get("signin", [""])[0] == "github":
            return self.reply(*start_authorize(name, form.get("hash", [""])[0], approver), html_page=True)
        status, message = decide(name, decision, form.get("hash", [""])[0], approver)
        self.reply(status, f"<p>{e(message)}</p><p><a href=\"{e(self.path)}\">Back</a></p>", html_page=True)

    def form(self):
        """The posted form, or None if it has no length or is too large."""
        try:
            length = int(self.headers["Content-Length"])
        except (TypeError, ValueError):
            return None
        if not 0 <= length <= 4096:
            return None
        return urllib.parse.parse_qs(self.rfile.read(length).decode())

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
    threading.Thread(target=sync_loop, daemon=True).start()
    server = ThreadingHTTPServer(("", 8080), Handler)
    print(f"Listening on :8080; approvals at {PUBLIC_URL}; ntfy {'on' if NTFY_URL else 'off'}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
