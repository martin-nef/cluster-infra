"""Nightly sweep of the scratchpad namespace, so old tests don't fill the quota.

Deletes every object an agent made more than MAX_AGE ago. An object labelled
scratchpad.nefcloud.top/keep=true lasts KEEP_MAX_AGE instead. Objects with an
owner (a Deployment's ReplicaSets and pods, a CronJob's Jobs) are left to the
garbage collector, which deletes them with their owner.

  python3 -I sweep.py [--dry-run]

Stdlib only. Reads object metadata, never a Secret's data.
"""
import json
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
K8S_API = "https://kubernetes.default.svc"
NAMESPACE = "scratchpad"
KEEP_LABEL = "scratchpad.nefcloud.top/keep"
MAX_AGE = timedelta(hours=24)
KEEP_MAX_AGE = timedelta(days=7)

# What the agents' Role (../rbac.yaml) lets them create. Owners before what
# they own, so a Deployment goes before the ReplicaSets it would remake.
KINDS = [
    ("/apis/batch/v1", "cronjobs"),
    ("/apis/batch/v1", "jobs"),
    ("/apis/apps/v1", "deployments"),
    ("/apis/apps/v1", "statefulsets"),
    ("/apis/apps/v1", "replicasets"),
    ("/api/v1", "pods"),
    ("/api/v1", "services"),
    ("/api/v1", "configmaps"),
    ("/api/v1", "secrets"),
    ("/api/v1", "persistentvolumeclaims"),
]
# Made by Kubernetes, not by an agent: deleted, it comes straight back.
SKIP = {("configmaps", "kube-root-ca.crt")}

# Only names, labels and times come back, for Secrets too.
METADATA_ONLY = "application/json;as=PartialObjectMetadataList;g=meta.k8s.io;v=v1, application/json"


class K8sError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code


def k8s(method, path, body=None, accept="application/json"):
    # Re-read on every call: the projected token is rotated by the kubelet.
    with open(f"{SA_DIR}/token") as f:
        token = f.read().strip()
    req = urllib.request.Request(
        K8S_API + path,
        data=None if body is None else json.dumps(body).encode(),
        method=method,
        headers={"Authorization": f"Bearer {token}", "Accept": accept, "Content-Type": "application/json"},
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


def parse_time(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def due(meta, now):
    """Whether the sweep deletes the object with this metadata now."""
    if meta.get("ownerReferences") or meta.get("deletionTimestamp"):
        return False
    keep = (meta.get("labels") or {}).get(KEEP_LABEL) == "true"
    return now - parse_time(meta["creationTimestamp"]) > (KEEP_MAX_AGE if keep else MAX_AGE)


def listing(api, prefix, resource):
    path = f"{prefix}/namespaces/{NAMESPACE}/{resource}"
    token = ""
    while True:
        query = urllib.parse.urlencode({"limit": 500, **({"continue": token} if token else {})})
        page = api("GET", f"{path}?{query}", accept=METADATA_ONLY)
        for item in page.get("items") or []:
            yield item["metadata"]
        token = (page.get("metadata") or {}).get("continue")
        if not token:
            return


def sweep(api, now, dry_run=False):
    """Deletes what is due. Returns the number of objects it failed to delete."""
    failed = 0
    for prefix, resource in KINDS:
        for meta in listing(api, prefix, resource):
            name = meta["name"]
            if (resource, name) in SKIP or not due(meta, now):
                continue
            # The uid precondition: never a newer object that took the name.
            body = {"propagationPolicy": "Background", "preconditions": {"uid": meta["uid"]}}
            if dry_run:
                # In the body: with a body, the API server ignores ?dryRun=.
                body["dryRun"] = ["All"]
            try:
                api("DELETE", f"{prefix}/namespaces/{NAMESPACE}/{resource}/{name}", body)
            except K8sError as e:
                if e.code in (404, 409):
                    continue  # Gone already, or deleted with its owner.
                print(f"failed {resource}/{name}: {e}", file=sys.stderr)
                failed += 1
                continue
            age = timedelta(seconds=int((now - parse_time(meta["creationTimestamp"])).total_seconds()))
            print(f"{'would delete' if dry_run else 'deleted'} {resource}/{name} (age {age})")
    return failed


def main(argv):
    dry_run = argv[1:] == ["--dry-run"]
    if argv[1:] and not dry_run:
        sys.exit(f"usage: {argv[0]} [--dry-run]")
    failed = sweep(k8s, datetime.now(timezone.utc), dry_run)
    if failed:
        sys.exit(f"{failed} objects not deleted")


if __name__ == "__main__":
    main(sys.argv)
