# ops-broker: break-glass v1

Some fixes can't wait for a PR, or can't be made by one: a pod stuck after a strategy change, a suspended Kustomization, a release that needs a nudge. In v0 the agent (claude-remote) stops and tells you the command, and you run it in Headlamp. v1 keeps you as the one who decides, but turns "find it in Headlamp and do it" into "read one page, press Approve".

## How it works

1. The agent creates an `OpsRequest` in the `ops-broker` namespace:

   ```yaml
   apiVersion: ops.nefcloud.top/v1alpha1
   kind: OpsRequest
   metadata:
     generateName: restart-
     namespace: ops-broker
   spec:
     verb: rollout-restart
     target: {kind: Deployment, namespace: valheim, name: valheim}
     reason: Stuck on the old ReplicaSet after the strategy change in valheim#12.
   ```

2. The broker checks the target exists (and, for `delete-pod`, that a controller will recreate the pod), marks it `Pending`, and sends an ntfy notification with a **Review** button.
3. The button opens `https://ops.nefcloud.top/r/<name>`, behind the same Google login as Headlamp. It shows the verb, the target and the agent's reason, with **Approve** and **Deny**.
4. On Approve the broker runs the action and writes the result to the request's status. The agent watches it with `kubectl -n ops-broker get opsrequest <name> -w`.

