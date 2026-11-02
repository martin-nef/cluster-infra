#!/usr/bin/env bash
# Checks that the NetworkPolicy components (nefcloud/components/netpol/) render
# exactly today's hand-written policies.
#
#   tests/netpol.test.sh
#
# Each tests/netpol/<ns>/ overlay builds nefcloud/<ns>/networkpolicy.yaml from
# the components; between them the overlays use every component. Both sides go
# through `kubectl kustomize`, which drops comments and sorts keys and objects,
# so only a real difference fails. Needs kubectl, nothing else (no cluster).
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$HERE/.."
command -v kubectl > /dev/null 2>&1 || { echo "kubectl is required" >&2; exit 2; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

fail=0
for overlay in "$HERE"/netpol/*/; do
  ns="$(basename "$overlay")"
  # Today's policies, through kustomize too so both sides are formatted alike.
  mkdir "$WORK/$ns"
  cp "$ROOT/nefcloud/$ns/networkpolicy.yaml" "$WORK/$ns/"
  printf 'resources:\n  - networkpolicy.yaml\n' > "$WORK/$ns/kustomization.yaml"

  if ! kubectl kustomize "$WORK/$ns" > "$WORK/$ns.want" ||
     ! kubectl kustomize "$overlay" > "$WORK/$ns.got"; then
    echo "FAIL $ns: kustomize build failed"
    fail=1
  elif diff -u --label "nefcloud/$ns/networkpolicy.yaml" --label "tests/netpol/$ns" \
         "$WORK/$ns.want" "$WORK/$ns.got"; then
    echo "ok   $ns"
  else
    echo "FAIL $ns: components differ from nefcloud/$ns/networkpolicy.yaml"
    fail=1
  fi
done

# Every component is used by at least one overlay, so none goes untested.
for component in "$ROOT"/nefcloud/components/netpol/*/; do
  name="$(basename "$component")"
  if ! grep -qx "  - \.\./\.\./\.\./nefcloud/components/netpol/$name" "$HERE"/netpol/*/kustomization.yaml; then
    echo "FAIL component $name: no overlay in tests/netpol/ uses it"
    fail=1
  fi
done

exit "$fail"
