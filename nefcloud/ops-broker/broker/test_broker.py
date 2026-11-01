"""Tests for the ops broker (broker.py).

  cd nefcloud/ops-broker/broker && python3 -I -m unittest -v

Stdlib only. Nothing touches a cluster, GitHub or ntfy: the Kubernetes and
GitHub APIs are faked in memory, the HTTP handler is driven through a real
local server, and the GitHub verbs get their tokens from a real minter
(minter.py) on a Unix socket in a temporary directory. test_minter.py covers
the minter on its own.
"""
import base64
import copy
import html
import http.client
import itertools
import json
import os
import re
import shutil
import subprocess
import tempfile
import sys
import threading
import unittest
import urllib.parse
from datetime import timedelta
from http.server import ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.update(
    PUBLIC_URL="https://ops.example.test",
    ALLOWED_APPROVERS="Boss@Example.test",
    REQUEST_TTL="900",
    MAX_PENDING="2",
    RETENTION_DAYS="90",
    GITHUB_OWNER="owner",
)
import broker  # noqa: E402  (reads the environment above at import)
import minter  # noqa: E402

APPROVER = "boss@example.test"


class FakeAPI:
    """The slice of the Kubernetes API that broker.k8s() is used for."""

    def __init__(self):
        rs = [{"kind": "ReplicaSet", "controller": True}]
        self.objects = {}
        self.put("Pod", "app", "web-1", ownerReferences=rs)
        self.put("Pod", "app", "bare")
        self.put("Pod", "app", "job-1", ownerReferences=[{"kind": "Job", "controller": True}])
        self.put("Pod", "app", "loose", ownerReferences=[{"kind": "ReplicaSet"}])  # an owner, not a controller
        self.put("Deployment", "app", "web")
        self.put("Kustomization", "flux-system", "web")
        self.requests = {}
        self.calls = []
        self.versions = itertools.count(1)
        self.fail = {}  # (method, path) -> exception to raise once
        self.secrets = {}  # flux-system Secrets the broker created

    def put(self, kind, ns, name, uid=None, **meta):
        self.objects[broker.KINDS[kind].format(ns=ns, name=name)] = {
            "metadata": {"uid": uid or f"uid-{name}", **meta}, "spec": {}}

    def add(self, name, verb, kind, ns, target, age=0, **status):
        self.requests[name] = {
            "metadata": {"name": name, "resourceVersion": str(next(self.versions)),
                         "creationTimestamp": broker.stamp(broker.now() - timedelta(seconds=age))},
            "spec": {"verb": verb, "target": {"kind": kind, "namespace": ns, "name": target}, "reason": f"because {name}"},
            **({"status": status} if status else {}),
        }

    def __call__(self, method, path, body=None, content_type="application/merge-patch+json"):
        self.calls.append((method, path, body))
        if (method, path) in self.fail:
            raise self.fail.pop((method, path))
        if path == broker.REQUESTS and method == "GET":
            return {"items": copy.deepcopy(list(self.requests.values()))}
        if path.startswith(broker.REQUESTS + "/"):
            name, _, sub = path[len(broker.REQUESTS) + 1:].partition("/")
            if name not in self.requests:
                raise broker.K8sError(404, f"opsrequests {name!r} not found")
            if method == "GET":
                return copy.deepcopy(self.requests[name])
            if method == "PATCH" and sub == "status":
                req = self.requests[name]
                if body["metadata"]["resourceVersion"] != req["metadata"]["resourceVersion"]:
                    raise broker.K8sError(409, "the object has been modified")
                req.setdefault("status", {}).update(body["status"])
                req["metadata"]["resourceVersion"] = str(next(self.versions))
                return copy.deepcopy(req)
            if method == "DELETE":
                del self.requests[name]
                return {}
        if method == "POST" and path == "/api/v1/namespaces/flux-system/secrets":
            name = body["metadata"]["name"]
            if name in self.secrets:
                raise broker.K8sError(409, f'secrets "{name}" already exists')
            self.secrets[name] = copy.deepcopy(body)
            return body
        if path in self.objects:
            if method == "GET":
                return copy.deepcopy(self.objects[path])
            if method == "DELETE":
                uid = (body or {}).get("preconditions", {}).get("uid")
                if uid and uid != self.objects[path]["metadata"]["uid"]:
                    raise broker.K8sError(409, "Precondition failed: UID in precondition")
                del self.objects[path]
                return {}
            if method == "PATCH":
                return {}
        raise broker.K8sError(404, f"{path} not found")

    def status(self, name):
        return self.requests[name].get("status", {})

    def actions(self):
        """Calls that touched something other than the request queue."""
        return [(m, p, b) for m, p, b in self.calls if m != "GET" and not p.startswith(broker.REQUESTS)]


