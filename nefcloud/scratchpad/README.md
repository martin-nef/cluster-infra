# scratchpad

A namespace where agents (`claude-remote/claude-remote`, `night-shift/worker`) try a deploy before they open a PR. They may create, change and delete workloads here and nowhere else. pr-reviewer has no access.

## How it is limited

| Limit | File | Effect |
|---|---|---|
| Pod Security `restricted` | `namespace.yaml` | Non-root, no privilege escalation, no host access. |
| Quota + LimitRange | `quota.yaml` | 2 CPU / 4 Gi requests, 4 CPU / 8 Gi limits, 10 pods, 20 Gi PVCs. No LoadBalancer or NodePort Services. |
| PriorityClass `scratchpad` (-100, never preempts) | `quota.yaml` | Game servers share the nodes. Scratch pods are evicted first. |
| NetworkPolicy | `networkpolicy.yaml` | Default deny. Allowed: pods in the namespace, DNS, public addresses on 80/443. No API server, LAN, tailnet or other namespace. |
| ValidatingAdmissionPolicy `scratchpad-services` | `admission.yaml` | Services are ClusterIP only, with no `externalIPs` (they would capture traffic to those IPs cluster-wide). |
| Role `agent` | `rbac.yaml` | Workloads, config, Secrets, logs, exec, port-forward. No RBAC, NetworkPolicy, quota, ServiceAccount or Ingress rights. |

## Use it

Every pod must set `priorityClassName: scratchpad`. A second quota (`other-priorities`) refuses pods without it. Every pod must also satisfy `restricted`: `runAsNonRoot: true`, `allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`, `seccompProfile.type: RuntimeDefault`.

```yaml
spec:
  priorityClassName: scratchpad
  securityContext: {runAsNonRoot: true, seccompProfile: {type: RuntimeDefault}}
  containers:
    - name: app
      image: ghcr.io/example/app:1
      securityContext: {allowPrivilegeEscalation: false, capabilities: {drop: [ALL]}}
```

Reach a workload with `kubectl -n scratchpad port-forward deploy/<name> 8080:80`.

No Ingress yet. Cleanup of old workloads is not automatic yet. Delete what you deploy.
