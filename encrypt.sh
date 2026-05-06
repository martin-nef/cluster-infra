#!/bin/sh
set -eu
ANSIBLE_DIR="$(cd "$(dirname "$0")/ansible" && pwd)"
find "$ANSIBLE_DIR" -name "vault.yml" -exec ansible-vault encrypt --vault-password-file "$ANSIBLE_DIR/.vault_password" {} +
