#!/bin/sh
# Turn "helm install" guides into Flux manifests and raise a PR with the result.
#
# Inputs are plain text files at nefcloud/<dir>/helm holding the helm commands
# a guide tells you to run (comments, blank lines, `\` continuations and
# non-helm lines such as `kubectl get pod` are fine):
#
#   helm repo add tailscale https://pkgs.tailscale.com/helmcharts
#   helm upgrade --install tailscale-release tailscale/tailscale-operator \
#     --namespace tailscale --create-namespace --version 1.96.5 -f values.yaml
#
# For every such file the script (re)writes, next to it:
#   helmrepo.yaml     HelmRepository per repo used (in the release's namespace)
#   helmrelease.yaml  HelmRelease per `helm install|upgrade`
#   namespace.yaml    only for releases passing --create-namespace
# and makes sure they are listed in <dir>/kustomization.yaml (created if
# missing) and <dir> in nefcloud/kustomization.yaml. Existing entries are never
# removed. The generated files are overwritten on every run, so edit the `helm`
# file, not them. The `helm` file is only parsed, never executed.
#
# Supported: repo add (https), install/upgrade with <repo>/<chart>, -n/--namespace,
# --version, -f/--values (a values*.yaml next to the `helm` file, inlined into the
# HelmRelease), --create-namespace; --install/--wait/--atomic are ignored.
# Anything else is an error rather than being silently dropped. The HelmRelease
# is named after the release, so keep the release name of anything already
# running (a rename makes Flux uninstall the old one and install a new one).
#
# Changes go to the fixed branch $PR_BRANCH (force-pushed, bot-owned) and a PR
# into $BASE_BRANCH is opened unless one is already open. Nothing to change
# means no push and no PR. Running it again on its own output is a no-op.
#
# Requires: flux (only for --export, no cluster access needed), git, curl,
# awk, sed, GITHUB_TOKEN (contents + pull requests write on the repo).
# Run manually (when GitHub Actions is unavailable):
#   { printf "export GITHUB_TOKEN='%s'\n" "$GITHUB_TOKEN"; cat flux-helm.sh; } \
#     | ssh shitbox bash -s
#
# Configuration (environment, defaults below):
#   OWNER REPO BASE_BRANCH PR_BRANCH   where to read from and open the PR
#   REPO_URL      any git URL to clone instead of GitHub (e.g. a local path)
#   ROOT          directory scanned for */helm (default nefcloud)
#   REPO_INTERVAL RELEASE_INTERVAL     reconcile intervals of the generated objects
#   PR_DRAFT=true open the PR as a draft
#   DRY_RUN=1     print the diff and stop; no push, no PR, no token needed with REPO_URL
#   OPEN_PR=0     push the branch but do not call the GitHub API
#
# Wrapped in main() so the whole script is parsed before it runs; CI pipes it
# into `ssh ... bash -s`, where commands would otherwise read the rest of the
# script from stdin.
set -eu

main() {
  OWNER="${OWNER:-martin-nef}"
  REPO="${REPO:-cluster-infra}"
  BASE_BRANCH="${BASE_BRANCH:-main}"
  PR_BRANCH="${PR_BRANCH:-flux-helm-sync}"
  ROOT="${ROOT:-nefcloud}"
  HELM_FILE=helm
  REPO_INTERVAL="${REPO_INTERVAL:-12h}"
  RELEASE_INTERVAL="${RELEASE_INTERVAL:-10m}"
  PR_DRAFT="${PR_DRAFT:-false}"
  DRY_RUN="${DRY_RUN:-0}"
  OPEN_PR="${OPEN_PR:-1}"

  if [ -z "${REPO_URL:-}" ]; then
    : "${GITHUB_TOKEN:?GITHUB_TOKEN must be set}"
    REPO_URL="https://x-access-token:$GITHUB_TOKEN@github.com/$OWNER/$REPO.git"
  fi
  if [ "$DRY_RUN" != 1 ] && [ "$OPEN_PR" = 1 ]; then
    : "${GITHUB_TOKEN:?GITHUB_TOKEN must be set}"
  fi

  WORKDIR="$(mktemp -d)"
  trap 'rm -rf "$WORKDIR"' EXIT INT TERM

  # Work on a fresh clone rather than the local checkout, since CI runs this
  # script remotely.
  REPO_DIR="$WORKDIR/clone"
  git clone -q --branch "$BASE_BRANCH" "$REPO_URL" "$REPO_DIR"
  git -C "$REPO_DIR" config user.name "Flux Bot"
  git -C "$REPO_DIR" config user.email "flux@users.noreply.github.com"
  cd "$REPO_DIR"

  found=0
  for helm in "$ROOT"/*/"$HELM_FILE"; do
    [ -f "$helm" ] || continue
    found=1
    sync_dir "$(dirname "$helm")"
  done
  [ "$found" = 1 ] || echo "no $ROOT/*/$HELM_FILE files found"

  if [ -z "$(git status --porcelain -- "$ROOT")" ]; then
    echo "manifests up to date"
    return
  fi
  git add -A -- "$ROOT"

  if [ "$DRY_RUN" = 1 ]; then
    git --no-pager diff --cached
    return
  fi
  open_pr
}

