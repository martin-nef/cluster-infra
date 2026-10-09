# cluster-infra

This repo serves to manage my nodes and my cluster in a reproducible manner. It also does IaC from the trunk for my apps, my cluster, but not the nodes yet.

## Prerequisites

- Ansible 2.15+ with Python 3.10+
- SSH access to the server as the `deploy` user
- Make sure the shitbox is available at the hostname `shitbox`. This can be done using the hosts file.
- The ansible vault password in `ansible/.vault_password`.

## Quick start

### 1. Install Ansible and dependencies

```bash
pip install ansible
cd ansible
ansible-galaxy collection install -r requirements.yml
```

### 2. Configure inventory

Edit `ansible/inventory.yml` and set your server's IP address.

### 3. Create vault secrets

Edit `ansible/group_vars/all/vault.yml` with your actual secrets, then encrypt:

```bash
cd ansible
echo 'your-vault-password' > .vault_password
chmod 600 .vault_password
ansible-vault encrypt group_vars/all/vault.yml
```

### 4. Run the playbook

```bash
cd ansible
ansible-playbook playbook.yml
```

## Common tasks

### Edit vault secrets

To edit a vault file
```bash
cd ansible
export EDITOR='code --wait' # to use vscode instead of vim
ansible-vault edit path/to/vault.yml
```

### Add or update a Helm chart

Helm charts are managed from plain text files at `nefcloud/<dir>/helm` holding the
`helm repo add` / `helm install` commands from the chart's install guide. `flux-helm.sh`
(run by `.github/workflows/flux-helm.yml` on a GitHub-hosted runner when such a file changes on `main`)
translates them into Flux `HelmRepository` + `HelmRelease` manifests next to the file,
updates the `kustomization.yaml` files, and opens a PR (branch `flux-helm-sync`) with the result.

`nefcloud/tailscale/helm`:

```
helm repo add tailscale https://pkgs.tailscale.com/helmcharts
helm repo update
helm upgrade --install tailscale-release tailscale/tailscale-operator \
  --namespace tailscale --create-namespace --version 1.96.5
```

- Supported: `repo add` (http/https), `install`/`upgrade` of `<repo>/<chart>`, `-n/--namespace`,
  `--version`, `-f/--values values*.yaml` (next to the helm file), `--create-namespace`. Other flags, `--set`,
  OCI and local charts are rejected rather than ignored, and so is a `helm` that is not the first word
  of its line (`sudo helm ...`, `cd x && helm ...`) or has shell operators/expansions. Other commands
  (`kubectl ...`, `export X=$(...)`) are skipped.
- Pin `--version`, otherwise the chart floats to the latest (the script warns and repeats the warning
  in the PR description).
- Every HelmRepository, HelmRelease and Namespace may come from one directory only, as kustomize rejects
  duplicates. Add the same URL under another repo name in a second directory, and pass `--create-namespace`
  in only one directory per namespace; the script fails and says which directory already owns it.
- The release name is the HelmRelease name. Changing it makes Flux uninstall the old release and
  install a new one, so keep the name of anything already running.
- `helmrepo.yaml`, `helmrelease.yaml` and `namespace.yaml` (only with `--create-namespace`) are
  generated and overwritten; other files in the directory (secrets, ingresses) are yours. Entries are
  only ever added to `kustomization.yaml`; removing a chart means deleting its files by hand (the script
  warns about a `namespace.yaml` that is no longer generated). Deleting a Namespace makes Flux prune
  everything in it.
- Preview locally: `DRY_RUN=1 GITHUB_TOKEN=... ./flux-helm.sh` (needs `flux` and `git`).
  `FLUX_VERSION=2.8.6` makes it fail on any other flux CLI, as the workflow does: the CLI decides the
  apiVersions of the generated objects, so keep it equal to the controllers in `nefcloud/flux-system`.
- If GitHub Actions is down, run `GITHUB_TOKEN=... ./flux-helm.sh` from any machine with `flux`, `git` and
  `gh` (no cluster or tailnet access needed). Only core git is needed on the Flux side to apply the
  resulting merge.
- Tests: `tests/flux-helm.test.sh [sh|bash]` runs the script against throwaway local repos with a stubbed
  `gh`; nothing leaves the machine. Needs `git` and `flux`; without `kubectl` the render check is skipped,
  without `perl` the three signal checks are. `.github/workflows/shelltest.yml` runs it under `sh` and
  `bash` on PRs that touch `flux-helm.sh` or `tests/`.

### Shell scripts

`.github/workflows/shellcheck.yml` runs [shellcheck](https://www.shellcheck.net) on PRs that touch a
`*.sh` file, and only on the scripts the PR changes (changing the workflow checks them all). It pins
shellcheck 0.11.0; to run exactly that locally:
`pipx run --spec shellcheck-py==0.11.0.1 shellcheck <file>`. A script declares its own dialect (shebang or `# shellcheck shell=...`)
and any exception as a `# shellcheck disable=...` comment with the reason; there is no global config.

`ansible/roles/base/files/tailscale-flannel-guard.sh` restarts k3s on a node whose flannel lost
`tailscale0` (nodes running an etcd member take turns so etcd keeps quorum). `tests/tailscale-flannel-guard.test.sh [sh|bash]`
runs it against a fake `/sys` with stubbed `systemctl`, `curl` and `sleep`, so it touches nothing and
finishes instantly; `.github/workflows/shelltest.yml` runs it on PRs that touch the script or `tests/`.