class Base(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        patches = [mock.patch.object(broker, "k8s", self.api),
                   mock.patch.object(broker, "notify", mock.Mock()),
                   mock.patch("builtins.print")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.notify = broker.notify

    def admit(self, name, *args, **kw):
        self.api.add(name, *args, **kw)
        broker.scan()
        return self.api.status(name)


BASE = "a" * 40
MOVED = "b" * 40
REPO = "/repos/owner/web"


class FakeGitHub:
    """The slice of the GitHub API that githubops.api() is used for."""

    def __init__(self):
        self.calls = []
        self.tokens = []  # (repo, permissions) of every token minted
        self.revoked = []
        self.fail = {}  # (method, path) -> exception to raise once
        self.default = "main"
        self.branches = {"main": {"protected": True, "commit": {"sha": "m" * 40}},
                         "work": {"protected": False, "commit": {"sha": BASE}}}
        self.files = {".github/workflows/ci.yml": "on: push\njobs: {}\n"}
        self.protected = set()
        self.runs = {7: {"status": "completed", "conclusion": "failure", "html_url": "https://github.test/run/7",
                         "display_title": "Fix <b>it</b>", "name": "CI", "event": "pull_request",
                         "head_branch": "work", "head_sha": BASE},
                     8: {"status": "completed", "conclusion": "success"}}
        self.workflows = {"image.yml": {"state": "active", "name": "Image", "path": ".github/workflows/image.yml"}}
        self.user_login = "Owner"
        self.oauth_error = None
        self.oauth_exchanges = []
        self.user_tokens_revoked = []
        head = "h" * 40
        self.pulls = {5: {"number": 5, "state": "open", "head": {"sha": head}, "title": "Widen <RBAC>",
                          "html_url": "https://github.test/owner/web/pull/5", "user": {"login": "agent[bot]"}}}
        self.reviews = {5: []}
        self.pr_files = {5: [{"filename": "rbac.yaml", "additions": 3, "deletions": 0}]}
        self.statuses = {}  # sha -> [status], newest first
        self.installed = [{"name": "web", "open_issues_count": 1}]  # what the ops App is installed on
        self.rulesets = []

    def __call__(self, method, path, token=None, body=None, basic=None, base=None):
        self.calls.append((method, path, body))
        if (method, path) in self.fail:
            raise self.fail.pop((method, path))
        route = path.split("?")[0]
        if route == "/login/oauth/access_token":
            self.oauth_exchanges.append(body)
            return {"error": self.oauth_error} if self.oauth_error else {"access_token": "gho_user"}
        if route == "/user":
            return {"login": self.user_login}
        if route == "/user/repos" and method == "POST":
            return {"full_name": f"owner/{body['name']}", "private": body["private"]}
        if route == "/app":
            return {"client_id": "Iv1.c"}
        if route == "/applications/Iv1.c/token" and method == "DELETE":
            if basic != ("Iv1.c", "CS"):
                raise broker.githubops.GitHubError(401, "Bad credentials")
            if body["access_token"] != "gho_user":
                raise broker.githubops.GitHubError(404, "Not Found")
            self.user_tokens_revoked.append(body["access_token"])
            return {}
        if route == "/users/owner/installation":
            return {"id": 9}
        if route == "/app/installations/9/access_tokens":
            self.tokens.append((body.get("repositories"), body["permissions"]))
            return {"token": f"tok{len(self.tokens)}"}
        if route == "/installation/token" and method == "DELETE":
            self.revoked.append(token)
            return {}
        if route == "/installation/repositories":
            n = int(dict(urllib.parse.parse_qsl(path.partition("?")[2])).get("page", 1))
            return {"repositories": copy.deepcopy(self.installed[(n - 1) * 100:n * 100])}
        if route.startswith("/repos/owner/") and route.endswith("/rulesets"):  # one list for every repo
            if method == "POST":
                self.rulesets.append({**body, "id": 77})
                return {"id": 77}
            n = int(dict(urllib.parse.parse_qsl(path.partition("?")[2])).get("page", 1))
            return copy.deepcopy(self.rulesets[(n - 1) * 100:n * 100])
        if route == REPO and method == "GET":
            return {"default_branch": self.default}
        sub = route[len(REPO):]
        if sub.startswith("/branches/"):
            name, _, rest = urllib.parse.unquote(sub[len("/branches/"):]).partition("/")
            if rest == "protection":
                if method == "PUT":
                    self.protected.add(name)
                    return {}
                if name in self.protected:
                    return {"url": "..."}
                raise broker.githubops.GitHubError(404, "Branch not protected")
            if name in self.branches:
                return copy.deepcopy(self.branches[name])
        if sub.startswith("/contents/"):
            path_ = urllib.parse.unquote(sub[len("/contents/"):])
            if path_ in self.files:
                return {"content": base64.b64encode(self.files[path_].encode()).decode()}
        if sub == f"/git/commits/{BASE}":
            return {"tree": {"sha": "basetree"}}
        if sub == "/git/trees" and method == "POST":
            return {"sha": "newtree"}
        if sub == "/git/commits" and method == "POST":
            return {"sha": "c" * 40}
        if sub == "/git/refs/heads/work" and method == "PATCH":
            self.branches["work"]["commit"]["sha"] = body["sha"]
            return {}
        query = dict(urllib.parse.parse_qsl(path.partition("?")[2]))

        def page(items):
            n, size = int(query.get("page", 1)), int(query.get("per_page", 30))
            return copy.deepcopy(items[(n - 1) * size:n * size])
        if sub == "/pulls" and method == "GET":
            return page([p for p in self.pulls.values() if p["state"] == "open"])
        if sub.startswith("/pulls/") and method == "GET":
            number, _, rest = sub[len("/pulls/"):].partition("/")
            if int(number) in self.pulls:
                found = {"": self.pulls, "reviews": self.reviews, "files": self.pr_files}[rest][int(number)]
                return page(found) if isinstance(found, list) else copy.deepcopy(found)
        if sub.startswith("/commits/") and sub.endswith("/status"):
            return {"statuses": copy.deepcopy(self.statuses.get(sub.split("/")[2], []))}
        if sub.startswith("/statuses/") and method == "POST":
            self.statuses.setdefault(sub.split("/")[2], []).insert(0, dict(body))
            return {}
        if sub.startswith("/actions/runs/"):
            run_id, _, rest = sub[len("/actions/runs/"):].partition("/")
            if int(run_id) in self.runs:
                return {} if rest else copy.deepcopy(self.runs[int(run_id)])
        if sub.startswith("/actions/workflows/"):
            name, _, rest = sub[len("/actions/workflows/"):].partition("/")
            if name in self.workflows:
                return {} if rest else copy.deepcopy(self.workflows[name])
        raise broker.githubops.GitHubError(404, f"{path} not found")

    def writes(self):
        """Calls that changed something on GitHub."""
        return [(m, p, b) for m, p, b in self.calls
                if m != "GET" and p not in ("/installation/token",) and "access_tokens" not in p]

    def for_minter(self, method, path, token=None, body=None, basic=None, base=None):
        """The same API as minter.api() sees it: its own GitHubError."""
        try:
            return self(method, path, token, body, basic, base)
        except broker.githubops.GitHubError as e:
            raise minter.GitHubError(e.code, str(e).partition(": ")[2]) from None


COMMIT = {"repo": "web", "branch": "work", "baseSHA": BASE, "message": "ci: build <arm64>",
          "files": [{"path": ".github/workflows/ci.yml", "content": "on: [push, pull_request]\njobs: {}\n"}]}


def write_key(key_dir, app_id="42", slug="ops-app"):
    """What the kubelet puts in the minter's Secret volume."""
    for k, v in {"app-id": app_id, "slug": slug, "private-key.pem": "unused"}.items():
        with open(os.path.join(key_dir, k), "w") as f:
            f.write(v)


class MinterBase(unittest.TestCase):
    """A real minter on a Unix socket, with GitHub faked."""

    def setUp(self):
        super().setUp()
        self.gh = FakeGitHub()
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        self.key_dir = os.path.join(d, "key")
        os.mkdir(self.key_dir)
        sock = os.path.join(d, "minter.sock")
        for p in (mock.patch.object(minter, "api", self.gh.for_minter),
                  mock.patch.object(minter.App, "jwt", lambda app: "jwt"),
                  mock.patch.object(minter, "KEY_DIR", self.key_dir),
                  mock.patch.object(minter, "GITHUB_OWNER", "owner"),
                  mock.patch.object(minter, "PUBLIC_URL", broker.PUBLIC_URL),
                  mock.patch.object(minter, "created", None),
                  mock.patch.object(minter, "created_oauth", None),
                  mock.patch.object(minter, "OAUTH_DIR", os.path.join(d, "oauth")),
                  mock.patch.object(minter, "audit", mock.Mock())):
            p.start()
            self.addCleanup(p.stop)
        server = minter.serve(sock)
        threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.minter = broker.githubops.Minter(sock, "owner")


class GitHubBase(MinterBase, Base):
    def setUp(self):
        super().setUp()
        for p in (mock.patch.object(broker.githubops, "api", self.gh),
                  mock.patch.object(broker, "MINTER", self.minter)):
            p.start()
            self.addCleanup(p.stop)
        write_key(self.key_dir)

    def add_github(self, name, verb, age=0, **github):
        self.api.add(name, verb, "Pod", "x", "x", age=age)
        spec = self.api.requests[name]["spec"]
        del spec["target"]
        spec["github"] = github

    def admit_github(self, name, verb, **github):
        self.add_github(name, verb, **github)
        broker.scan()
        return self.api.status(name)


class TestScan(Base):
    def test_valid_request_waits_and_notifies(self):
        st = self.admit("r1", "rollout-restart", "Deployment", "app", "web")
        self.assertEqual(st["phase"], "Pending")
        self.assertEqual(st["specHash"], broker.spec_hash(self.api.requests["r1"]))
        self.notify.assert_called_once()
        self.assertEqual(self.api.actions(), [])

    def test_missing_target_is_rejected_unannounced(self):
        st = self.admit("r1", "rollout-restart", "Deployment", "app", "nope")
        self.assertEqual(st["phase"], "Rejected")
        self.assertIn("not found", st["message"])
        self.notify.assert_not_called()

    def test_verb_kind_mismatch_is_rejected(self):
        # The CRD refuses this; the broker must too, in case the CRD is loosened.
        st = self.admit("r1", "delete-pod", "Deployment", "app", "web")
        self.assertEqual(st["phase"], "Rejected")

    def test_unknown_verb_is_rejected(self):
        st = self.admit("r1", "exec", "Pod", "app", "web-1")
        self.assertEqual(st["phase"], "Rejected")

    def test_pods_that_may_not_come_back_are_rejected(self):
        for pod in ("bare", "job-1", "loose"):
            with self.subTest(pod):
                st = self.admit(pod, "delete-pod", "Pod", "app", pod)
                self.assertEqual(st["phase"], "Rejected")
                self.assertIn("may not come back", st["message"])

    def test_pod_uid_is_recorded(self):
        st = self.admit("r1", "delete-pod", "Pod", "app", "web-1")
        self.assertEqual((st["phase"], st["targetUID"]), ("Pending", "uid-web-1"))

    def test_transient_lookup_error_is_retried(self):
        self.api.add("r1", "rollout-restart", "Deployment", "app", "web")
        self.api.fail[("GET", broker.KINDS["Deployment"].format(ns="app", name="web"))] = broker.K8sError(503, "busy")
        broker.scan()
        self.assertNotIn("phase", self.api.status("r1"))
        broker.scan()
        self.assertEqual(self.api.status("r1")["phase"], "Pending")

    def test_one_bad_request_doesnt_stop_the_scan(self):
        self.api.add("a", "rollout-restart", "Deployment", "app", "web", age=2)
        self.api.add("b", "rollout-restart", "Deployment", "app", "web", age=1)
        self.api.fail[("PATCH", f"{broker.REQUESTS}/a/status")] = broker.K8sError(409, "conflict")
        broker.scan()
        self.assertEqual(self.api.status("b")["phase"], "Pending")

    def test_pending_cap(self):
        for i in range(3):
            self.api.add(f"r{i}", "flux-reconcile", "Kustomization", "flux-system", "web", age=10 - i)
        broker.scan()
        self.assertEqual([self.api.status(f"r{i}")["phase"] for i in range(3)], ["Pending", "Pending", "Rejected"])
        self.assertEqual(self.notify.call_count, 2)

    def test_stale_new_requests_expire_before_taking_slots(self):
        # After a broker outage: three stale unadmitted requests and a fresh one.
        for i in range(3):
            self.api.add(f"stale{i}", "flux-reconcile", "Kustomization", "flux-system", "web", age=2000 + i)
        st = self.admit("fresh", "flux-reconcile", "Kustomization", "flux-system", "web")
        self.assertEqual([self.api.status(f"stale{i}")["phase"] for i in range(3)], ["Expired"] * 3)
        self.assertEqual(st["phase"], "Pending")
        self.notify.assert_called_once()

    def test_expiry_frees_a_slot(self):
        self.admit("old1", "flux-reconcile", "Kustomization", "flux-system", "web")
        self.admit("old2", "flux-reconcile", "Kustomization", "flux-system", "web")
        for n in ("old1", "old2"):
            self.api.requests[n]["metadata"]["creationTimestamp"] = broker.stamp(broker.now() - timedelta(seconds=1000))
        st = self.admit("new", "flux-reconcile", "Kustomization", "flux-system", "web")
        self.assertEqual(self.api.status("old1")["phase"], "Expired")
        self.assertEqual(st["phase"], "Pending")

    def test_stranded_approval_is_unknown_not_run(self):
        decided = broker.stamp(broker.now() - timedelta(seconds=120))
        self.api.add("r1", "delete-pod", "Pod", "app", "web-1", phase="Approved", decidedAt=decided)
        broker.scan()
        st = self.api.status("r1")
        self.assertEqual(st["phase"], "Unknown")
        self.assertIn("check the target", st["message"])
        self.assertEqual(self.api.actions(), [])

    def test_fresh_approval_is_left_to_the_pod_running_it(self):
        self.api.add("r1", "delete-pod", "Pod", "app", "web-1", phase="Approved", decidedAt=broker.stamp())
        broker.scan()
        self.assertEqual(self.api.status("r1")["phase"], "Approved")

    def test_old_finished_requests_are_deleted(self):
        self.api.add("old", "delete-pod", "Pod", "app", "web-1", age=91 * 86400, phase="Succeeded")
        self.api.add("recent", "delete-pod", "Pod", "app", "web-1", age=89 * 86400, phase="Succeeded")
        self.api.add("old-junk", "delete-pod", "Pod", "app", "web-1", age=8 * 86400, phase="Rejected")
        self.api.add("old-expired", "delete-pod", "Pod", "app", "web-1", age=8 * 86400, phase="Expired")
        self.api.add("recent-junk", "delete-pod", "Pod", "app", "web-1", age=6 * 86400, phase="Rejected")
        broker.scan()
        self.assertEqual(sorted(self.api.requests), ["recent", "recent-junk"])


class TestDecide(Base):
    def pending(self, verb="rollout-restart", kind="Deployment", ns="app", name="web"):
        self.admit("r1", verb, kind, ns, name)
        return self.api.status("r1")["specHash"]

    def test_approve_runs_once(self):
        h = self.pending()
        self.assertEqual(broker.decide("r1", "approve", h, APPROVER)[0], 200)
        st = self.api.status("r1")
        self.assertEqual((st["phase"], st["decidedBy"]), ("Succeeded", APPROVER))
        [(method, path, body)] = self.api.actions()
        self.assertEqual((method, path), ("PATCH", broker.KINDS["Deployment"].format(ns="app", name="web")))
        self.assertIn("kubectl.kubernetes.io/restartedAt", body["spec"]["template"]["metadata"]["annotations"])
        # A second press (or a replayed form) does nothing.
        self.assertEqual(broker.decide("r1", "approve", h, APPROVER)[0], 409)
        self.assertEqual(len(self.api.actions()), 1)

    def test_deny_runs_nothing(self):
        h = self.pending()
        self.assertEqual(broker.decide("r1", "deny", h, APPROVER)[0], 200)
        self.assertEqual(self.api.status("r1")["phase"], "Denied")
        self.assertEqual(self.api.actions(), [])

    def test_stale_hash_is_refused(self):
        self.pending()
        self.assertEqual(broker.decide("r1", "approve", "0" * 16, APPROVER)[0], 409)
        self.assertEqual(self.api.actions(), [])

    def test_changed_spec_is_refused(self):
        h = self.pending()
        self.api.requests["r1"]["spec"]["target"]["name"] = "other"
        self.assertEqual(broker.decide("r1", "approve", h, APPROVER)[0], 409)
        self.assertEqual(self.api.actions(), [])

    def test_expired_is_refused(self):
        h = self.pending()
        self.api.requests["r1"]["metadata"]["creationTimestamp"] = broker.stamp(broker.now() - timedelta(seconds=1000))
        self.assertEqual(broker.decide("r1", "approve", h, APPROVER)[0], 409)
        self.assertEqual(self.api.actions(), [])

    def test_unadmitted_is_refused(self):
        self.api.add("r1", "rollout-restart", "Deployment", "app", "web")
        self.assertEqual(broker.decide("r1", "approve", "", APPROVER)[0], 409)

    def test_concurrent_decision_stops_before_running(self):
        # Another broker pod decided it between this one's read and its claim.
        h = self.pending()
        real = self.api.__call__

        def racing(method, path, body=None, **kw):
            if method == "PATCH" and path.endswith("/r1/status") and body["status"].get("phase") == "Approved":
                self.api.requests["r1"]["metadata"]["resourceVersion"] = "other"
            return real(method, path, body, **kw)
        with mock.patch.object(broker, "k8s", racing):
            self.assertEqual(broker.decide("r1", "approve", h, APPROVER)[0], 409)
        self.assertEqual(self.api.actions(), [])

    def test_replaced_pod_is_not_deleted(self):
        # Seen by the check on approval: nothing is run.
        h = self.pending("delete-pod", "Pod", "app", "web-1")
        self.api.put("Pod", "app", "web-1", uid="uid-new", ownerReferences=[{"kind": "StatefulSet", "controller": True}])
        self.assertEqual(broker.decide("r1", "approve", h, APPROVER)[0], 409)
        st = self.api.status("r1")
        self.assertEqual(st["phase"], "Rejected")
        self.assertIn("replaced", st["message"])
        self.assertEqual(self.api.actions(), [])
        self.assertIn(broker.KINDS["Pod"].format(ns="app", name="web-1"), self.api.objects)

    def test_pod_replaced_after_the_check_is_not_deleted(self):
        # Replaced between that check and the delete: the UID precondition stops it.
        h = self.pending("delete-pod", "Pod", "app", "web-1")
        self.api.put("Pod", "app", "web-1", uid="uid-new", ownerReferences=[{"kind": "StatefulSet", "controller": True}])
        with mock.patch.object(broker, "check", return_value=(None, "uid-web-1")):
            self.assertEqual(broker.decide("r1", "approve", h, APPROVER)[0], 502)
        st = self.api.status("r1")
        self.assertEqual(st["phase"], "Failed")
        self.assertIn("replaced", st["message"])
        self.assertIn(broker.KINDS["Pod"].format(ns="app", name="web-1"), self.api.objects)

    def test_recreated_target_is_not_run(self):
        # A Deployment deleted and made again since the request is not the one asked about.
        h = self.pending()
        self.api.put("Deployment", "app", "web", uid="uid-again")
        self.assertEqual(broker.decide("r1", "approve", h, APPROVER)[0], 409)
        self.assertEqual(self.api.status("r1")["phase"], "Rejected")
        self.assertEqual(self.api.actions(), [])

    def test_recheck_outage_leaves_it_pending(self):
        h = self.pending()
        self.api.fail[("GET", broker.KINDS["Deployment"].format(ns="app", name="web"))] = broker.K8sError(503, "busy")
        self.assertEqual(broker.decide("r1", "approve", h, APPROVER)[0], 502)
        self.assertEqual(self.api.status("r1")["phase"], "Pending")
        self.assertEqual(broker.decide("r1", "approve", h, APPROVER)[0], 200)

    def test_ambiguous_errors_are_unknown(self):
        path = broker.KINDS["Deployment"].format(ns="app", name="web")
        for error in (broker.K8sError(504, "timeout"), TimeoutError("read timed out")):
            with self.subTest(error):
                self.setUp()
                h = self.pending()
                self.api.fail[("PATCH", path)] = error
                self.assertEqual(broker.decide("r1", "approve", h, APPROVER)[0], 502)
                self.assertEqual(self.api.status("r1")["phase"], "Unknown")

    def test_lost_result_is_unknown_not_failed(self):
        # The action ran, but recording Succeeded failed: a later scan must not
        # call it not run.
        h = self.pending()
        real = self.api.__call__

        def lose_result(method, path, body=None, **kw):
            if method == "PATCH" and path.endswith("/r1/status") and body["status"].get("phase") == "Succeeded":
                raise OSError("connection reset")
            return real(method, path, body, **kw)
        with mock.patch.object(broker, "k8s", lose_result):
            self.assertEqual(broker.decide("r1", "approve", h, APPROVER)[0], 200)
        self.assertEqual(self.api.status("r1")["phase"], "Approved")
        self.api.requests["r1"]["status"]["decidedAt"] = broker.stamp(broker.now() - timedelta(seconds=120))
        broker.scan()
        self.assertEqual(self.api.status("r1")["phase"], "Unknown")

    def test_gone_target_is_not_run(self):
        h = self.pending()
        del self.api.objects[broker.KINDS["Deployment"].format(ns="app", name="web")]
        self.assertEqual(broker.decide("r1", "approve", h, APPROVER)[0], 409)
        self.assertEqual(self.api.status("r1")["phase"], "Rejected")

    def test_failure_is_recorded(self):
        h = self.pending()
        self.api.fail[("PATCH", broker.KINDS["Deployment"].format(ns="app", name="web"))] = broker.K8sError(422, "invalid")
        self.assertEqual(broker.decide("r1", "approve", h, APPROVER)[0], 502)
        self.assertEqual(self.api.status("r1")["phase"], "Failed")

    def test_each_verb(self):
        cases = [
            ("delete-pod", "Pod", "app", "web-1", "DELETE", None),
            ("flux-reconcile", "Kustomization", "flux-system", "web", "PATCH", "reconcile.fluxcd.io/requestedAt"),
            ("flux-suspend", "Kustomization", "flux-system", "web", "PATCH", {"spec": {"suspend": True}}),
            ("flux-resume", "Kustomization", "flux-system", "web", "PATCH", {"spec": {"suspend": False}}),
        ]
        for verb, kind, ns, name, method, expect in cases:
            with self.subTest(verb):
                self.setUp()
                h = self.pending(verb, kind, ns, name)
                self.assertEqual(broker.decide("r1", "approve", h, APPROVER)[0], 200)
                [(m, p, body)] = self.api.actions()
                self.assertEqual((m, p), (method, broker.KINDS[kind].format(ns=ns, name=name)))
                if isinstance(expect, dict):
                    self.assertEqual(body, expect)
                elif expect:
                    self.assertIn(expect, body["metadata"]["annotations"])


class Served:
    """A real local server around the handler; mix in before a Base."""

    def setUp(self):
        super().setUp()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), broker.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, email=APPROVER, origin=broker.PUBLIC_URL, form=None):
        conn = http.client.HTTPConnection(*self.server.server_address)
        headers = {}
        if email:
            headers["X-Auth-Request-Email"] = email
        if origin:
            headers["Origin"] = origin
        body = None
        if form is not None:
            body = urllib.parse.urlencode(form)
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read().decode()


