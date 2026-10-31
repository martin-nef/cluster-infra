"""Tests for the ops broker (broker.py).

  cd nefcloud/ops-broker/broker && python3 -I -m unittest -v

Stdlib only. Nothing touches a cluster, GitHub or ntfy: the Kubernetes and
GitHub APIs are faked in memory and the HTTP handler is driven through a real
local server.
"""
import base64
import copy
import html
import http.client
import itertools
import json
import os
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
        self.secrets = {}

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
        if path == broker.SECRETS and method == "POST":
            name = body["metadata"]["name"]
            if name in self.secrets:
                raise broker.K8sError(409, "already exists")
            self.secrets[name] = {"data": {k: base64.b64encode(v.encode()).decode()
                                           for k, v in body["stringData"].items()}}
            return body
        if path.startswith(broker.SECRETS + "/") and method == "GET":
            name = path.rpartition("/")[2]
            if name not in self.secrets:
                raise broker.K8sError(404, "not found")
            return copy.deepcopy(self.secrets[name])
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

    def set_up_github(self):
        self.secrets[broker.GITHUB_SECRET] = {"data": {
            k: base64.b64encode(v.encode()).decode()
            for k, v in {"app-id": "42", "slug": "ops-app", "private-key.pem": "unused"}.items()}}


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

    def __call__(self, method, path, token=None, body=None):
        self.calls.append((method, path, body))
        if (method, path) in self.fail:
            raise self.fail.pop((method, path))
        route = path.split("?")[0]
        if route == "/users/owner/installation":
            return {"id": 9}
        if route == "/app/installations/9/access_tokens":
            self.tokens.append((body["repositories"], body["permissions"]))
            return {"token": f"tok{len(self.tokens)}"}
        if route == "/installation/token" and method == "DELETE":
            self.revoked.append(token)
            return {}
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


COMMIT = {"repo": "web", "branch": "work", "baseSHA": BASE, "message": "ci: build <arm64>",
          "files": [{"path": ".github/workflows/ci.yml", "content": "on: [push, pull_request]\njobs: {}\n"}]}


class GitHubBase(Base):
    def setUp(self):
        super().setUp()
        self.gh = FakeGitHub()
        for p in (mock.patch.object(broker.githubops, "api", self.gh),
                  mock.patch.object(broker.githubops.App, "jwt", lambda app: "jwt")):
            p.start()
            self.addCleanup(p.stop)
        self.api.set_up_github()

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
        h = self.pending("delete-pod", "Pod", "app", "web-1")
        self.api.put("Pod", "app", "web-1", uid="uid-new", ownerReferences=[{"kind": "StatefulSet", "controller": True}])
        self.assertEqual(broker.decide("r1", "approve", h, APPROVER)[0], 502)
        st = self.api.status("r1")
        self.assertEqual(st["phase"], "Failed")
        self.assertIn("replaced", st["message"])
        self.assertIn(broker.KINDS["Pod"].format(ns="app", name="web-1"), self.api.objects)

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

    def test_failure_is_recorded(self):
        h = self.pending()
        del self.api.objects[broker.KINDS["Deployment"].format(ns="app", name="web")]
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


class TestRS256(unittest.TestCase):
    @unittest.skipUnless(shutil.which("openssl"), "needs the openssl CLI to compare against")
    def test_matches_openssl(self):
        with tempfile.TemporaryDirectory() as d:
            for fmt in ("-traditional", ""):  # PKCS#1, as GitHub hands out; PKCS#8
                with self.subTest(fmt or "pkcs8"):
                    key = os.path.join(d, "key.pem")
                    subprocess.run(["openssl", "genrsa", "-out", key, "2048"], check=True, capture_output=True)
                    if fmt:
                        subprocess.run(["openssl", "rsa", fmt, "-in", key, "-out", key], check=True, capture_output=True)
                    expected = subprocess.run(["openssl", "dgst", "-sha256", "-sign", key], input=b"header.claims",
                                              check=True, capture_output=True).stdout
                    with open(key) as f:
                        self.assertEqual(broker.githubops.rs256(f.read(), b"header.claims"), expected)


