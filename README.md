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