class TestHTTP(Served, Base):
    def test_healthz_needs_no_login(self):
        self.assertEqual(self.request("GET", "/healthz", email=None)[0], 200)

    def test_pages_need_an_approver(self):
        self.assertEqual(self.request("GET", "/", email=None)[0], 403)
        self.assertEqual(self.request("GET", "/", email="someone@else.test")[0], 403)
        self.assertEqual(self.request("GET", "/", email="BOSS@example.test")[0], 200)

    def test_index_always_lists_pending(self):
        self.admit("waiting", "rollout-restart", "Deployment", "app", "web")
        for i in range(40):
            self.api.add(f"done{i}", "rollout-restart", "Deployment", "app", "web", phase="Succeeded")
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("/r/waiting", body)
        self.assertEqual(body.count("/r/done"), 30)

    def test_page_escapes_the_reason(self):
        self.api.add("r1", "rollout-restart", "Deployment", "app", "web")
        self.api.requests["r1"]["spec"]["reason"] = "<script>alert(1)</script>"
        broker.scan()
        status, body = self.request("GET", "/r/r1")
        self.assertEqual(status, 200)
        self.assertNotIn("<script>", body)
        self.assertIn("&lt;script&gt;", body)
        self.assertIn('name=decision value=approve', body)

    def test_approve_through_the_page(self):
        self.admit("r1", "rollout-restart", "Deployment", "app", "web")
        h = self.api.status("r1")["specHash"]
        status, _ = self.request("POST", "/r/r1", form={"decision": "approve", "hash": h})
        self.assertEqual(status, 200)
        self.assertEqual(self.api.status("r1")["phase"], "Succeeded")

    def test_post_needs_approver_and_origin(self):
        self.admit("r1", "rollout-restart", "Deployment", "app", "web")
        form = {"decision": "approve", "hash": self.api.status("r1")["specHash"]}
        self.assertEqual(self.request("POST", "/r/r1", email="someone@else.test", form=form)[0], 403)
        self.assertEqual(self.request("POST", "/r/r1", origin=None, form=form)[0], 403)
        self.assertEqual(self.request("POST", "/r/r1", origin="https://evil.test", form=form)[0], 403)
        self.assertEqual(self.api.status("r1")["phase"], "Pending")
        self.assertEqual(self.api.actions(), [])

    def test_path_cannot_escape_the_queue(self):
        for path in ("/r/..%2F..%2Fsecrets", "/r/..", "/r/a/b"):
            self.assertEqual(self.request("GET", path)[0], 404, path)
            self.assertEqual(self.request("POST", path, form={"decision": "approve"})[0], 404, path)
        self.assertTrue(all(p.startswith(broker.REQUESTS + "/") for _, p, _ in self.api.calls))