class TestGitHubScan(GitHubBase):
    def test_commit_waits_with_read_only_tokens(self):
        st = self.admit_github("r1", "github-commit-workflows", **COMMIT)
        self.assertEqual(st["phase"], "Pending", st.get("message"))
        self.notify.assert_called_once()
        self.assertEqual(self.gh.writes(), [])
        self.assertEqual(self.gh.tokens, [(["web"], {"contents": "read"})])
        self.assertEqual(self.gh.revoked, ["tok1"])

    def test_not_set_up_is_rejected(self):
        self.api.secrets.clear()
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
        self.assertEqual(self.gh.tokens, [(["web"], {"contents": "write", "workflows": "write"})])
        tree, commit, ref = self.gh.writes()
        self.assertEqual(tree[2], {"base_tree": "basetree", "tree": [
            {"path": ".github/workflows/ci.yml", "mode": "100644", "type": "blob",
             "content": COMMIT["files"][0]["content"]}]})
        self.assertEqual(commit[2]["parents"], [BASE])
        self.assertEqual(ref[2], {"sha": "c" * 40, "force": False})
        self.assertEqual(len(self.gh.revoked), 2)  # the check's token and the run's

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
            ("no token", ("POST", "/app/installations/9/access_tokens"), broker.githubops.GitHubError(502, "x"), "Failed"),
        ]
        for label, call, error, expected in cases:
            with self.subTest(label):
                self.setUp()
                st = self.admit_github("r1", "github-commit-workflows", **COMMIT)
                self.gh.fail[call] = error
                broker.decide("r1", "approve", st["specHash"], APPROVER)
                self.assertEqual(self.api.status("r1")["phase"], expected)

    def test_protect(self):
        code, st = self.approve("github-protect-branch", repo="web")
        self.assertEqual((code, st["phase"]), (200, "Succeeded"))
        [(method, path, body)] = self.gh.writes()
        self.assertEqual((method, path, body), ("PUT", f"{REPO}/branches/main/protection", broker.githubops.PROTECTION))
        self.assertEqual(self.gh.tokens, [(["web"], {"administration": "write"})])

    def test_protect_never_replaces_a_rule(self):
        st = self.admit_github("r1", "github-protect-branch", repo="web")
        self.gh.protected.add("main")  # someone set one up after the request
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
        self.assertEqual(self.gh.tokens, [(["web"], {"actions": "write"})])


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
        self.api.secrets.clear()
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
        with mock.patch.object(broker.githubops, "convert", return_value=creds) as convert:
            self.assertEqual(self.request("GET", "/github/callback?code=c&state=forged")[0], 400)
            self.assertEqual(self.request("GET", f"/github/callback?code=c&state={state}",
                                          email="someone@else.test")[0], 403)
            status, body = self.request("GET", f"/github/callback?code=c&state={state}")
            self.assertEqual(status, 200, body)
            self.assertIn("installations/new", body)
            convert.assert_called_once_with("c", "owner")
            # The state is single use.
            self.assertEqual(self.request("GET", f"/github/callback?code=c&state={state}")[0], 400)
        self.assertEqual(broker.github_app().slug, "martin-nef-ops")
        self.assertEqual(self.request("POST", "/github", form={})[0], 409)

    def test_convert_refuses_an_app_it_didnt_ask_for(self):
        made = {"id": 5, "slug": "x", "pem": "KEY", "html_url": "https://github.test/apps/x",
                "owner": {"login": "Owner"}, "permissions": dict(broker.githubops.PERMISSIONS)}
        with mock.patch.object(broker.githubops, "api", return_value=made):
            self.assertEqual(broker.githubops.convert("c", "owner")["app-id"], "5")
            made["permissions"] = {**made["permissions"], "secrets": "write"}
            with self.assertRaisesRegex(ValueError, "nothing was stored"):
                broker.githubops.convert("c", "owner")
            made["permissions"], made["owner"] = broker.githubops.PERMISSIONS, {"login": "someone"}
            with self.assertRaisesRegex(ValueError, "belongs to someone"):
                broker.githubops.convert("c", "owner")


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

    def test_off_without_url(self):
        with mock.patch.object(broker, "NTFY_URL", ""), mock.patch("urllib.request.urlopen") as urlopen:
            broker.notify({})
        urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