die() {
  echo "flux-helm: $*" >&2
  exit 1
}

# Lex a helm file like a shell would (quotes, backslashes, comments, line
# continuations) without expanding anything. Emits "T<word>" per word and "E"
# at the end of each command.
tokenize() {
  awk '
    function flush() {
      if (have) {
        if (index(tok, "\n")) { print "multi-line word not supported" > "/dev/stderr"; exit 1 }
        print "T" tok; ntok++
      }
      tok = ""; have = 0
    }
    {
      n = length($0); cont = 0
      for (i = 1; i <= n; i++) {
        c = substr($0, i, 1)
        if (q == "\047") { if (c == "\047") q = ""; else tok = tok c; continue }
        if (q == "\"") {
          if (c == "\"") q = ""
          else if (c == "\\" && i == n) cont = 1
          else if (c == "\\") {
            i++; d = substr($0, i, 1)
            if (d == "\"" || d == "\\" || d == "$" || d == "`") tok = tok d; else tok = tok c d
          } else tok = tok c
          continue
        }
        if (c == "\047" || c == "\"") { q = c; have = 1; continue }
        if (c == "\\") {
          if (i == n) cont = 1; else { i++; tok = tok substr($0, i, 1); have = 1 }
          continue
        }
        if (c == " " || c == "\t") { flush(); continue }
        if (c == "#" && !have) break
        tok = tok c; have = 1
      }
      if (q != "") { if (!cont) tok = tok "\n"; next }
      if (cont) next
      flush()
      if (ntok > 0) { print "E"; ntok = 0 }
    }
    END { if (q != "") { print "unterminated quote" > "/dev/stderr"; exit 1 } }
  ' "$1"
}

# Regenerate the manifests for one nefcloud/<dir>/helm.
sync_dir() {
  dir="$1"
  helmfile="$dir/$HELM_FILE"
  echo "$dir: reading $helmfile"

  out="$WORKDIR/out"
  rm -rf "$out"
  mkdir -p "$out"
  : > "$out/repos"      # alias url
  : > "$out/emitted"    # ns alias, repos already written to helmrepo.yaml
  : > "$out/releases"   # ns name
  : > "$out/namespaces"
  : > "$out/helmrepo.yaml"
  : > "$out/helmrelease.yaml"

  tokenize "$helmfile" > "$out/tokens" || die "$helmfile: cannot parse"

  set --
  while IFS= read -r line; do
    case "$line" in
      T*) set -- "$@" "${line#T}" ;;
      E) handle_command "$@"; set -- ;;
    esac
  done < "$out/tokens"

  [ -s "$out/releases" ] || die "$helmfile: no helm install/upgrade found"

  generated="helmrepo.yaml helmrelease.yaml"
  if [ -s "$out/namespaces" ]; then
    : > "$out/namespace.yaml"
    while read -r ns; do
      printf -- '---\napiVersion: v1\nkind: Namespace\nmetadata:\n  name: %s\n' "$ns" >> "$out/namespace.yaml"
    done < "$out/namespaces"
    generated="namespace.yaml $generated"
  fi

  if [ ! -f "$dir/kustomization.yaml" ]; then
    printf 'apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nresources: []\n' > "$dir/kustomization.yaml"
  fi
  for f in $generated; do
    {
      echo "# Generated by flux-helm.sh from ./$HELM_FILE. Do not edit; change ./$HELM_FILE instead."
      cat "$out/$f"
    } > "$dir/$f"
    kustomization_add "$dir/kustomization.yaml" "$f"
  done
  kustomization_add "$ROOT/kustomization.yaml" "$(basename "$dir")"
}

# $@: one tokenized command line
handle_command() {
  for t in "$@"; do
    case "$t" in
      *'$'* | *'`'*) die "$helmfile: shell expansion is not supported: $t" ;;
    esac
  done
  if [ "$1" != helm ]; then
    echo "  skipping (not helm): $*"
    return
  fi
  shift
  case "${1:-}" in
    repo)
      shift
      case "${1:-}" in
        add) shift; helm_repo_add "$@" ;;
        update | list) ;;
        *) die "$helmfile: unsupported command: helm repo $*" ;;
      esac
      ;;
    install | upgrade) shift; helm_release "$@" ;;
    search | show | list | ls | status | get | history | version | template | lint | pull) ;;
    *) die "$helmfile: unsupported command: helm $*" ;;
  esac
}

