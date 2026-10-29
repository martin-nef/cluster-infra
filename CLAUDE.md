Manages the nodes (ansible/) and the k3s cluster (nefcloud/, reconciled by Flux from `main`). See README.md for the general layout.

Important! Do not `kubectl apply`. Everything under nefcloud/ is managed through Flux: commit + push to `main`, then `flux reconcile kustomization <name> --with-source` if you don't want to wait.

## Adding a new Flux app

An app is its own GitHub repo under `martin-nef/` with its manifests in `deploy/`. Registering it here is one line plus CI. Do not hand-write `nefcloud/apps/<name>.yaml` for a new app, `flux.sh` generates it.

1. Make sure the app repo exists on GitHub (`martin-nef/<repo>`); `flux.sh` registers a deploy key on it. It does not need a `deploy/` yet, that comes in step 6.
2. Add one line to `flux-apps`: `name [repo] [branch] [path]`. Use `-` to keep a default. Defaults: `repo=name`, `branch=main`, `path=./deploy`. Example: `playit playit-gg` for app `playit` in repo `playit-gg`.
3. Commit and push to `main`. Previous registrations were committed straight to `main` (e.g. `add project-zomboid app`).
4. The `Flux` workflow (`.github/workflows/flux.yml`, runs on push to `main`) pipes `flux.sh` over SSH to shitbox. For each app in `flux-apps` it:
   - opens a draft PR from branch `<name>-flux-bootstrap` adding `nefcloud/apps/<name>.yaml` (a `GitRepository` plus a `Kustomization`) and appending it to `nefcloud/apps/kustomization.yaml`. It skips apps whose manifest is already on `main` or whose branch is already pushed.
   - creates a read-only deploy key `flux-<name>` on the app repo and the `<name>-auth` secret in `flux-system`, unless that secret already exists.
5. Review and merge the draft PR (`flux-check.yml` validates PRs that touch `nefcloud/**`). Flux picks the app up after the merge. Until step 6 is done its Kustomization shows `kustomization path not found`, which is expected.
6. After the PR is merged, stamp out the Kustomization in the app repo (skip if it already has one at the registered path):

   ```bash
   cd ../<repo>
   mkdir -p deploy
   cat > deploy/kustomization.yaml <<'EOF'
   apiVersion: kustomize.config.k8s.io/v1beta1
   kind: Kustomization
   resources: []
   EOF
   kubectl kustomize deploy   # must build; empty output is fine
   git add deploy/kustomization.yaml
   git commit -m "chore: add empty Flux kustomization in deploy/" -- deploy/kustomization.yaml
   git push
   flux reconcile kustomization <name> -n flux-system --with-source
   ```

   Commit with the pathspec (`-- deploy/kustomization.yaml`) so nothing else that happens to be staged in the app repo goes along. Add the app's manifests to `resources:` later.

### Follow-ups the generated manifest does not cover

- **Secrets.** If the app repo has SOPS-encrypted secrets (`deploy/secrets.yaml`, matched by `.sops.yaml`), add a `decryption` block to the app's `Kustomization` in `nefcloud/apps/<name>.yaml`, as `project-zomboid` does:

  ```yaml
  spec:
    decryption:
      provider: sops
      secretRef:
        name: <name>-sops-age
  ```

  The `<name>-sops-age` secret lives in `flux-system` and nothing in this repo creates it, so check that it exists (`kubectl -n flux-system get secret <name>-sops-age`) before relying on it. `smart-reviewer` uses the shared `flux-sops-age` instead.
- **Non-HTTP ports (game servers).** Add an entrypoint per port to `nefcloud/traefik/helmchartconfig.yaml` (`port`, `expose.default: true`, `exposedPort`, `protocol: TCP|UDP`), and reference it from an `IngressRoute`/`IngressRouteUDP` in the app's `deploy/`. Entrypoint names are at most 15 characters.
- **HTTP.** A plain `Ingress` in the app's `deploy/` with host `<name>.nefcloud.top` is enough (see `smart-reviewer`).

### Checking it worked

```bash
flux get sources git -n flux-system
flux get kustomizations -n flux-system
```

`<name>` should be `Ready` in both. A `path not found` error on the Kustomization means step 6 hasn't been done (no `deploy/`, or whatever `path` was set to, on the app repo's branch).