class TestGitHubScan(GitHubBase):
    def test_commit_waits_with_read_only_tokens(self):
        st = self.admit_github("r1", "github-commit-workflows", **COMMIT)
        self.assertEqual(st["phase"], "Pending", st.get("message"))
        self.notify.assert_called_once()
        self.assertEqual(self.gh.writes(), [])
        self.assertEqual(self.gh.tokens, [(["web"], {"contents": "read"})])
        self.assertEqual(self.gh.revoked, ["tok1"])

    def test_not_set_up_is_rejected(self):
        for f in os.listdir(self.key_dir):
            os.remove(os.path.join(self.key_dir, f))
        st = self.admit_github("r1", "github-commit-workflows", **COMMIT)
        self.assertEqual(st["phase"], "Rejected")
        self.assertIn("/github", st["message"])

    def test_commit_rejections(self):
        cases = [
            ("default branch", dict(branch="main"), "default branch"),
            ("protected", dict(branch="release"), "protected"),
            ("moved", dict(baseSHA=MOVED), "not baseSHA"),
            ("not a workflow", dict(files=[{"path": "deploy/x.yml", "content": "x"}]), "isn't a workflow"),
            ("both", dict(files=[{"path": ".github/workflows/a.yml", "content": "x", "delete": True}]), "either"),
            ("branch escape", dict(branch="heads/../tags/v1"), "isn't a branch name"),
            ("repo escape", dict(repo=".."), "isn't a repo name"),
            ("delete missing", dict(files=[{"path": ".github/workflows/gone.yml", "delete": True}]), "doesn't exist"),
        ]
        for label, change, why in cases:
            with self.subTest(label):
                self.setUp()
                self.gh.branches["release"] = {"protected": True, "commit": {"sha": BASE}}
                st = self.admit_github("r1", "github-commit-workflows", **{**COMMIT, **change})
                self.assertEqual(st["phase"], "Rejected")
                self.assertIn(why, st["message"])
                self.assertEqual(self.gh.writes(), [])

    def test_not_installed_is_rejected(self):
        self.gh.fail[("POST", "/app/installations/9/access_tokens")] = broker.githubops.GitHubError(422, "no access")
        st = self.admit_github("r1", "github-commit-workflows", **COMMIT)
        self.assertEqual(st["phase"], "Rejected")
        self.assertIn("not installed there", st["message"])

    def test_github_outage_is_retried(self):
        self.gh.fail[("GET", REPO)] = broker.githubops.GitHubError(502, "bad gateway")
        st = self.admit_github("r1", "github-commit-workflows", **COMMIT)
        self.assertNotIn("phase", st)
        broker.scan()
        self.assertEqual(self.api.status("r1")["phase"], "Pending")

    def test_github_verb_with_a_target_is_rejected(self):
        self.add_github("r1", "github-protect-branch", repo="web")
        self.api.requests["r1"]["spec"]["target"] = {"kind": "Pod", "namespace": "x", "name": "x"}
        broker.scan()
        self.assertEqual(self.api.status("r1")["phase"], "Rejected")

    def test_other_verbs(self):
        self.gh.protected.add("main")
        cases = [
            ("github-protect-branch", dict(repo="web"), "Rejected"),
            ("github-rerun-workflow", dict(repo="web", runID=8), "Rejected"),
            ("github-rerun-workflow", dict(repo="web", runID=7), "Pending"),
            ("github-dispatch-workflow", dict(repo="web", workflow="image.yml"), "Pending"),
            ("github-dispatch-workflow", dict(repo="web", workflow="nope.yml"), "Rejected"),
        ]
        for i, (verb, github, expected) in enumerate(cases):
            with self.subTest(verb, **github):
                self.assertEqual(self.admit_github(f"r{i}", verb, **github)["phase"], expected)
                self.api.requests.clear()
        self.assertEqual(self.gh.writes(), [])


class TestGitHubDecide(GitHubBase):
    def assertWrites(self, permissions):
        """The approval re-checks with read tokens, then runs with one write token."""
        *checks, run = self.gh.tokens
        self.assertEqual(run, (["web"], permissions))
        for repos, perms in checks:
            self.assertEqual((repos, set(perms.values())), (["web"], {"read"}))

    def approve(self, verb, **github):
        st = self.admit_github("r1", verb, **github)
        self.assertEqual(st["phase"], "Pending", st.get("message"))
        self.gh.calls.clear()
        self.gh.tokens.clear()
        code, _ = broker.decide("r1", "approve", st["specHash"], APPROVER)
        return code, self.api.status("r1")

    def test_commit_lands_on_exactly_the_base(self):
        code, st = self.approve("github-commit-workflows", **COMMIT)
        self.assertEqual((code, st["phase"]), (200, "Succeeded"))
        self.assertWrites({"contents": "write", "workflows": "write"})
        tree, commit, ref = self.gh.writes()
        self.assertEqual(tree[2], {"base_tree": "basetree", "tree": [
            {"path": ".github/workflows/ci.yml", "mode": "100644", "type": "blob",
             "content": COMMIT["files"][0]["content"]}]})
        self.assertEqual(commit[2]["parents"], [BASE])
        self.assertEqual(ref[2], {"sha": "c" * 40, "force": False})
        self.assertEqual(len(self.gh.revoked), 3)  # every token: the checks' on admission and approval, and the run's

    def test_delete_is_a_null_sha(self):
        self.approve("github-commit-workflows", **{**COMMIT, "files": [{"path": ".github/workflows/ci.yml", "delete": True}]})
        self.assertEqual(self.gh.writes()[0][2]["tree"][0]["sha"], None)

    def test_commit_outcomes(self):
        ref = ("PATCH", f"{REPO}/git/refs/heads/work")
        cases = [
            ("branch moved", ref, broker.githubops.GitHubError(422, "not a fast forward"), "Failed"),
            ("ref timeout", ref, broker.githubops.GitHubError(502, "bad gateway"), "Unknown"),
            ("ref reset", ref, ConnectionResetError("reset"), "Unknown"),
            ("tree 5xx", ("POST", f"{REPO}/git/trees"), broker.githubops.GitHubError(502, "x"), "Failed"),
            # The approval's re-check can't get a token: nothing is decided, try again.
            ("no token", ("POST", "/app/installations/9/access_tokens"), broker.githubops.GitHubError(502, "x"), "Pending"),
            ("branch moved before approval", ("GET", f"{REPO}/branches/work"),
             None, "Rejected"),
        ]
        for label, call, error, expected in cases:
            with self.subTest(label):
                self.setUp()
                st = self.admit_github("r1", "github-commit-workflows", **COMMIT)
                if error is None:
                    self.gh.branches["work"]["commit"]["sha"] = MOVED
                else:
                    self.gh.fail[call] = error
                broker.decide("r1", "approve", st["specHash"], APPROVER)
                self.assertEqual(self.api.status("r1")["phase"], expected)

    def test_protect(self):
        code, st = self.approve("github-protect-branch", repo="web")
        self.assertEqual((code, st["phase"]), (200, "Succeeded"))
        [(method, path, body)] = self.gh.writes()
        self.assertEqual((method, path, body), ("PUT", f"{REPO}/branches/main/protection", broker.githubops.PROTECTION))
        self.assertWrites({"administration": "write"})

    def test_protect_never_replaces_a_rule(self):
        # Someone set one up after the request: the approval's check sees it.
        st = self.admit_github("r1", "github-protect-branch", repo="web")
        self.gh.protected.add("main")
        broker.decide("r1", "approve", st["specHash"], APPROVER)
        self.assertEqual(self.api.status("r1")["phase"], "Rejected")
        # Or after that check: the run checks again just before writing.
        self.setUp()
        st = self.admit_github("r1", "github-protect-branch", repo="web")
        self.gh.protected.add("main")
        with mock.patch.object(broker, "check", return_value=(None, None)):
            broker.decide("r1", "approve", st["specHash"], APPROVER)
        self.assertEqual(self.api.status("r1")["phase"], "Failed")
        self.assertEqual([w for w in self.gh.writes() if w[0] == "PUT"], [])

    def test_rerun_and_dispatch(self):
        code, st = self.approve("github-rerun-workflow", repo="web", runID=7)
        self.assertEqual(st["phase"], "Succeeded")
        self.assertEqual(self.gh.writes(), [("POST", f"{REPO}/actions/runs/7/rerun-failed-jobs", None)])
        self.setUp()
        code, st = self.approve("github-dispatch-workflow", repo="web", workflow="image.yml", inputs={"tag": "1.2"})
        self.assertEqual(st["phase"], "Succeeded")
        self.assertEqual(self.gh.writes(), [("POST", f"{REPO}/actions/workflows/image.yml/dispatches",
                                             {"ref": "main", "inputs": {"tag": "1.2"}})])
        self.assertWrites({"actions": "write"})


