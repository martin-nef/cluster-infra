"""Tests for the scratchpad sweep (sweep.py).

  cd nefcloud/scratchpad/sweep && python3 -I -m unittest -v

Stdlib only. The Kubernetes API is faked in memory.
"""
import contextlib
import io
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sweep  # noqa: E402

NOW = datetime(2026, 11, 27, 5, 30, tzinfo=timezone.utc)


def meta(name, age, **extra):
    created = (NOW - age).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"name": name, "uid": f"uid-{name}", "creationTimestamp": created, **extra}


class FakeAPI:
    """Lists from a dict of resource -> metadata, two items a page; records DELETEs."""

    def __init__(self, objects, fail=None):
        self.objects = objects
        self.fail = fail or {}
        self.deleted = []

    def __call__(self, method, path, body=None, accept="application/json"):
        if method == "GET":
            assert accept == sweep.METADATA_ONLY
            resource, _, query = path.rsplit("/", 1)[1].partition("?")
            start = int(dict(p.split("=") for p in query.split("&")).get("continue", 0))
            items = self.objects.get(resource, [])
            more = start + 2 < len(items)
            return {
                "items": [{"metadata": m} for m in items[start:start + 2]],
                "metadata": {"continue": str(start + 2)} if more else {},
            }
        assert method == "DELETE"
        name = path.rsplit("/", 1)[1]
        if name in self.fail:
            raise sweep.K8sError(self.fail[name], "nope")
        self.deleted.append((path, body))
        return {}


def run(api, dry_run=False):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        failed = sweep.sweep(api, NOW, dry_run)
    return failed, out.getvalue(), err.getvalue()


class DueTest(unittest.TestCase):
    def test_age(self):
        self.assertFalse(sweep.due(meta("a", timedelta(hours=23)), NOW))
        self.assertTrue(sweep.due(meta("a", timedelta(hours=25)), NOW))

    def test_keep_lasts_a_week(self):
        keep = {"labels": {sweep.KEEP_LABEL: "true"}}
        self.assertFalse(sweep.due(meta("a", timedelta(days=6), **keep), NOW))
        self.assertTrue(sweep.due(meta("a", timedelta(days=8), **keep), NOW))

    def test_keep_must_be_true(self):
        other = {"labels": {sweep.KEEP_LABEL: "yes"}}
        self.assertTrue(sweep.due(meta("a", timedelta(days=2), **other), NOW))

    def test_owned_and_deleting_are_left(self):
        old = timedelta(days=30)
        self.assertFalse(sweep.due(meta("a", old, ownerReferences=[{"kind": "ReplicaSet"}]), NOW))
        self.assertFalse(sweep.due(meta("a", old, deletionTimestamp="2026-11-27T05:00:00Z"), NOW))


class SweepTest(unittest.TestCase):
    def test_deletes_only_what_is_due(self):
        api = FakeAPI({
            "deployments": [meta("old", timedelta(days=2)), meta("new", timedelta(hours=1))],
            "pods": [meta("old-pod", timedelta(days=2), ownerReferences=[{"kind": "ReplicaSet"}])],
            "configmaps": [meta("kube-root-ca.crt", timedelta(days=90)), meta("cfg", timedelta(days=2))],
        })
        failed, out, _ = run(api)
        self.assertEqual(failed, 0)
        self.assertEqual([p for p, _ in api.deleted], [
            "/apis/apps/v1/namespaces/scratchpad/deployments/old",
            "/api/v1/namespaces/scratchpad/configmaps/cfg",
        ])
        self.assertEqual(api.deleted[0][1], {"propagationPolicy": "Background", "preconditions": {"uid": "uid-old"}})
        self.assertNotIn("dryRun", api.deleted[0][1])
        self.assertIn("deleted deployments/old", out)

    def test_pages(self):
        api = FakeAPI({"secrets": [meta(f"s{i}", timedelta(days=2)) for i in range(5)]})
        run(api)
        self.assertEqual(len(api.deleted), 5)

    def test_dry_run(self):
        api = FakeAPI({"services": [meta("svc", timedelta(days=2))]})
        _, out, _ = run(api, dry_run=True)
        path, body = api.deleted[0]
        self.assertEqual(path, "/api/v1/namespaces/scratchpad/services/svc")
        self.assertEqual(body["dryRun"], ["All"])
        self.assertIn("would delete services/svc", out)

    def test_gone_is_fine_other_errors_count(self):
        old = timedelta(days=2)
        api = FakeAPI(
            {"jobs": [meta("gone", old), meta("raced", old), meta("denied", old), meta("ok", old)]},
            fail={"gone": 404, "raced": 409, "denied": 403},
        )
        failed, _, err = run(api)
        self.assertEqual(failed, 1)
        self.assertIn("failed jobs/denied", err)
        self.assertEqual([p.rsplit("/", 1)[1] for p, _ in api.deleted], ["ok"])


if __name__ == "__main__":
    unittest.main()
