#!/bin/sh
# Bootstrap Flux and register app repos. Replaces the old ansible flux role.
# Requires: flux, kubectl, curl, ssh-keygen, git, a kubeconfig pointing at the
# cluster, and GITHUB_TOKEN (repo admin scope, used for bootstrap, deploy keys and PRs).
# Each new app gets a branch <name>-flux-bootstrap and a draft PR into main.
# The deploy key and <name>-auth secret are created once the PR is open, and
# again on any later run where the secret is missing.
#
# Wrapped in main() so the whole script is parsed before it runs; CI pipes it
# into `ssh ... bash -s`, where commands would otherwise read the rest of the
# script from stdin.
set -eu

main() {
  OWNER=martin-nef

  : "${GITHUB_TOKEN:?GITHUB_TOKEN must be set}"
  export GITHUB_TOKEN

  if flux get sources git flux-system -n flux-system >/dev/null 2>&1; then
    echo "Flux already bootstrapped"
  else
    flux bootstrap github \
      --owner="$OWNER" \
      --repository=cluster-infra \
      --branch=main \
      --path=nefcloud \
      --personal
  fi

  WORKDIR="$(mktemp -d)"
  trap 'rm -rf "$WORKDIR"' EXIT INT TERM

  REPO_DIR="$WORKDIR/cluster-infra"
  git clone -q --branch main \
    "https://x-access-token:$GITHUB_TOKEN@github.com/$OWNER/cluster-infra.git" \
    "$REPO_DIR"
  git -C "$REPO_DIR" config user.name "Flux Bot"
  git -C "$REPO_DIR" config user.email "flux@users.noreply.github.com"

  # Read from the clone rather than the local checkout, since CI runs this
  # script remotely. Strips # comments and blank lines.
  APPS="$(sed -e 's/#.*//' -e '/^[[:space:]]*$/d' "$REPO_DIR/flux-apps")"

  echo "$APPS" | while read -r name repo branch path; do
    [ -n "$name" ] || continue
    [ -n "${repo:-}" ] && [ "$repo" != - ] || repo="$name"
    [ -n "${branch:-}" ] && [ "$branch" != - ] || branch=main
    [ -n "${path:-}" ] && [ "$path" != - ] || path=./deploy

    url="ssh://git@github.com/$OWNER/$repo.git"
    open_pr "$name" "$url" "$branch" "$path"
    ensure_deploy_key "$name" "$repo" "$url"
  done
}

# Commit the app's Flux manifests to a new branch and open a draft PR,
# unless they are already on main or the branch has been pushed.
open_pr() {
  name="$1" url="$2" branch="$3" path="$4"
  pr_branch="$name-flux-bootstrap"

  if git -C "$REPO_DIR" cat-file -e "origin/main:nefcloud/apps/$name.yaml" 2>/dev/null; then
    echo "$name: manifest exists on main"
    return
  fi
  rc=0
  git -C "$REPO_DIR" ls-remote --exit-code --heads origin "$pr_branch" >/dev/null || rc=$?
  case "$rc" in
    0) echo "$name: branch $pr_branch already pushed"; return ;;
    2) ;;
    *) echo "$name: ls-remote failed ($rc)" >&2; exit 1 ;;
  esac

  wt="$WORKDIR/wt-$name"
  git -C "$REPO_DIR" worktree add -q -B "$pr_branch" "$wt" origin/main
  {
    flux create source git "$name" \
      --url="$url" \
      --branch="$branch" \
      --interval=1m \
      --secret-ref="$name-auth" \
      --export
    flux create kustomization "$name" \
      --source="GitRepository/$name" \
      --path="$path" \
      --prune=true \
      --interval=10m \
      --export
  } > "$wt/nefcloud/apps/$name.yaml"
  # Assumes resources: is the last key in the file.
  printf '  - %s.yaml\n' "$name" >> "$wt/nefcloud/apps/kustomization.yaml"

  title="chore(flux): register $name"
  git -C "$wt" add "nefcloud/apps/$name.yaml" nefcloud/apps/kustomization.yaml
  git -C "$wt" commit -q -m "$title"
  git -C "$wt" push -q -u origin "$pr_branch"
  git -C "$REPO_DIR" worktree remove "$wt"

  pr_url="$(curl -sS --fail --connect-timeout 10 --max-time 30 -X POST \
    -H "Authorization: token $GITHUB_TOKEN" \
    -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/$OWNER/cluster-infra/pulls" \
    -d "{\"title\":\"$title\",\"head\":\"$pr_branch\",\"base\":\"main\",\"draft\":true,\"body\":\"Registers $name with Flux. The deploy key and $name-auth secret are created by flux.sh once this PR is open.\"}" \
    | grep -o '"html_url": *"[^"]*/pull/[0-9]*"' | head -n 1 | sed 's/.*"\(http[^"]*\)"/\1/')"
  [ -n "$pr_url" ] || { echo "$name: failed to open PR for $pr_branch" >&2; exit 1; }
  echo "$name: opened draft PR $pr_url"
}

# Print the ids of the repo's deploy keys titled flux-<name>, one per line.
# Parses with sed/awk since jq is not a requirement.
list_deploy_key_ids() {
  name="$1" repo="$2"
  curl -sS --fail --connect-timeout 10 --max-time 30 \
    -H "Authorization: token $GITHUB_TOKEN" \
    -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/$OWNER/$repo/keys?per_page=100" \
    | grep -oE '"(id|title)": *("[^"]*"|[0-9]+)' \
    | sed -E 's/^"id": *([0-9]+)$/id \1/; s/^"title": *"(.*)"$/title \1/' \
    | awk -v t="flux-$name" '$1 == "id" { id = $2 } $1 == "title" && $2 == t { print id }'
}

# Generate a deploy key, register it on the app repo and store it as the
# <name>-auth secret, unless that secret already exists. A key left on the
# repo by an earlier run that died before the secret was created is useless
# (the private half is gone), so it is deleted and replaced.
ensure_deploy_key() {
  name="$1" repo="$2" url="$3"

  if kubectl -n flux-system get secret "$name-auth" >/dev/null 2>&1; then
    return
  fi

  for key_id in $(list_deploy_key_ids "$name" "$repo"); do
    curl -sS --fail --connect-timeout 10 --max-time 30 -X DELETE \
      -H "Authorization: token $GITHUB_TOKEN" \
      -H "Accept: application/vnd.github+json" \
      "https://api.github.com/repos/$OWNER/$repo/keys/$key_id" >/dev/null
    echo "$name: removed stale deploy key flux-$name ($key_id)"
  done

  keyfile="$WORKDIR/$name-id_ed25519"
  rm -f "$keyfile" "$keyfile.pub"
  ssh-keygen -t ed25519 -f "$keyfile" -N "" -C "flux-$name" -q

  curl -sS --fail --connect-timeout 10 --max-time 30 -X POST \
    -H "Authorization: token $GITHUB_TOKEN" \
    -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/$OWNER/$repo/keys" \
    -d "{\"title\":\"flux-$name\",\"key\":\"$(cat "$keyfile.pub")\",\"read_only\":true}" \
    >/dev/null
  echo "$name: registered deploy key flux-$name"

  flux create secret git "$name-auth" --url="$url" --private-key-file="$keyfile"
  rm -f "$keyfile" "$keyfile.pub"
}

main "$@"