class TestGitHubHTTP(Served, GitHubBase):
    def test_page_shows_the_escaped_diff(self):
        self.admit_github("r1", "github-commit-workflows", **COMMIT)
        status, body = self.request("GET", "/r/r1")
        self.assertEqual(status, 200)
        self.assertIn("<span class=del>-on: push\n</span>", body)
        self.assertIn("<span class=add>+on: [push, pull_request]\n</span>", body)
        self.assertIn("ci: build &lt;arm64&gt;", body)
        self.assertIn("with this repo's secrets", body)
        self.assertIn("value=approve", body)

    def test_no_approve_without_details(self):
        self.admit_github("r1", "github-rerun-workflow", repo="web", runID=7)
        self.gh.fail[("GET", f"{REPO}/actions/runs/7")] = broker.githubops.GitHubError(502, "x")
        status, body = self.request("GET", "/r/r1")
        self.assertIn("can't be approved", body)
        self.assertNotIn("value=approve", body)
        self.assertIn("value=deny", body)
        status, body = self.request("GET", "/r/r1")
        self.assertIn("Fix &lt;b&gt;it&lt;/b&gt;", body)

    def test_setup_creates_the_secret(self):
        for f in os.listdir(self.key_dir):
            os.remove(os.path.join(self.key_dir, f))
        status, body = self.request("GET", "/github")
        self.assertIn("Create the App", body)
        self.assertEqual(self.request("POST", "/github", origin="https://evil.test")[0], 403)
        conn = http.client.HTTPConnection(*self.server.server_address)
        conn.request("POST", "/github", headers={"X-Auth-Request-Email": APPROVER, "Origin": broker.PUBLIC_URL,
                                                 "Content-Length": "0"})
        resp = conn.getresponse()
        page = resp.read().decode()
        self.assertIn("form-action 'self' https://github.com", resp.getheader("Content-Security-Policy"))
        state = urllib.parse.unquote(page.split("state=")[1].split('"')[0])
        manifest = json.loads(html.unescape(page.split('name=manifest value="')[1].split('"')[0]))
        self.assertEqual(manifest["redirect_url"], "https://ops.example.test/github/callback")
        self.assertFalse(manifest["public"])

        creds = {"app-id": "5", "slug": "martin-nef-ops", "private-key.pem": "KEY"}
        client = {"client-id": "Iv1.c", "client-secret": "CS"}
        with mock.patch.object(minter, "convert", return_value=(creds, client)) as convert, \
                mock.patch.object(minter, "store_secret") as store:
            self.assertEqual(self.request("GET", "/github/callback?code=c&state=forged")[0], 400)
            self.assertEqual(self.request("GET", f"/github/callback?code=c&state={state}",
                                          email="someone@else.test")[0], 403)
            status, body = self.request("GET", f"/github/callback?code=c&state={state}")
            self.assertEqual(status, 200, body)
            self.assertIn("installations/new", body)
            # The minter got the code and stored the key; the broker never saw it.
            convert.assert_called_once_with("c", "owner")
            self.assertEqual(store.call_args_list, [mock.call(creds), mock.call(client, minter.OAUTH_SECRET)])
            self.assertNotIn("KEY", body)
            # The state is single use.
            self.assertEqual(self.request("GET", f"/github/callback?code=c&state={state}")[0], 400)
        # Before the kubelet fills the Secret volume in, the minter uses the key it holds.
        self.assertEqual(self.minter.status(), {"app_id": "5", "slug": "martin-nef-ops"})
        self.assertEqual(self.request("POST", "/github", form={})[0], 409)

    def test_minter_down(self):
        broker.MINTER.path = "/nonexistent/minter.sock"
        st = self.admit_github("r1", "github-commit-workflows", **COMMIT)
        self.assertNotIn("phase", st)  # left for the next scan, not rejected
        self.assertIn("token minter", self.request("GET", "/github")[1])
        self.add_github("r2", "github-rerun-workflow", repo="web", runID=7)
        self.api.requests["r2"]["status"] = {"phase": "Pending", "specHash": broker.spec_hash(self.api.requests["r2"])}
        # Without the minter, the approval can't check it again: nothing is decided.
        self.assertEqual(broker.decide("r2", "approve", self.api.status("r2")["specHash"], APPROVER)[0], 502)
        self.assertEqual(self.api.status("r2")["phase"], "Pending")
        self.assertEqual(self.gh.writes(), [])

    def test_broker_never_reads_secrets(self):
        st = self.admit_github("r1", "github-commit-workflows", **COMMIT)
        broker.decide("r1", "approve", st["specHash"], APPROVER)
        self.request("GET", "/github")
        self.assertEqual([c for c in self.api.calls if "/secrets" in c[1]], [])


class TestCreateRepo(Served, GitHubBase):
    """github-create-repo: approved through GitHub's sign-in, run with the owner's token."""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(minter, "created_oauth", ("Iv1.c", "CS"))
        p.start()
        self.addCleanup(p.stop)

    def pending(self):
        st = self.admit_github("r1", "github-create-repo", repo="new", description="<b>x</b>")
        self.assertEqual(st["phase"], "Pending", st.get("message"))
        return st

    def sign_in(self, st):
        """Press Approve; returns the state in the GitHub link."""
        status, page = self.request("POST", "/r/r1", form={"decision": "approve", "hash": st["specHash"],
                                                             "signin": "github"})
        self.assertEqual(status, 200, page)
        url = urllib.parse.urlsplit(html.unescape(page.split('href="')[1].split('"')[0]))
        self.assertEqual(url.netloc + url.path, "github.com/login/oauth/authorize")
        query = urllib.parse.parse_qs(url.query)
        self.assertEqual(query["client_id"], ["Iv1.c"])
        self.assertEqual(query["redirect_uri"], ["https://ops.example.test/github/authorize"])
        return query["state"][0]

    def test_check_uses_an_unscoped_metadata_token(self):
        self.pending()
        self.assertEqual(self.gh.tokens, [(None, {"metadata": "read"})])
        st = self.admit_github("r2", "github-create-repo", repo="web")
        self.assertIn("already exists", st["message"])
        self.assertEqual(st["phase"], "Rejected")

    def test_not_set_up_is_rejected(self):
        with mock.patch.object(minter, "created_oauth", None):
            st = self.admit_github("r1", "github-create-repo", repo="new")
        self.assertEqual(st["phase"], "Rejected")
        self.assertIn("OAuth client", st["message"])

    def test_approve_goes_through_github(self):
        st = self.pending()
        status, page = self.request("GET", "/r/r1")
        self.assertIn("a private</b> repo <code>owner/new</code>", page)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", page)
        self.assertIn("name=signin value=github", page)
        state = self.sign_in(st)
        self.assertEqual(self.api.status("r1")["phase"], "Pending")  # nothing claimed until GitHub returns
        self.assertEqual(self.request("GET", f"/github/authorize?code=c&state={state}", email="x@evil.test")[0], 403)
        status, page = self.request("GET", f"/github/authorize?code=c&state={state}")
        self.assertEqual(status, 200, page)
        st = self.api.status("r1")
        self.assertEqual((st["phase"], st["message"], st["decidedBy"]), ("Succeeded", "created owner/new (private)", APPROVER))
        self.assertEqual(self.gh.user_tokens_revoked, ["gho_user"])
        # Single use.
        self.assertEqual(self.request("GET", f"/github/authorize?code=c&state={state}")[0], 400)

    def test_forged_state_runs_nothing(self):
        self.sign_in(self.pending())
        self.assertEqual(self.request("GET", "/github/authorize?code=c&state=forged")[0], 400)
        self.assertEqual(self.api.status("r1")["phase"], "Pending")
        self.assertEqual(self.gh.oauth_exchanges, [])

    def test_no_approval_without_a_code(self):
        st = self.pending()
        code, message = broker.decide("r1", "approve", st["specHash"], APPROVER)
        self.assertEqual(code, 400)
        self.assertEqual(self.api.status("r1")["phase"], "Pending")
        # The plain approve form doesn't run it either.
        status, _ = self.request("POST", "/r/r1", form={"decision": "approve", "hash": st["specHash"]})
        self.assertEqual(self.api.status("r1")["phase"], "Pending")

    def test_wrong_account_fails_without_creating(self):
        state = self.sign_in(self.pending())
        self.gh.user_login = "mallory"
        self.request("GET", f"/github/authorize?code=c&state={state}")
        self.assertEqual(self.api.status("r1")["phase"], "Failed")
        self.assertEqual([c for c in self.gh.calls if c[1] == "/user/repos"], [])

    def test_ambiguous_creation_is_unknown(self):
        state = self.sign_in(self.pending())
        self.gh.fail[("POST", "/user/repos")] = broker.githubops.GitHubError(502, "bad gateway")
        self.request("GET", f"/github/authorize?code=c&state={state}")
        self.assertEqual(self.api.status("r1")["phase"], "Unknown")

    def test_oauth_setup_page(self):
        with mock.patch.object(minter, "created_oauth", None), mock.patch.object(minter, "store_secret") as store:
            self.assertIn("client_secret", self.request("GET", "/github")[1])
            self.assertEqual(self.request("POST", "/github/oauth", form={"client_secret": "CS"},
                                          origin="https://evil.test")[0], 403)
            self.assertEqual(self.request("POST", "/github/oauth", form={"client_secret": "nope"})[0], 400)
            status, page = self.request("POST", "/github/oauth", form={"client_secret": "CS"})
            self.assertEqual(status, 200, page)
            store.assert_called_once()
        self.assertNotIn("CS", self.request("GET", "/github")[1])


AGENT = "system:serviceaccount:claude-remote:claude-remote"
NIGHT = "system:serviceaccount:night-shift:night-worker"


