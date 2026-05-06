#!/bin/sh

set -eu

usage() {
  echo "Usage: $0 <hostname> <path_to_ssh_public_key>"
  echo ""
  echo "Example:"
  echo "  $0 myhost ~/.ssh/id_ed25519.pub"
  exit 1
}

if [ $# -ne 2 ]; then
  usage
fi

hostname="$1"
pubkey="$2"

if [ ! -f "$pubkey" ]; then
  echo "Error: SSH public key file '$pubkey' not found."
  exit 1
fi

cp autoinstall.yaml.template autoinstall.yaml

# Escape parameters for sed replacement (& \ and | need escaping)
hostname_escaped=$(printf '%s\n' "$hostname" | sed -e 's/[&\|]/\\&/g')
pubkey_content=$(cat "$pubkey")
pubkey_escaped=$(printf '%s\n' "$pubkey_content" | sed -e 's/[&\|]/\\&/g')

# Use temp file for POSIX-compliant sed (no -i flag)
tmp_file="autoinstall.yaml.tmp.$$"
sed "s|<HOSTNAME>|$hostname_escaped|g" autoinstall.yaml > "$tmp_file"
sed "s|<SSH_PUBLIC_KEY>|$pubkey_escaped|g" "$tmp_file" > autoinstall.yaml
rm "$tmp_file"

echo "✓ Stamped autoinstall.yaml with hostname=$hostname"
