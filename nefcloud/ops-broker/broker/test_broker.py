"""Tests for the ops broker (broker.py).

  cd nefcloud/ops-broker/broker && python3 -I -m unittest -v

Stdlib only. Nothing touches a cluster or ntfy: the Kubernetes API is faked in
memory and the HTTP handler is driven through a real local server.
"""
import copy
import http.client
import itertools
import json
import os
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


class TestHTTP(Base):
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