class TestAsync(Base):
    """Requests from agents that run while you sleep."""

    def setUp(self):
        super().setUp()
        for p in (mock.patch.object(broker, "PENDING_CAPS", {AGENT: 2, NIGHT: 3}),
                  mock.patch.object(broker, "MAX_TTL", 43200),
                  mock.patch.object(broker, "QUIET_HOURS", ""),
                  mock.patch.object(broker, "notify_digest", mock.Mock())):
            p.start()
            self.addCleanup(p.stop)

    def add(self, name, who=None, ttl=None, age=0):
        self.api.add(name, "flux-reconcile", "Kustomization", "flux-system", "web", age=age)
        spec = self.api.requests[name]["spec"]
        if who:
            spec["requester"] = who
        if ttl:
            spec["ttlSeconds"] = ttl

    def test_requesters_have_their_own_slots(self):
        for i in range(4):
            self.add(f"n{i}", NIGHT, age=100 - i)
        for i in range(3):
            self.add(f"a{i}", AGENT, age=50 - i)
        for i in range(3):
            self.add(f"x{i}", age=20 - i)  # no requester: the shared pool (MAX_PENDING=2)
        broker.scan()
        phases = {n: self.api.status(n)["phase"] for n in self.api.requests}
        self.assertEqual([phases[f"n{i}"] for i in range(4)], ["Pending"] * 3 + ["Rejected"])
        self.assertEqual([phases[f"a{i}"] for i in range(3)], ["Pending"] * 2 + ["Rejected"])
        self.assertEqual([phases[f"x{i}"] for i in range(3)], ["Pending"] * 2 + ["Rejected"])
        self.assertIn(NIGHT, self.api.status("n3")["message"])

    def test_ttl_is_the_requesters_choice_capped(self):
        self.add("long", NIGHT, ttl=8 * 3600, age=3600)
        self.add("huge", NIGHT, ttl=86400, age=13 * 3600)
        self.add("default", AGENT, age=1000)
        broker.scan()
        self.assertEqual(self.api.status("long")["phase"], "Pending")
        self.assertEqual(self.api.status("long")["expiresAt"], broker.stamp(broker.expires(self.api.requests["long"])))
        self.assertEqual(self.api.status("huge")["phase"], "Expired")  # past MAX_TTL (12 h)
        self.assertEqual(self.api.status("default")["phase"], "Expired")  # past REQUEST_TTL

    def test_long_ttl_can_be_approved_late(self):
        self.add("r1", NIGHT, ttl=8 * 3600)
        broker.scan()
        h = self.api.status("r1")["specHash"]
        self.api.requests["r1"]["metadata"]["creationTimestamp"] = broker.stamp(broker.now() - timedelta(hours=7))
        self.assertEqual(broker.decide("r1", "approve", h, APPROVER)[0], 200)
        self.assertEqual(self.api.status("r1")["phase"], "Succeeded")

    def test_quiet_hours(self):
        # 23:00-07:00 UTC; it is 02:00.
        at = broker.parse_time("2026-11-01T02:00:00Z")
        with mock.patch.object(broker, "QUIET_HOURS", "23:00-07:00"), mock.patch.object(broker, "QUIET_ZONE", broker.timezone.utc):
            self.assertEqual(broker.quiet_until(at), broker.parse_time("2026-11-01T07:00:00Z"))
            self.assertEqual(broker.quiet_until(broker.parse_time("2026-11-01T23:30:00Z")),
                             broker.parse_time("2026-11-02T07:00:00Z"))
            self.assertIsNone(broker.quiet_until(broker.parse_time("2026-11-01T12:00:00Z")))
            with mock.patch.object(broker, "now", return_value=at):
                self.add("night", NIGHT, ttl=8 * 3600)
                self.add("urgent", AGENT)  # 15 minutes: worth nothing by 07:00
                self.api.requests["night"]["metadata"]["creationTimestamp"] = broker.stamp(at)
                self.api.requests["urgent"]["metadata"]["creationTimestamp"] = broker.stamp(at)
                broker.scan()
            self.assertEqual(self.api.status("night")["phase"], "Pending")
            self.assertNotIn("notifiedAt", self.api.status("night"))
            self.assertIn("notifiedAt", self.api.status("urgent"))
            self.notify.assert_called_once()
            self.assertEqual(self.notify.call_args[0][0]["metadata"]["name"], "urgent")
            broker.notify_digest.assert_not_called()
            # Morning: one digest for what the night held, once.
            with mock.patch.object(broker, "now", return_value=broker.parse_time("2026-11-01T07:01:00Z")):
                broker.scan()
                broker.scan()
            broker.notify_digest.assert_called_once()
            self.assertEqual([r["metadata"]["name"] for r in broker.notify_digest.call_args[0][0]], ["night"])
            self.assertIn("notifiedAt", self.api.status("night"))

    def test_quiet_hours_in_a_zone(self):
        zone = broker.ZoneInfo("Europe/London")
        with mock.patch.object(broker, "QUIET_HOURS", "22:00-07:00"), mock.patch.object(broker, "QUIET_ZONE", zone):
            # British summer time: 07:00 in London is 06:00 UTC.
            self.assertEqual(broker.quiet_until(broker.parse_time("2026-07-01T01:00:00Z")),
                             broker.parse_time("2026-07-01T06:00:00Z"))
            self.assertIsNone(broker.quiet_until(broker.parse_time("2026-07-01T20:59:00Z")))

    def test_page_shows_requester_and_expiry(self):
        self.add("r1", NIGHT, ttl=3600)
        broker.scan()
        page = broker.request_page(self.api.requests["r1"])
        self.assertIn(NIGHT, page)
        self.assertIn(self.api.status("r1")["expiresAt"], page)
        self.assertIn("night-worker", broker.index_page(list(self.api.requests.values())))

    def test_audit_names_the_requester(self):
        self.add("r1", NIGHT)
        broker.scan()
        lines = [json.loads(c.args[0]) for c in print.call_args_list if c.args and c.args[0].startswith("{")]
        self.assertEqual({l["requester"] for l in lines}, {NIGHT})


class TestNotify(unittest.TestCase):
    def test_posts_a_link_not_a_decision(self):
        req = {"metadata": {"name": "r1"},
               "spec": {"verb": "flux-resume", "target": {"kind": "Kustomization", "namespace": "flux-system", "name": "web"},
                        "reason": "x"}}
        with mock.patch.object(broker, "NTFY_URL", "https://ntfy.example.test/topic123"), \
                mock.patch("urllib.request.urlopen") as urlopen:
            broker.notify(req)
        sent = urlopen.call_args[0][0]
        self.assertEqual(sent.full_url, "https://ntfy.example.test")
        body = json.loads(sent.data)
        self.assertEqual(body["topic"], "topic123")
        self.assertEqual([a["action"] for a in body["actions"]], ["view"])
        self.assertEqual(body["click"], "https://ops.example.test/r/r1")

    def test_digest_links_to_the_list(self):
        req = {"metadata": {"name": "r1", "creationTimestamp": "2026-11-01T02:00:00Z"},
               "spec": {"verb": "flux-resume", "requester": "someone",
                        "target": {"kind": "Kustomization", "namespace": "flux-system", "name": "web"}, "reason": "x"}}
        with mock.patch.object(broker, "NTFY_URL", "https://ntfy.example.test/topic123"), \
                mock.patch("urllib.request.urlopen") as urlopen:
            broker.notify_digest([req, req])
        body = json.loads(urlopen.call_args[0][0].data)
        self.assertEqual(body["click"], "https://ops.example.test/")
        self.assertIn("2 ops requests", body["title"])
        self.assertIn("someone", body["message"])

    def test_off_without_url(self):
        with mock.patch.object(broker, "NTFY_URL", ""), mock.patch("urllib.request.urlopen") as urlopen:
            broker.notify({})
        urlopen.assert_not_called()


HEAD = "h" * 40
REVIEWER = "martin-nef-reviewer[bot]"


def review(login, commit, state=None, at="2026-11-01T01:00:00Z"):
    mark = f"\n\n<!-- pr-reviewer-state {json.dumps(state)} -->" if state is not None else ""
    return {"user": {"login": login}, "commit_id": commit, "submitted_at": at, "body": "Review." + mark}