# $@: <alias> <url> [--force-update]
helm_repo_add() {
  alias="" url=""
  for a in "$@"; do
    case "$a" in
      --force-update) ;;
      -*) die "$helmfile: unsupported flag for helm repo add: $a" ;;
      *) if [ -z "$alias" ]; then alias="$a"; elif [ -z "$url" ]; then url="$a"; else die "$helmfile: too many arguments to helm repo add"; fi ;;
    esac
  done
  [ -n "$url" ] || die "$helmfile: helm repo add needs <name> <url>"
  valid_name "$alias" || die "$helmfile: invalid repo name: $alias"
  case "$url" in
    https://* | http://*) ;;
    *) die "$helmfile: only http(s) chart repositories are supported: $url" ;;
  esac
  if grep -q "^$alias " "$out/repos"; then die "$helmfile: repo $alias added twice"; fi
  echo "$alias $url" >> "$out/repos"
}

# $@: <release> <repo>/<chart> [flags]
helm_release() {
  name="" chart="" ns=default version="" createns=0 values=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --namespace | -n | --version | --values | -f)
        [ $# -ge 2 ] || die "$helmfile: $1 needs a value"
        set_flag "$1" "$2"; shift 2 ;;
      --namespace=* | --version=* | --values=*)
        set_flag "${1%%=*}" "${1#*=}"; shift ;;
      --create-namespace) createns=1; shift ;;
      --install | -i | --wait | --wait-for-jobs | --atomic) shift ;;
      -*) die "$helmfile: unsupported flag: $1" ;;
      *)
        if [ -z "$name" ]; then name="$1"
        elif [ -z "$chart" ]; then chart="$1"
        else die "$helmfile: unexpected argument: $1"; fi
        shift ;;
    esac
  done
  [ -n "$chart" ] || die "$helmfile: helm install needs <release> <repo>/<chart>"
  valid_name "$name" || die "$helmfile: invalid release name: $name"
  valid_name "$ns" || die "$helmfile: invalid namespace: $ns"

  case "$chart" in
    */*/* | /* | ./* | ../*) die "$helmfile: only <repo>/<chart> references are supported: $chart" ;;
    */*) ;;
    *) die "$helmfile: only <repo>/<chart> references are supported: $chart" ;;
  esac
  alias="${chart%%/*}" chartname="${chart#*/}"
  url="$(sed -n "s/^$alias //p" "$out/repos")"
  [ -n "$url" ] || die "$helmfile: no 'helm repo add $alias <url>' before $chart"

  if grep -qx "$ns $name" "$out/releases"; then die "$helmfile: release $name in $ns defined twice"; fi
  echo "$ns $name" >> "$out/releases"
  [ "$createns" = 0 ] || grep -qx "$ns" "$out/namespaces" || echo "$ns" >> "$out/namespaces"

  if ! grep -qx "$ns $alias" "$out/emitted"; then
    flux create source helm "$alias" --namespace="$ns" --url="$url" \
      --interval="$REPO_INTERVAL" --export >> "$out/helmrepo.yaml"
    echo "$ns $alias" >> "$out/emitted"
  fi

  set -- "$name" --namespace="$ns" --source="HelmRepository/$alias" --chart="$chartname" \
    --interval="$RELEASE_INTERVAL"
  [ -z "$version" ] || set -- "$@" --chart-version="$version"
  for v in $values; do set -- "$@" --values="$v"; done
  flux create helmrelease "$@" --export >> "$out/helmrelease.yaml"
  echo "  $ns/$name: chart $chart${version:+ $version}"
}

# Sets the enclosing helm_release's variables (sh has no nested scopes).
set_flag() {
  case "$1" in
    --namespace | -n) ns="$2" ;;
    --version) version="$2" ;;
    --values | -f)
      case "$2" in
        values*.yaml) ;;
        *) die "$helmfile: values file must be named values*.yaml next to $HELM_FILE (the workflow only watches those): $2" ;;
      esac
      case "$2" in
        *[!A-Za-z0-9._-]*) die "$helmfile: invalid values file name: $2" ;;
      esac
      [ -f "$dir/$2" ] || die "$helmfile: values file not found: $dir/$2"
      values="$values $dir/$2" ;;
  esac
}

valid_name() {
  case "$1" in
    "" | *[!a-z0-9-]* | -* | *-) return 1 ;;
  esac
}