| Verb | Kinds | What it does |
|---|---|---|
| `delete-pod` | Pod | Deletes that exact pod (by the UID seen when the request came in, so a StatefulSet's same-named replacement is safe). Only pods a ReplicaSet, StatefulSet or DaemonSet controls; bare and Job pods may not come back. |
| `rollout-restart` | Deployment, StatefulSet, DaemonSet | What `kubectl rollout restart` does: bumps `restartedAt` in the pod template. |
| `flux-reconcile` | Kustomization, HelmRelease, GitRepository, HelmRepository | What `flux reconcile` does: bumps `reconcile.fluxcd.io/requestedAt`. No `--with-source`; request the source separately. |
| `flux-suspend` / `flux-resume` | same | Sets `spec.suspend`. |

A request is `Pending` → `Succeeded` / `Failed` / `Unknown` / `Denied` / `Expired`, or `Rejected` straight away (target missing, a pod that may not come back, too many pending). `Unknown` means the action may or may not have taken effect: check the target before asking again. Requests expire after 15 minutes, at most 3 wait at once, and finished ones are kept 90 days, or 7 for `Rejected` and `Expired`, which decided nothing (`config.env`).

## Design decisions

**The approval lives behind your login, not in the notification.** The agent has the internet on 443, so it can read and post to any ntfy topic it learns. If a notification carried an approve link or token, the agent could approve itself. Instead the notification only links to a page, and only a request carrying oauth2-proxy's `X-Auth-Request-Email` for an address in `ALLOWED_APPROVERS` can approve. The agent can reach `ops.nefcloud.top` through Cloudflare like anyone else, but it can't sign in to your Google account. Claude Code's permission prompt was never an option: it runs inside the pod the broker is meant to check.

**Requests are a CRD, not an HTTP API.** The agent already talks to the API server and to nothing else in the cluster (its NetworkPolicy refuses 10.0.0.0/8). A CRD gets authentication, authorisation, validation and storage from the API server, with no new network path and no auth code in the broker:

- The schema is the verb list. CEL rules tie each verb to its kinds, so `kubectl create` refuses anything else before the broker sees it. The broker checks again, in case the CRD is ever loosened.
- `spec` is immutable (`self == oldSelf`), so what you read on the page is what runs. The page also posts the spec's hash back, and a mismatch is refused.
- `status` is a subresource. The agent's Role is `create/get/list/watch` on `opsrequests` only, so it can't mark its own request approved, edit it, or delete it from the audit trail.
- An admission policy (`admission.yaml`) refuses `ownerReferences` and `finalizers` on new requests. Either would let a requester undo the audit trail: an owner, even a made-up one, hands the record to the garbage collector, and a finalizer blocks its cleanup and holds quota.
- A ResourceQuota caps the namespace at 200 requests, even if the broker is down. Junk (`Rejected`, `Expired`) is deleted after 7 days, so filling it takes sustained, visible effort, and the fallback is Headlamp.

**It lives in cluster-infra, code in a ConfigMap.** The broker holds cluster-wide delete and patch rights. If it lived in a repo the agent can open PRs to and Flux deploys without impersonation, one rushed merge could widen what it does. Here it sits with the sandbox it extends, on the same protected `main`. `broker.py` is stdlib Python on the stock `python:3.13-slim` image (pinned by digest, the one cv-current uses), mounted from a hashed ConfigMap, so there is no image to build and no workflow to change. An edit rolls the pod.

**Fail closed, and say so honestly.** An approved action runs once, right away, or never:

- Every status write carries the resourceVersion the broker read. `Recreate` doesn't stop a deleted pod's replacement from starting while the old one shuts down. If two brokers overlap, only one can claim an approval, and the other stops before running anything.
- If the outcome isn't known, the request ends `Unknown`, not `Failed`. That covers a timeout or 5xx from the API server, or the broker stopping after Approve but before recording the result. "Failed, request it again" could mean deleting or restarting twice. A stranded `Approved` is marked `Unknown` after a minute, and never run late.
- A 429 or 5xx while checking a new request leaves it for the next scan instead of rejecting it. Stale requests are expired before new ones are admitted, so after an outage they don't take the pending slots.

A lost ntfy message only costs time: the page lists every waiting request first. Without the ntfy Secret the broker still runs; requests just wait unannounced.

**The page trusts a header, so only Traefik may reach it.** `X-Auth-Request-Email` is set by the oauth2-proxy forwardAuth. A NetworkPolicy lets only Traefik's pods connect, as for headlamp-auth. POSTs must also carry `Origin: https://ops.nefcloud.top`, which stops cross-site form posts. The oauth2-proxy cookie is `SameSite=Lax`, which mostly does too. The page escapes everything the agent wrote and sends a strict CSP.

## Trade-offs and limits

- **`patch` is broader than the verbs.** RBAC can't say "only this annotation", so the broker's ServiceAccount can patch any field of Deployments, StatefulSets, DaemonSets and the four Flux kinds cluster-wide, and delete any pod. The code only sends the fixed patches above, and nothing but an approved request reaches that code. A ValidatingAdmissionPolicy limiting this ServiceAccount's updates to those fields would close it. It's left for later because it can't be tested without a cluster.
- **Approvals are as strong as your Google session.** oauth2-proxy cookies last 7 days. Anyone with your unlocked phone can approve. There's no second factor at approve time.
- **Who asked isn't recorded by the API.** Anything allowed to create OpsRequests can file one. Today that's the agent and cluster admins. When there are more requesters, an admission policy that stamps `request.userInfo` onto the object is the fix.
- **The ntfy topic is a shared secret, but not a security one.** It's random and SOPS-encrypted (`secrets.yaml`), so outsiders can't read what the agent asks for. Knowing it lets someone send you fake notifications. Those can only link somewhere else, so check the link is `ops.nefcloud.top` before you sign in to anything. The agent generated this topic, so it knows it. A reserved topic with an access token on ntfy, or a self-hosted ntfy, would take that away.
- **Polling, not watching.** The broker lists requests every 5 seconds. A watch would be quicker, but it's more code to get right for no real gain at this volume.
- **Audit lives in etcd for 90 days.** Each request's status records who decided what and when, and the broker prints one JSON line per transition (`kubectl -n ops-broker logs deploy/ops-broker`). Logs go when the pod does, and there is no API server audit log on k3s here.
- **The verb list is short on purpose.** Things like removing a finalizer or scaling to zero are left out. They can lose data or take a service down, they're rare, and Headlamp still covers them. Adding a verb means a PR here that touches the CRD, the ClusterRole and `broker.py`.

## Later

- **Discord** instead of, or beside, ntfy. It's planned as its own project: a generic, secure integration for the whole cluster. When it exists, `notify()` becomes one more sender. The approval stays on this page either way, for the same reason as with ntfy.
- An admission policy limiting the broker's patches (above).
- Approve from inside the ntfy app with a signed one-time link that still needs the login. Not worth it while the page is one tap away.

## Setup and checks

After merge, subscribe to the topic in the ntfy app (the PR says where to find it). Then:

```bash
kubectl -n ops-broker logs deploy/ops-broker          # "Listening ...; ntfy on"
kubectl -n ops-broker get opsrequests                 # the queue and its history
cd nefcloud/ops-broker/broker && python3 -I -m unittest -v   # broker against a fake API
```