class TestHumanReview(Served, GitHubBase):
    def setUp(self):
        super().setUp()
        for p in (mock.patch.object(broker, "HUMAN_REVIEW_EXCLUDE", set()),
                  mock.patch.object(broker, "REVIEWER_LOGIN", REVIEWER)):
            p.start()
            self.addCleanup(p.stop)

    def status(self, sha=HEAD):
        mine = [s for s in self.gh.statuses.get(sha, []) if s["context"] == "human-review"]
        return mine[0]["state"] if mine else None

    def flag(self, name="hr1", head=HEAD, **status):
        self.add_github(name, "github-human-review", repo="web", pr=5, headSHA=head)
        if status:
            self.api.requests[name]["status"] = status

    def test_unflagged_prs_get_success_and_writes_are_skipped_when_unchanged(self):
        broker.sync_human_review()
        self.assertEqual(self.status(), "success")
        writes = len(self.gh.writes())
        broker.sync_human_review()
        self.assertEqual(len(self.gh.writes()), writes)  # same status: no second write

    def test_a_flag_holds_the_pr_until_signed_off(self):
        self.flag()
        broker.scan()  # admitted: Pending
        self.assertEqual(self.api.status("hr1")["phase"], "Pending")
        broker.sync_human_review()
        self.assertEqual(self.status(), "pending")
        self.assertIn("human review required: because hr1", self.gh.statuses[HEAD][0]["description"])
        code, _ = broker.decide("hr1", "approve", self.api.status("hr1")["specHash"], APPROVER)
        self.assertEqual((code, self.api.status("hr1")["phase"]), (200, "Succeeded"))
        self.assertEqual(self.status(), "success")  # set by the approval itself
        broker.sync_human_review()
        self.assertEqual(self.status(), "success")

    def test_tokens(self):
        self.flag()
        broker.scan()
        broker.sync_human_review()
        perms = [p for _, p in self.gh.tokens]
        self.assertIn({"pull_requests": "read"}, perms)  # the scan's check
        self.assertIn({"pull_requests": "read", "statuses": "read"}, perms)  # sync
        self.assertIn({"statuses": "write"}, perms)  # sync, to set the status
        self.assertTrue(all("contents" not in p and "administration" not in p for p in perms))

    def test_states(self):
        def state(**status):
            r = {"metadata": {"creationTimestamp": broker.stamp()},
                 "spec": {"github": {"headSHA": status.pop("head", HEAD)}, "reason": "RBAC"}, "status": status}
            return broker.human_review_state(HEAD, [r])
        self.assertEqual(broker.human_review_state(HEAD, []), ("success", "not flagged", False))
        self.assertEqual(state(phase="Pending")[0::2], ("pending", True))
        self.assertEqual(state(phase="Succeeded", decidedBy="boss"), ("success", "signed off by boss", True))
        self.assertEqual(state(phase="Succeeded", head="o" * 40)[0], "pending")  # pushed since
        self.assertEqual(state(phase="Denied")[0], "pending")
        self.assertEqual(state(phase="Expired")[0::2], ("pending", False))  # the reviewer's flag is filed again
        self.assertEqual(state(phase="Rejected")[0::2], ("pending", True))  # not refiled: no loop

    def test_a_push_after_the_sign_off_needs_a_new_one(self):
        self.flag(phase="Succeeded", decidedBy=APPROVER)
        self.gh.pulls[5]["head"]["sha"] = "n" * 40
        broker.sync_human_review()
        self.assertEqual(self.status("n" * 40), "pending")

    def test_approving_a_moved_head_is_refused(self):
        self.flag()
        broker.scan()
        self.gh.pulls[5]["head"]["sha"] = "n" * 40
        code, msg = broker.decide("hr1", "approve", self.api.status("hr1")["specHash"], APPROVER)
        self.assertEqual((code, self.api.status("hr1")["phase"]), (409, "Rejected"))
        self.assertIn("flag it again", msg)
        self.assertIsNone(self.status("n" * 40))  # nothing signed off

    def test_the_reviewers_flag_is_filed_once(self):
        self.gh.reviews[5] = [review(REVIEWER, HEAD, {"critical": {}, "needs_human": "it widens RBAC"})]
        created = []

        def k8s(method, path, body=None, content_type=None):
            if method == "POST" and path == broker.REQUESTS:
                created.append(body)
                self.api.requests[f"hr{len(created)}"] = {
                    "metadata": {"name": f"hr{len(created)}", "resourceVersion": "1",
                                 "creationTimestamp": broker.stamp()}, "spec": body["spec"]}
                return body
            return self.api(method, path, body)
        with mock.patch.object(broker, "k8s", k8s):
            broker.sync_human_review()
            broker.sync_human_review()
        self.assertEqual(len(created), 1)
        spec = created[0]["spec"]
        self.assertEqual((spec["verb"], spec["github"]), ("github-human-review", {"repo": "web", "pr": 5, "headSHA": HEAD}))
        self.assertEqual(spec["reason"], f"{REVIEWER}: it widens RBAC")
        self.assertEqual(self.status(), "pending")

    def test_the_reviewers_flag_waits_until_nothing_is_required(self):
        # Flagged with required changes: held, but you aren't pinged and then pinged again.
        self.gh.reviews[5] = [review(REVIEWER, HEAD, {"required": {"bug": "Bug"}, "needs_human": "RBAC",
                                                      "ready": False})]
        created = []

        def k8s(method, path, body=None, content_type=None):
            if method == "POST" and path == broker.REQUESTS:
                created.append(body)
                self.api.requests["hr1"] = {"metadata": {"name": "hr1", "resourceVersion": "1",
                                                         "creationTimestamp": broker.stamp()}, "spec": body["spec"]}
                return body
            return self.api(method, path, body)
        with mock.patch.object(broker, "k8s", k8s):
            broker.sync_human_review()
            self.assertEqual((len(created), self.status()), (0, "pending"))
            self.assertIn("after the required changes", self.gh.statuses[HEAD][0]["description"])
            # The author fixed them; the reviewer's review of the new head says ready.
            self.gh.pulls[5]["head"]["sha"] = "n" * 40
            self.gh.reviews[5].append(review(REVIEWER, "n" * 40, {"needs_human": "RBAC", "ready": True},
                                             at="2026-11-01T05:00:00Z"))
            broker.sync_human_review()
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["spec"]["github"]["headSHA"], "n" * 40)
        self.assertEqual(self.status("n" * 40), "pending")

    def test_only_the_reviewers_own_latest_review_of_this_head_counts(self):
        forged = review("agent[bot]", HEAD, {"needs_human": "x"}, at="2026-11-01T02:00:00Z")
        stale = review(REVIEWER, "o" * 40, {"needs_human": "x"})
        self.assertIsNone(broker.githubops.reviewer_flag([forged, stale], REVIEWER, HEAD))
        newer_clean = review(REVIEWER, HEAD, {"critical": {}}, at="2026-11-01T03:00:00Z")
        flagged = review(REVIEWER, HEAD, {"needs_human": "x"})
        self.assertIsNone(broker.githubops.reviewer_flag([flagged, newer_clean], REVIEWER, HEAD))
        self.assertEqual(broker.githubops.reviewer_flag([flagged], REVIEWER, HEAD), ("x", True))
        not_yet = review(REVIEWER, HEAD, {"needs_human": "x", "ready": False})
        self.assertEqual(broker.githubops.reviewer_flag([not_yet], REVIEWER, HEAD), ("x", False))
        broken = {**flagged, "body": "Review.\n\n<!-- pr-reviewer-state {not json -->"}
        self.assertIsNone(broker.githubops.reviewer_flag([broken], REVIEWER, HEAD))

    def test_an_aged_out_flag_stays_while_its_pr_is_open(self):
        # Expired a long time ago: retention would delete it, and the PR would read as never flagged.
        self.add_github("old", "github-human-review", age=86400 * 30, repo="web", pr=5, headSHA=HEAD)
        self.api.requests["old"]["status"] = {"phase": "Expired"}
        with mock.patch.object(broker, "OPEN_PRS", {}):
            broker.scan()
            self.assertIn("old", self.api.requests)  # not synced yet: kept
            broker.sync_human_review()
            broker.scan()
            self.assertIn("old", self.api.requests)  # its PR is open: kept
            self.assertEqual(self.status(), "pending")
            self.gh.pulls[5]["state"] = "closed"
            broker.sync_human_review()
            broker.scan()
            self.assertNotIn("old", self.api.requests)  # closed: retention applies again

    def test_every_page_of_prs_and_reviews_is_read(self):
        for n in range(6, 160):
            self.gh.pulls[n] = {**copy.deepcopy(self.gh.pulls[5]), "number": n, "head": {"sha": f"{n:040d}"}}
            self.gh.reviews[n] = []
        flagged = f"{150:040d}"
        self.gh.reviews[150] = [review("x", flagged, at=f"2026-10-01T00:{i // 60:02d}:{i % 60:02d}Z")
                                for i in range(120)] + [review(REVIEWER, flagged, {"needs_human": "late"})]
        created = []
        with mock.patch.object(broker, "file_human_review", lambda *a: created.append(a)):
            broker.sync_human_review()
        self.assertEqual(self.status(f"{159:040d}"), "success")  # past the first 100 PRs
        self.assertEqual(created, [("web", 150, flagged, "late")])  # past the first 100 reviews

    def test_a_fresh_sign_off_isnt_overwritten_by_a_stale_read(self):
        self.flag()
        broker.scan()
        self.gh.statuses[HEAD] = [{"context": "human-review", "state": "success", "description": "signed off"}]
        stale = copy.deepcopy(list(self.api.requests.values()))  # read before the sign-off landed
        self.api.requests["hr1"]["status"].update(phase="Succeeded", decidedBy=APPROVER)
        real = broker.list_requests
        calls = iter([stale])
        with mock.patch.object(broker, "list_requests", lambda: next(calls, None) or real()):
            broker.sync_human_review()
        self.assertEqual(self.status(), "success")

    def test_page_shows_the_pr_escaped(self):
        self.flag()
        broker.scan()
        status, body = self.request("GET", "/r/hr1")
        self.assertEqual(status, 200)
        self.assertIn("Widen &lt;RBAC&gt;", body)
        self.assertIn("rbac.yaml", body)

    def approve_on_github(self, login="owner", commit=HEAD, state="APPROVED", at="2026-11-01T02:00:00Z"):
        self.gh.reviews[5].append({**review(login, commit, at=at), "state": state})

    def test_your_approve_on_github_signs_off_the_flag(self):
        self.flag()
        broker.scan()
        broker.sync_human_review()
        self.assertEqual(self.status(), "pending")
        self.approve_on_github()
        with mock.patch.object(broker, "HUMAN_REVIEWERS", {"Owner"}):
            broker.sync_human_review()
            st = self.api.status("hr1")
            self.assertEqual((st["phase"], st["decidedBy"]), ("Succeeded", "owner (GitHub review)"))
            self.assertEqual(self.status(), "success")
            writes = len(self.gh.writes())
            broker.sync_human_review()  # the request now says the same: nothing to write
            self.assertEqual(len(self.gh.writes()), writes)

    def test_only_a_current_approve_by_you_counts(self):
        self.flag()
        broker.scan()
        with mock.patch.object(broker, "HUMAN_REVIEWERS", {"owner"}):
            for kw in ({"login": "agent[bot]"}, {"commit": "o" * 40}, {"state": "COMMENTED"}):
                self.gh.reviews[5] = []
                self.approve_on_github(**kw)
                broker.sync_human_review()
                self.assertEqual((self.status(), self.api.status("hr1")["phase"]), ("pending", "Pending"), kw)
            # Approved, then asked for changes: the later one stands.
            self.gh.reviews[5] = []
            self.approve_on_github()
            self.approve_on_github(state="CHANGES_REQUESTED", at="2026-11-01T03:00:00Z")
            self.approve_on_github(state="COMMENTED", at="2026-11-01T04:00:00Z")  # a comment changes nothing
            broker.sync_human_review()
            self.assertEqual(self.status(), "pending")

    def test_a_reviewer_flag_you_already_approved_isnt_filed(self):
        self.gh.reviews[5] = [review(REVIEWER, HEAD, {"needs_human": "rbac"})]
        self.approve_on_github()
        created = []
        with mock.patch.object(broker, "HUMAN_REVIEWERS", {"owner"}), \
                mock.patch.object(broker, "file_human_review", lambda *a: created.append(a)):
            broker.sync_human_review()
        self.assertEqual((created, self.status()), ([], "success"))

    def test_nobody_is_asked_about_an_excluded_repo(self):
        with mock.patch.object(broker, "HUMAN_REVIEW_EXCLUDE", {"web"}):
            self.flag()
            broker.scan()
        st = self.api.status("hr1")
        self.assertEqual(st["phase"], "Rejected")
        self.assertIn("without human review", st["message"])
        self.notify.assert_not_called()

    def test_every_installed_repo_but_the_excluded_archived_and_quiet_ones(self):
        self.gh.installed += [{"name": "old", "open_issues_count": 3, "archived": True},
                              {"name": "quiet", "open_issues_count": 0},
                              {"name": "skip", "open_issues_count": 3}]
        with mock.patch.object(broker, "HUMAN_REVIEW_EXCLUDE", {"skip"}), \
                mock.patch.object(broker, "OPEN_PRS", {}) as open_prs:
            broker.sync_human_review()
            self.assertEqual(open_prs, {"web": {5}, "quiet": set()})
        self.assertEqual(self.status(), "success")
        touched = {p.split("/")[3] for _, p, _ in self.gh.calls if p.startswith("/repos/")}
        self.assertEqual(touched, {"web"})

    def test_tokens_are_per_repo_and_write_only_when_a_status_changes(self):
        broker.sync_human_review()
        self.assertEqual(self.gh.tokens, [(None, {"metadata": "read"}),
                                          (["web"], {"pull_requests": "read", "statuses": "read"}),
                                          (["web"], {"statuses": "write"})])
        self.assertEqual(len(self.gh.revoked), 3)
        self.gh.tokens.clear()
        broker.sync_human_review()  # nothing changed: no write token
        self.assertEqual([p for _, p in self.gh.tokens], [{"metadata": "read"},
                                                          {"pull_requests": "read", "statuses": "read"}])

    def test_only_the_list_token_is_for_every_repo(self):
        with self.assertRaises(broker.githubops.GitHubError) as cm:
            self.minter.token("web", "github-human-review", "list")
        self.assertEqual(cm.exception.code, 400)
        for step in ("sync", "write", "run"):
            with self.assertRaises(broker.githubops.GitHubError):
                self.minter.token(None, "github-human-review", step)

    def test_off_until_the_app_is_set_up(self):
        with mock.patch.object(broker.MINTER, "status", lambda: None):
            broker.sync_human_review(rulesets=True)
        self.assertEqual(self.gh.calls, [])


