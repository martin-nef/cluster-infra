# NetworkPolicy components

Shared building blocks for a namespace's NetworkPolicies. Each one is a
kustomize `Component` holding one policy, with no namespace: the namespace's
own `kustomization.yaml` sets `namespace:` and lists the ones it needs.

| Component        | Policy                 | Allows                                                     |
|------------------|------------------------|------------------------------------------------------------|
| `default-deny`   | `default-deny`         | nothing: denies all ingress and egress, the rest add allows |
| `dns`            | `allow-dns`            | egress to CoreDNS, 53 UDP/TCP                              |
| `apiserver`      | `allow-apiserver`      | egress to the k3s servers, 6443 on the tailnet range       |
| `internet-https` | `allow-internet-https` | egress to public addresses on 443                          |
| `internet-web`   | `allow-internet-web`   | the same on 443 and 80 (use instead of `internet-https`)   |
| `from-traefik`   | `allow-from-traefik`   | ingress from Traefik's pods on 8080                        |

The policies cover every pod in the namespace. Narrow `podSelector` (or change
`ports`) with a patch in the namespace, for example:

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: pr-reviewer
components:
  - ../components/netpol/default-deny
  - ../components/netpol/dns
  - ../components/netpol/apiserver
patches:
  - target: {kind: NetworkPolicy, name: allow-apiserver}
    patch: |-
      - op: replace
        path: /spec/podSelector
        value: {matchLabels: {app.kubernetes.io/name: receiver}}
```

Use a JSON patch (`op: replace`) for `podSelector`: a strategic merge patch
merges `matchLabels` into what is there, which is fine on `{}` but not on a
selector that already has labels.

`tests/netpol/<ns>/` builds each namespace's current hand-written
`networkpolicy.yaml` from these components, and `tests/netpol.test.sh` checks
the output matches. When moving a namespace over, its overlay there is the
starting point.
