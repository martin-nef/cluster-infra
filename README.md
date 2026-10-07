# cluster-infra

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
(run by `.github/workflows/flux-helm.yml` on shitbox when such a file changes on `main`)
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
  `--version`, `-f/--values <file next to the helm file>`, `--create-namespace`. Other flags, `--set`,
  OCI and local charts are rejected rather than ignored. Non-helm lines (`kubectl ...`) are skipped.
- Pin `--version`, otherwise the chart floats to the latest.
- The release name is the HelmRelease name. Changing it makes Flux uninstall the old release and
  install a new one, so keep the name of anything already running.
- `helmrepo.yaml`, `helmrelease.yaml` and `namespace.yaml` (only with `--create-namespace`) are
  generated and overwritten; other files in the directory (secrets, ingresses) are yours. Entries are
  only ever added to `kustomization.yaml`; removing a chart means deleting its files by hand.
- Preview locally: `DRY_RUN=1 GITHUB_TOKEN=... ./flux-helm.sh` (needs `flux` and `git`).
- If GitHub Actions is down: `{ printf "export GITHUB_TOKEN='%s'\n" "$GITHUB_TOKEN"; cat flux-helm.sh; } | ssh shitbox bash -s`.
  Only core git is needed on the Flux side to apply the resulting merge.
