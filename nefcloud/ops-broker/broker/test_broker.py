"""Tests for the ops broker (broker.py).

  cd nefcloud/ops-broker/broker && python3 -I -m unittest -v

Stdlib only. Nothing touches a cluster or ntfy: the Kubernetes API is faked in
memory and the HTTP handler is driven through a real local server.
"""
import copy
import http.client
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
        self.objects = {
            broker.KINDS["Pod"].format(ns="app", name="web-1"): {"metadata": {"ownerReferences": [{"kind": "ReplicaSet"}]}},
            broker.KINDS["Pod"].format(ns="app", name="bare"): {"metadata": {}},
            broker.KINDS["Deployment"].format(ns="app", name="web"): {"metadata": {}, "spec": {}},
            broker.KINDS["Kustomization"].format(ns="flux-system", name="web"): {"metadata": {}, "spec": {}},
        }
        self.requests = {}
        self.calls = []

    def add(self, name, verb, kind, ns, target, age=0, **status):
        self.requests[name] = {
            "metadata": {"name": name, "creationTimestamp": broker.stamp(broker.now() - timedelta(seconds=age))},
            "spec": {"verb": verb, "target": {"kind": kind, "namespace": ns, "name": target}, "reason": f"because {name}"},
            **({"status": status} if status else {}),
        }

    def __call__(self, method, path, body=None, content_type="application/merge-patch+json"):
        self.calls.append((method, path, body))
        if path == broker.REQUESTS and method == "GET":
            return {"items": copy.deepcopy(list(self.requests.values()))}
        if path.startswith(broker.REQUESTS + "/"):
            name, _, sub = path[len(broker.REQUESTS) + 1:].partition("/")
            if name not in self.requests:
                raise broker.K8sError(404, f"opsrequests {name!r} not found")
            if method == "GET":
                return copy.deepcopy(self.requests[name])
            if method == "PATCH" and sub == "status":
                self.requests[name].setdefault("status", {}).update(body["status"])
                return {}
            if method == "DELETE":
                del self.requests[name]
                return {}
        if path in self.objects:
            if method == "GET":
                return copy.deepcopy(self.objects[path])
            if method == "DELETE":
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

    def test_bare_pod_is_rejected(self):
        st = self.admit("r1", "delete-pod", "Pod", "app", "bare")
        self.assertEqual(st["phase"], "Rejected")
        self.assertIn("no owner", st["message"])

    def test_pending_cap(self):
        for i in range(3):
            self.api.add(f"r{i}", "flux-reconcile", "Kustomization", "flux-system", "web", age=10 - i)
        broker.scan()
        self.assertEqual([self.api.status(f"r{i}")["phase"] for i in range(3)], ["Pending", "Pending", "Rejected"])
        self.assertEqual(self.notify.call_count, 2)

    def test_expiry_frees_a_slot(self):
        self.admit("old1", "flux-reconcile", "Kustomization", "flux-system", "web")
        self.admit("old2", "flux-reconcile", "Kustomization", "flux-system", "web")
        for n in ("old1", "old2"):
            self.api.requests[n]["metadata"]["creationTimestamp"] = broker.stamp(broker.now() - timedelta(seconds=1000))
        st = self.admit("new", "flux-reconcile", "Kustomization", "flux-system", "web")
        self.assertEqual(self.api.status("old1")["phase"], "Expired")
        self.assertEqual(st["phase"], "Pending")

    def test_approved_after_restart_is_failed_not_run(self):
        self.api.add("r1", "delete-pod", "Pod", "app", "web-1", phase="Approved")
        broker.scan()
        self.assertEqual(self.api.status("r1")["phase"], "Failed")
        self.assertEqual(self.api.actions(), [])

    def test_old_finished_requests_are_deleted(self):
        self.api.add("old", "delete-pod", "Pod", "app", "web-1", age=91 * 86400, phase="Succeeded")
        self.api.add("recent", "delete-pod", "Pod", "app", "web-1", age=89 * 86400, phase="Succeeded")
        broker.scan()
        self.assertEqual(list(self.api.requests), ["recent"])


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
