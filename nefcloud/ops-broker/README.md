# ops-broker: the limits

Break-glass for the agents: they ask for one of a fixed set of actions, you approve it from your phone, and the broker runs it. The broker is its own Flux app, [martin-nef/ops-broker](https://github.com/martin-nef/ops-broker) (`nefcloud/apps/ops-broker.yaml`): its README has the design and the API, its `docs/verbs.md` what each verb does.

This directory holds what the broker may do, kept here and out of its own repo so that a change to the broker can't widen its own limits:

| File | What |
|---|---|
| `namespace.yaml` | The `ops-broker` namespace, Pod Security `restricted`. |
| `rbac.yaml` | The `ops-broker` ServiceAccount. `ops-broker-actions` (cluster-wide): the cluster verbs, `get` and `delete` on pods, `get` and `patch` on Deployments, StatefulSets, DaemonSets and the four Flux kinds. `ops-broker-sops-keys` (`flux-system`): `create` on Secrets, for `create-sops-age-key`. No `get` or `list` on Secrets anywhere. |
| `admission.yaml` | `ops-broker-sops-keys-only`: RBAC can't limit `create` to a name, so this does. In `flux-system`, the broker may only create `<app>-sops-age` Secrets holding exactly one key, `age.agekey`. |
| `secrets.yaml` | `ops-broker-ntfy`, the ntfy topic URL (SOPS). |

The GitHub App's Secrets (`ops-broker-github-app`, `ops-broker-github-oauth`) are not in git: they live only in the cluster. The agents reach only the broker's API port (8081), through their own egress NetworkPolicies (`nefcloud/claude-remote/`, `nefcloud/night-shift/`).

## Trade-offs

- **`patch` is broader than the verbs.** RBAC can't say "only this annotation", so the ServiceAccount can patch any field of those kinds cluster-wide, and delete any pod. The broker only sends its fixed patches, and only for an approved request. An admission policy limiting its updates to those fields would close this; not done.
- **The broker can add SOPS-looking Secrets to `flux-system`.** A compromised broker could plant a key for an app that has none yet. That only matters for files encrypted to that key, which you'd see in the app's `.sops.yaml` on a PR.