# Append "  - <entry>" to the resources: list of a kustomization.yaml unless it is
# already listed. Never reorders or removes anything.
kustomization_add() {
  file="$1" entry="$2"
  awk -v entry="$entry" '
    { line[NR] = $0 }
    END {
      for (i = 1; i <= NR; i++) {
        l = line[i]
        if (l ~ /^resources:[ \t]*\[\][ \t]*$/) { start = i; empty = 1; continue }
        if (l ~ /^resources:[ \t]*$/) { start = i; inres = 1; continue }
        if (!inres) continue
        if (l ~ /^[A-Za-z]/) { inres = 0; continue }
        if (l ~ /^[ \t]*\[\]/) { print "resources: [] is not supported here" > "/dev/stderr"; exit 1 }
        if (l ~ /^[ \t]*-[ \t]/) {
          last = i
          v = l; sub(/^[ \t]*-[ \t]+/, "", v); sub(/[ \t]*(#.*)?$/, "", v); gsub(/["\047]/, "", v)
          if (v == entry) present = 1
        }
      }
      if (present) { for (i = 1; i <= NR; i++) print line[i]; exit 0 }
      if (!start) { for (i = 1; i <= NR; i++) print line[i]; print "resources:"; print "  - " entry; exit 0 }
      at = last ? last : start
      indent = "  "
      if (last) { indent = line[last]; sub(/-.*/, "", indent) }
      for (i = 1; i <= NR; i++) {
        if (i == start && empty) { print "resources:"; print "  - " entry; continue }
        print line[i]
        if (i == at && !empty) print indent "- " entry
      }
    }
  ' "$file" > "$WORKDIR/kustomization.tmp" || die "cannot update $file"
  cat "$WORKDIR/kustomization.tmp" > "$file"
}

json_escape() {
  sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' | awk 'BEGIN { ORS = "\\n" } { print }'
}

# Commit the staged changes to $PR_BRANCH (force-pushed unless it already holds
# exactly this) and open a PR unless one is already open.
open_pr() {
  title="chore(flux): sync helm releases"
  files="$(git diff --cached --name-only)"
  git checkout -q -b "$PR_BRANCH"
  git commit -q -m "$title" -m "Generated by flux-helm.sh from $ROOT/*/$HELM_FILE on $BASE_BRANCH."
  echo "changes:"
  echo "$files" | sed 's/^/  /'

  if git fetch -q origin "$PR_BRANCH" 2>/dev/null && git diff --quiet FETCH_HEAD HEAD \
    && [ "$(git rev-parse FETCH_HEAD^)" = "$(git rev-parse HEAD^)" ]; then
    echo "branch $PR_BRANCH already up to date"
  else
    git push -q -f origin "HEAD:refs/heads/$PR_BRANCH"
    echo "pushed $PR_BRANCH"
  fi
  [ "$OPEN_PR" = 1 ] || return 0

  api="https://api.github.com/repos/$OWNER/$REPO/pulls"
  existing="$(curl -sS --fail --connect-timeout 10 --max-time 30 \
    -H "Authorization: token $GITHUB_TOKEN" \
    -H "Accept: application/vnd.github+json" \
    "$api?state=open&head=$OWNER:$PR_BRANCH" \
    | grep -o '"html_url": *"[^"]*/pull/[0-9]*"' | head -n 1 | sed 's/.*"\(http[^"]*\)"/\1/' || true)"
  if [ -n "$existing" ]; then
    echo "PR already open: $existing"
    # e.g. a PR stacked on a branch that has since been merged
    curl -sS --fail --connect-timeout 10 --max-time 30 -X PATCH \
      -H "Authorization: token $GITHUB_TOKEN" \
      -H "Accept: application/vnd.github+json" \
      "$api/${existing##*/}" -d "{\"base\":\"$BASE_BRANCH\"}" >/dev/null \
      || echo "warning: could not set the PR base to $BASE_BRANCH" >&2
    return
  fi

  body="Generated by flux-helm.sh from \`$ROOT/*/$HELM_FILE\`. Edit the helm file on $BASE_BRANCH rather than this branch, it is force-pushed on every run.

Changed files:
$(echo "$files" | sed 's/^/- /')"
  pr_url="$(curl -sS --fail --connect-timeout 10 --max-time 30 -X POST \
    -H "Authorization: token $GITHUB_TOKEN" \
    -H "Accept: application/vnd.github+json" \
    "$api" \
    -d "{\"title\":\"$title\",\"head\":\"$PR_BRANCH\",\"base\":\"$BASE_BRANCH\",\"draft\":$PR_DRAFT,\"body\":\"$(printf '%s' "$body" | json_escape)\"}" \
    | grep -o '"html_url": *"[^"]*/pull/[0-9]*"' | head -n 1 | sed 's/.*"\(http[^"]*\)"/\1/')"
  [ -n "$pr_url" ] || die "failed to open PR for $PR_BRANCH"
  echo "opened PR $pr_url"
}

main "$@"
