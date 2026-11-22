# NetworkPolicy components

Shared building blocks for a namespace's NetworkPolicies, so the CIDR lists and
selectors are written once. Each one is a kustomize `Component` holding one
NetworkPolicy. A namespace lists the ones it needs and gets those policies in
its own namespace.

| Component        | Policy                 | Allows                                                      |
|------------------|------------------------|-------------------------------------------------------------|
| `default-deny`   | `default-deny`         | nothing: denies all ingress and egress, the rest add allows |
| `dns`            | `allow-dns`            | egress to CoreDNS, 53 UDP/TCP                               |
| `apiserver`      | `allow-apiserver`      | egress to the k3s servers, 6443 on the tailnet range        |
| `internet-https` | `allow-internet-https` | egress to public addresses on 443                           |
| `internet-web`   | `allow-internet-web`   | the same on 443 and 80 (use instead of `internet-https`)    |
| `from-traefik`   | `allow-from-traefik`   | ingress from Traefik's pods on 8080                         |

Every policy covers every pod in the namespace (`podSelector: {}`) until you
narrow it.

## Using them in a namespace

1. Make a folder `nefcloud/<ns>/netpol/` with this `kustomization.yaml`. List
   `default-deny` and the allows the namespace needs:

   ```yaml
   apiVersion: kustomize.config.k8s.io/v1beta1
   kind: Kustomization
   namespace: pr-reviewer
   components:
     - ../../components/netpol/default-deny
     - ../../components/netpol/dns
     - ../../components/netpol/apiserver
     - ../../components/netpol/internet-https
     - ../../components/netpol/from-traefik
   patches:
     - path: networkpolicy-patch.yaml
   ```

   Then list `netpol` under `resources:` in `nefcloud/<ns>/kustomization.yaml`,
   in place of the hand-written `networkpolicy.yaml` if there is one.

   The components have no namespace, so `namespace:` is required here. Keep it
   in this folder: in `nefcloud/<ns>/kustomization.yaml` it would also move
   that folder's RBAC objects that live in `flux-system` or `ops-broker`.

2. To give a policy to only some pods, write the policy's name and the new
   `podSelector` in `netpol/networkpolicy-patch.yaml`, one document per
   policy. This is an ordinary strategic merge patch: fields you leave out stay
   as the component has them. The components' selectors are all `{}`, so the
   `podSelector` you write is the one you get. Leave out `patches:` if every
   pod gets every policy.

   ```yaml
   # Only the receiver manages Jobs and takes GitHub's webhooks.
   apiVersion: networking.k8s.io/v1
   kind: NetworkPolicy
   metadata:
     name: allow-apiserver
   spec:
     podSelector:
       matchLabels:
         app.kubernetes.io/name: receiver
   ---
   apiVersion: networking.k8s.io/v1
   kind: NetworkPolicy
   metadata:
     name: allow-from-traefik
   spec:
     podSelector:
       matchLabels:
         app.kubernetes.io/name: receiver
   ```

3. To change a port, for example `from-traefik` for a pod that listens on
   25565, patch `ingress` (or `egress`) too. A list in a patch replaces the
   whole list, so write the `from` (or `to`) again:

   ```yaml
   apiVersion: networking.k8s.io/v1
   kind: NetworkPolicy
   metadata:
     name: allow-from-traefik
   spec:
     ingress:
       - from:
           - namespaceSelector:
               matchLabels:
                 kubernetes.io/metadata.name: kube-system
             podSelector:
               matchLabels:
                 app.kubernetes.io/name: traefik
         ports:
           - port: 25565
   ```

4. Anything no component covers (another namespace's Service, a LAN address)
   is a plain NetworkPolicy in `netpol/`, listed under `resources:` there,
   with a comment that says why.

5. Check what you get before you push:

   ```bash
   kubectl kustomize nefcloud/<ns>/netpol
   ```

## Rules

- Use `internet-https` or `internet-web`, not both.
- Don't change a component for one namespace: patch it in that namespace. A
  change to a component changes every namespace that uses it.
- Policy names are the same as in the hand-written files, so moving a namespace
  over updates its policies in place.

## Tests

`tests/netpol/<ns>/` builds each namespace's hand-written `networkpolicy.yaml`
from these components, and `tests/netpol.test.sh` checks the output matches.
When moving a namespace over, copy its overlay from there to
`nefcloud/<ns>/netpol/` (the paths become `../../components/netpol/...`).
