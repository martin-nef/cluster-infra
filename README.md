# cluster-infra

## Prerequisites

- Ansible 2.15+ with Python 3.10+
- SSH access to the server as the `deploy` user
- Make sure the shitbox is available at the hostname `shitbox`. This can be using the hosts file.

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

### Redeploy a single app

```bash
cd ansible
ansible-playbook playbook.yml --tags apps -e app_filter=beam_demo
```

### Add a new app

1. Create `apps/<appname>/docker-compose.yml`
2. Optionally create `apps/<appname>/.env.j2` for secrets
3. Add the app to the `apps` list in `ansible/group_vars/all/vars.yml`
4. Add any new vault vars to `ansible/group_vars/all/vault.yml`
5. Run the playbook

### Edit vault secrets

```bash
cd ansible
ansible-vault edit group_vars/all/vault.yml
```