class TestRequireHumanReview(Served, GitHubBase):
    def setUp(self):
        super().setUp()
        self.gh.installed.append({"name": "cv", "open_issues_count": 0})
        for p in (mock.patch.object(broker, "HUMAN_REVIEW_EXCLUDE", set()),
                  mock.patch.object(broker, "REVIEWER_LOGIN", REVIEWER)):
            p.start()
            self.addCleanup(p.stop)

    def filed(self):
        return sorted(r["spec"]["github"]["repo"] for r in self.api.requests.values()
                      if r["spec"]["verb"] == "github-require-human-review")

    def capture(self):
        """file_require, into the fake API as the admission policy would."""
        return mock.patch.object(broker, "file_require",
                                 lambda repo: self.add_github(f"req-{repo}", "github-require-human-review", repo=repo))

    def test_repos_without_the_ruleset_are_asked_about_once(self):
        with self.capture():
            broker.sync_human_review(rulesets=True)
            self.assertEqual(self.filed(), ["cv", "web"])
            broker.sync_human_review(rulesets=True)
        self.assertEqual(self.filed(), ["cv", "web"])

    def test_not_every_pass(self):
        with self.capture():
            broker.sync_human_review()
        self.assertEqual(self.filed(), [])

    def test_a_repo_with_one_or_excluded_isnt_asked_about(self):
        self.gh.rulesets.append({"name": "human-review", "id": 1})  # the fake shares rulesets across repos
        with self.capture():
            broker.sync_human_review(rulesets=True)
        self.assertEqual(self.filed(), [])
        self.gh.rulesets.clear()
        with self.capture(), mock.patch.object(broker, "HUMAN_REVIEW_EXCLUDE", {"web", "cv"}):
            broker.sync_human_review(rulesets=True)
        self.assertEqual(self.filed(), [])

    def test_a_denied_repo_isnt_asked_again(self):
        self.add_github("no", "github-require-human-review", repo="web")
        self.api.requests["no"]["status"] = {"phase": "Denied"}
        with self.capture():
            broker.sync_human_review(rulesets=True)
        self.assertEqual(self.filed(), ["cv", "web"])  # web's is the Denied one, not a new one
        self.assertEqual(len(self.api.requests), 2)

    def test_at_most_a_batch_waits(self):
        self.gh.installed += [{"name": f"r{i}", "open_issues_count": 0} for i in range(10)]
        with self.capture():
            broker.sync_human_review(rulesets=True)
        self.assertEqual(len(self.filed()), broker.RULESET_BATCH)

    def test_nothing_filed_where_the_app_cant_set_statuses(self):
        self.gh.installed = [{"name": "cv", "open_issues_count": 0}]  # no status to write this pass
        real = self.gh.__call__

        def no_statuses(method, path, token=None, body=None, **kw):
            if "access_tokens" in path and body["permissions"] == {"statuses": "write"}:
                raise broker.githubops.GitHubError(422, "permissions not granted")
            return real(method, path, token, body, **kw)
        with self.capture(), mock.patch.object(broker.githubops, "api", no_statuses), \
                mock.patch.object(minter, "api", no_statuses):
            broker.sync_human_review(rulesets=True)
        self.assertEqual(self.filed(), [])

    def test_nothing_filed_after_a_pass_that_couldnt_set_statuses(self):
        # A required check nobody can set would hold every PR.
        self.gh.fail[("GET", f"{REPO}/commits/{HEAD}/status")] = broker.githubops.GitHubError(403, "no statuses")
        with self.capture():
            broker.sync_human_review(rulesets=True)
        self.assertEqual(self.filed(), [])

    def test_file_require_files_a_request(self):
        with mock.patch.object(broker, "k8s") as k8s:
            broker.file_require("web")
        (method, path, body), _ = k8s.call_args
        self.assertEqual((method, path, body["spec"]["verb"], body["spec"]["github"]), (
            "POST", broker.REQUESTS, "github-require-human-review", {"repo": "web"}))

    def test_approving_adds_the_standard_ruleset(self):
        st = self.admit_github("r1", "github-require-human-review", repo="web")
        self.assertEqual(st["phase"], "Pending")
        status, body = self.request("GET", "/r/r1")
        self.assertEqual(status, 200)
        self.assertIn("~DEFAULT_BRANCH", body)
        code, _ = broker.decide("r1", "approve", st["specHash"], APPROVER)
        self.assertEqual((code, self.api.status("r1")["phase"]), (200, "Succeeded"))
        (rs,) = self.gh.rulesets
        check = rs["rules"][0]["parameters"]["required_status_checks"]
        self.assertEqual(check, [{"context": "human-review", "integration_id": 42}])
        self.assertEqual(rs["bypass_actors"][0]["actor_type"], "RepositoryRole")
        self.assertIn({"administration": "write"}, [p for _, p in self.gh.tokens])

    def test_rejected_where_there_is_one_or_it_would_hold_prs_with_no_status(self):
        self.gh.rulesets.append({"name": "human-review", "id": 1})
        self.assertIn("already has", self.admit_github("r1", "github-require-human-review", repo="web")["message"])
        self.gh.rulesets.clear()
        with mock.patch.object(broker, "HUMAN_REVIEW_EXCLUDE", {"web"}):
            self.assertIn("EXCLUDE", self.admit_github("r2", "github-require-human-review", repo="web")["message"])

    def test_one_made_since_the_check_is_left_alone(self):
        st = self.admit_github("r1", "github-require-human-review", repo="web")
        self.gh.rulesets.append({"name": "human-review", "id": 1})
        broker.decide("r1", "approve", st["specHash"], APPROVER)
        self.assertEqual(len(self.gh.rulesets), 1)
        self.assertNotEqual(self.api.status("r1")["phase"], "Succeeded")


class TestSopsKey(Base):
    def add(self, name="k1", ns="flux-system", secret="night-shift-sops-age"):
        self.api.add(name, "create-sops-age-key", "Secret", ns, secret)
        broker.scan()
        return self.api.status(name)

    def test_only_an_app_key_in_flux_system(self):
        self.assertEqual(self.add()["phase"], "Pending")
        for name, ns, secret in (("k2", "ops-broker", "night-shift-sops-age"), ("k3", "flux-system", "flux-system"),
                                 ("k4", "flux-system", "night-shift-auth")):
            with self.subTest(secret=f"{ns}/{secret}"):
                self.assertEqual(self.add(name, ns, secret)["phase"], "Rejected")

    def test_approving_creates_it_and_only_the_public_key_comes_back(self):
        st = self.add()
        with mock.patch("builtins.print") as printed:
            code, msg = broker.decide("k1", "approve", st["specHash"], APPROVER)
        self.assertEqual((code, self.api.status("k1")["phase"]), (200, "Succeeded"))
        secret = self.api.secrets["night-shift-sops-age"]
        self.assertEqual(list(secret["data"]), ["age.agekey"])
        key_file = base64.b64decode(secret["data"]["age.agekey"]).decode()
        private = re.search(r"AGE-SECRET-KEY-1[0-9A-Z]+", key_file).group(0)
        public = re.search(r"public key: (age1[0-9a-z]+)", key_file).group(1)
        self.assertIn(public, self.api.status("k1")["message"])
        logged = " ".join(str(c) for c in printed.call_args_list)
        for where in (msg, self.api.status("k1")["message"], logged):
            self.assertNotIn(private, where)  # the private half is never logged or returned
        self.assertEqual(broker.agekey.recipient(_bech32_bytes(private)), public)

    def test_an_existing_key_is_never_replaced(self):
        self.api.secrets["night-shift-sops-age"] = {"data": {"age.agekey": "b2xk"}}
        st = self.add()
        code, msg = broker.decide("k1", "approve", st["specHash"], APPROVER)
        self.assertEqual(self.api.status("k1")["phase"], "Failed")
        self.assertIn("already exists; nothing changed", msg)
        self.assertEqual(self.api.secrets["night-shift-sops-age"], {"data": {"age.agekey": "b2xk"}})

    def test_the_broker_never_reads_a_secret_for_it(self):
        st = self.add()
        broker.decide("k1", "approve", st["specHash"], APPROVER)
        self.assertFalse([c for c in self.api.calls if c[0] == "GET" and "/secrets" in c[1]])


def _bech32_bytes(s):
    values = [broker.agekey.CHARSET.index(c) for c in s.lower().rpartition("1")[2][:-6]]
    acc, bits, out = 0, 0, []
    for v in values:
        acc, bits = (acc << 5) | v, bits + 5
        if bits >= 8:
            bits -= 8
            out.append((acc >> bits) & 255)
    return bytes(out)


if __name__ == "__main__":
    unittest.main()
