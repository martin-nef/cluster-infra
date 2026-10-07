#!/usr/bin/env bash
# Behaviour tests for flux-helm.sh.
#
#   tests/flux-helm.test.sh [SHELL]
#
# SHELL is the interpreter that runs the script under test (default: sh, i.e.
# dash on Debian/Ubuntu; also try bash). Needs git and flux. kubectl (renders
# the generated tree) and perl (SIGINT test) are optional, and the tests that
# need them are skipped when missing.
#
# Nothing leaves the machine: the script clones throwaway local repositories,
# `gh` is a stub that records its arguments, and git ignores the global and
# system config. Everything lives in a temp dir that is removed on exit.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/../flux-helm.sh"
SH="${1:-sh}"

for tool in git flux "$SH"; do
  command -v "$tool" > /dev/null 2>&1 || { echo "$tool is required" >&2; exit 2; }
done

# The script is configured through the environment (CI sets FLUX_VERSION, a
# developer may have GITHUB_TOKEN, ...); the tests must see only their own.
unset OWNER REPO BASE_BRANCH PR_BRANCH REPO_URL ROOT REPO_INTERVAL RELEASE_INTERVAL \
  FLUX_VERSION PR_DRAFT DRY_RUN OPEN_PR GITHUB_TOKEN GH_TOKEN

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
# The script under test makes its scratch dir here, so a leak shows up as a leftover.
export TMPDIR="$WORK/tmp"
mkdir "$TMPDIR"
# Reproducible commits that ignore the developer's git config (signing, hooks, ...).
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
export GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@example.com
export GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@example.com

pass=0
fail=0
OUT=""
RC=0

ok() {
  pass=$((pass + 1))
  echo "  ok   $1"
}

bad() {
  fail=$((fail + 1))
  echo "  FAIL $1"
  [ -z "$OUT" ] || printf '%s\n' "$OUT" | head -n 15 | sed 's/^/       | /'
}

skip() {
  echo "  skip $1"
}

section() {
  echo "== [$SH] $1"
}

# check <description> <predicate> [args]: ok if the predicate holds for the last run.
check() {
  local desc="$1"
  shift
  if "$@"; then ok "$desc"; else bad "$desc"; fi
}

has_all() {
  local s
  for s in "$@"; do
    grep -qF -- "$s" <<< "$OUT" || return 1
  done
}

lacks() {
  ! grep -qF -- "$1" <<< "$OUT"
}

fails_with() {
  [ "$RC" -ne 0 ] && has_all "$@"
}

succeeds_with() {
  [ "$RC" -eq 0 ] && has_all "$@"
}

# new_repo <name>: a repo at $WORK/repos/<name> with nefcloud/{t,u} and an empty
# root kustomization; leaves the shell inside it.
new_repo() {
  local dir="$WORK/repos/$1"
  rm -rf "$dir"
  mkdir -p "$dir/nefcloud/t" "$dir/nefcloud/u"
  cd "$dir" || exit 2
  git init -q -b main
  printf 'apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nresources: []\n' > nefcloud/kustomization.yaml
}

commit_all() {
  git add -A
  git commit -qm test
}

# run_dry <repo> [VAR=value ...]: dry-run the script against the repo.
run_dry() {
  local repo="$1"
  shift
  OUT="$(env "$@" DRY_RUN=1 REPO_URL="$WORK/repos/$repo" "$SH" "$SCRIPT" 2>&1)"
  RC=$?
}

# bare_of <repo>: a bare clone to push to; prints its path.
bare_of() {
  rm -rf "$WORK/repos/$1.bare"
  git clone -q --bare "$WORK/repos/$1" "$WORK/repos/$1.bare"
  echo "$WORK/repos/$1.bare"
}

GOOD='helm repo add foo https://example.com/charts
helm install real foo/real --version 1.0.0'

section "helm behind a prefix, path or operator is an error, never skipped"
for line in 'sudo helm install ghost foo/ghost --version 1.0.0' \
  'KUBECONFIG=x helm install ghost foo/ghost --version 1.0.0' \
  'cd x; helm install ghost foo/ghost --version 1.0.0' \
  'cd x;helm install ghost foo/ghost --version 1.0.0' \
  'true && helm install ghost foo/ghost --version 1.0.0' \
  '/usr/local/bin/helm install ghost foo/ghost --version 1.0.0' \
  'sudo -E helm install ghost foo/ghost --version 1.0.0' \
  'env A=b helm install ghost foo/ghost --version 1.0.0' \
  'sudo helm repo add ghost https://example.com/ghost' \
  'echo hi | helm install ghost foo/ghost --version 1.0.0'; do
  new_repo hidden
  printf '%s\n%s\n' "$GOOD" "$line" > nefcloud/t/helm
  commit_all
  run_dry hidden
  check "dies: $line" fails_with "must be the first word of its line"
done

section "other lines are skipped, even with \$(...) or operators"
new_repo noise
cat > nefcloud/t/helm <<'EOF'
helm repo add foo https://example.com/charts
export PW=$(openssl rand -hex 8)
kubectl get pod | grep -v x > /dev/null
kubectl get secret -o jsonpath="{.data.x}" | base64 -d
brew install helm
snap install helm --classic
sudo apt-get install -y helm
kubectl get pods | grep helm
helm list -A | grep x
helm search repo x | head -n 3
helm get values x -o jsonpath='{.a}' $HOME
helm repo update && echo done
helm install real foo/real --version 1.0.0
EOF
commit_all
run_dry noise
check "guide noise skipped, release generated" succeeds_with \
  "skipping (not helm): export PW=" "skipping (not helm): brew install helm" \
  "skipping (not helm): kubectl get pods | grep helm" "kind: HelmRelease"

new_repo expansion
# The helm file must contain a literal $V, so no expansion here.
# shellcheck disable=SC2016
printf 'helm repo add foo https://example.com/charts\nhelm install real foo/real --version "$V"\n' > nefcloud/t/helm
commit_all
run_dry expansion
# shellcheck disable=SC2016
check "expansion in a helm command names the whole command" fails_with \
  'shell expansion is not supported (in: helm install real foo/real --version $V)'

section "operators in commands that are acted on"
new_repo chain
printf 'helm repo add foo https://example.com/charts && helm repo update\nhelm install real foo/real --version 1.0.0\n' > nefcloud/t/helm
commit_all
run_dry chain
check "&& chain rejected, message shows it intact" fails_with \
  "helm repo add foo https://example.com/charts && helm repo update" "put each helm command on its own line"

new_repo pipe
printf 'helm repo add foo https://example.com/charts | tee /dev/null\nhelm install real foo/real --version 1.0.0\n' > nefcloud/t/helm
commit_all
run_dry pipe
check "operator in 'repo add' rejected" fails_with "shell operators" "one helm command per line"

new_repo quoted
printf 'helm repo add foo "https://example.com/charts?a=1&b=2"\nhelm install real foo/real --version ">=1.0.0 <2.0.0"\n' > nefcloud/t/helm
commit_all
run_dry quoted
check "quoted & < > are plain data" succeeds_with "chart foo/real >=1.0.0 <2.0.0"

section "CRLF helm file"
new_repo crlf
printf 'helm repo add foo https://example.com/charts\r\nhelm install real foo/real \\\r\n  --version 1.0.0\r\n' > nefcloud/t/helm
printf 'nefcloud/t/helm -text\n' > .gitattributes
commit_all
run_dry crlf
check "CRLF and line continuation" succeeds_with "chart foo/real 1.0.0"
check "no carriage return reaches the output" lacks $'\r'

section "kustomization editing"
new_repo flow
printf '%s\n' "$GOOD" > nefcloud/t/helm
printf 'apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nresources: [secrets.yaml]\n' > nefcloud/t/kustomization.yaml
commit_all
run_dry flow
check "flow-style list rejected, no duplicate key" fails_with "must be a block list"

new_repo commented
printf '%s\n' "$GOOD" > nefcloud/t/helm
printf 'apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nresources: # files\n  - secrets.yaml\n' > nefcloud/t/kustomization.yaml
commit_all
run_dry commented
kust_diff="$(sed -n '/^diff .*nefcloud\/t\/kustomization.yaml/,/^diff .*helmre/p' <<< "$OUT")"
check "commented header accepted" succeeds_with "+  - helmrelease.yaml"
OUT="$kust_diff"
check "commented header gets no second resources: key" lacks "+resources:"

new_repo dupkeys
printf '%s\n' "$GOOD" > nefcloud/t/helm
printf 'resources:\n  - a.yaml\nresources:\n  - b.yaml\n' > nefcloud/t/kustomization.yaml
commit_all
run_dry dupkeys
check "duplicate resources: keys rejected" fails_with "more than one top-level resources"

new_repo unindented
printf '%s\n' "$GOOD" > nefcloud/t/helm
printf 'resources:\n- a.yaml\n- b.yaml\nnamePrefix: x-\n' > nefcloud/t/kustomization.yaml
commit_all
run_dry unindented
check "unindented '- item' style keeps its indent" succeeds_with "+- helmrepo.yaml" "+- helmrelease.yaml"

new_repo dotslash
printf '%s\n' "$GOOD" > nefcloud/t/helm
printf 'apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nresources:\n  - ./t\n  - u/\n' > nefcloud/kustomization.yaml
commit_all
run_dry dotslash
not_listed_twice() {
  [ "$RC" -eq 0 ] && ! grep -q '^+  - t$' <<< "$OUT"
}
check "./t counts as t, so it is not listed twice" not_listed_twice

section "warnings"
new_repo stale
printf 'helm repo add foo https://example.com/charts\nhelm install app foo/app --namespace mon --create-namespace --version 1.0.0\n' > nefcloud/t/helm
commit_all
stale_bare="$(bare_of stale)"
OPEN_PR=0 REPO_URL="$stale_bare" "$SH" "$SCRIPT" > /dev/null 2>&1
# "merge" the sync PR, then drop --create-namespace on main
git -C "$stale_bare" update-ref refs/heads/main refs/heads/flux-helm-sync
git clone -q "$stale_bare" "$WORK/repos/stale.w"
(
  cd "$WORK/repos/stale.w" || exit 2
  printf 'helm repo add foo https://example.com/charts\nhelm install app foo/app --namespace mon --version 1.0.0\n' > nefcloud/t/helm
  git commit -qam drop
  git push -q origin main
)
OUT="$(DRY_RUN=1 REPO_URL="$stale_bare" "$SH" "$SCRIPT" 2>&1)"
RC=$?
check "a namespace.yaml that is no longer generated is flagged, not removed" succeeds_with \
  "warning: nefcloud/t/namespace.yaml is no longer generated" "manifests up to date"

new_repo unpinned
printf 'helm repo add foo https://example.com/charts\nhelm install real foo/real\n' > nefcloud/t/helm
commit_all
run_dry unpinned
check "unpinned chart warns" succeeds_with "warning: nefcloud/t/helm: release real has no --version"

section "cross-directory collisions"
new_repo ns_clash
printf 'helm repo add foo https://example.com/charts\nhelm install aa foo/a --namespace mon --create-namespace --version 1.0.0\n' > nefcloud/t/helm
printf 'helm repo add bar https://example.com/other\nhelm install bb bar/b --namespace mon --create-namespace --version 1.0.0\n' > nefcloud/u/helm
commit_all
run_dry ns_clash
check "--create-namespace for the same namespace in two directories" fails_with \
  "Namespace mon is also generated from nefcloud/t/helm" "--create-namespace in only one"

new_repo repo_clash
printf 'helm repo add foo https://example.com/charts\nhelm install aa foo/a --namespace kube-system --version 1.0.0\n' > nefcloud/t/helm
printf 'helm repo add foo https://example.com/charts\nhelm install bb foo/b --namespace kube-system --version 1.0.0\n' > nefcloud/u/helm
commit_all
run_dry repo_clash
check "same repo name and namespace in two directories" fails_with \
  "HelmRepository kube-system/foo is also generated from nefcloud/t/helm" "another name"

new_repo release_clash
printf 'helm repo add foo https://example.com/charts\nhelm install aa foo/a --namespace kube-system --version 1.0.0\n' > nefcloud/t/helm
printf 'helm repo add foo2 https://example.com/charts\nhelm install aa foo2/b --namespace kube-system --version 1.0.0\n' > nefcloud/u/helm
commit_all
run_dry release_clash
check "same release name and namespace in two directories" fails_with \
  "HelmRelease kube-system/aa is also generated from nefcloud/t/helm"

new_repo sharing
printf 'helm repo add foo https://example.com/charts\nhelm install aa foo/a --namespace mon --create-namespace --version 1.0.0\nhelm install ab foo/a --namespace mon --create-namespace --version 1.0.0\n' > nefcloud/t/helm
printf 'helm repo add foo2 https://example.com/charts\nhelm install bb foo2/b --namespace mon --version 1.0.0\n' > nefcloud/u/helm
commit_all
run_dry sharing
check "sharing within a directory, or using a namespace without creating it, is fine" succeeds_with "chart foo2/b"
if command -v kubectl > /dev/null 2>&1; then
  sharing_bare="$(bare_of sharing)"
  OPEN_PR=0 REPO_URL="$sharing_bare" "$SH" "$SCRIPT" > /dev/null 2>&1
  git clone -q -b flux-helm-sync "$sharing_bare" "$WORK/repos/sharing.out"
  OUT="$(kubectl kustomize "$WORK/repos/sharing.out/nefcloud" 2>&1)"
  RC=$?
  check "kubectl kustomize renders the generated tree" succeeds_with "kind: HelmRelease" "kind: Namespace"
else
  skip "kubectl kustomize render (kubectl not installed)"
fi

section "configuration"
new_repo config
printf '%s\n' "$GOOD" > nefcloud/t/helm
commit_all
run_dry config PR_DRAFT=yes
check "PR_DRAFT=yes rejected" fails_with "PR_DRAFT must be true or false"
run_dry config 'PR_BRANCH=a"b'
check "odd PR_BRANCH rejected" fails_with "invalid PR_BRANCH"
run_dry config FLUX_VERSION=0.0.1
check "FLUX_VERSION mismatch rejected" fails_with "flux 0.0.1 is required, found: flux version"
flux_version="$(flux --version | awk '{print $NF}')"
run_dry config FLUX_VERSION="$flux_version"
check "FLUX_VERSION match accepted" succeeds_with "using flux version $flux_version"

section "signals stop the run and clean up"
if command -v perl > /dev/null 2>&1; then
  mkdir -p "$WORK/fakebin"
  # flux that answers --version at once and hangs on anything else
  cat > "$WORK/fakebin/flux" <<'EOF'
#!/bin/sh
[ "$1" = --version ] && { echo "flux version 9.9.9"; exit 0; }
sleep 2
EOF
  chmod +x "$WORK/fakebin/flux"
  new_repo signal
  printf '%s\n' "$GOOD" > nefcloud/t/helm
  commit_all
  for sig in TERM INT HUP; do
    case "$sig" in TERM) want=143 ;; INT) want=130 ;; HUP) want=129 ;; esac
    find "$TMPDIR" -mindepth 1 -delete
    # perl resets SIGINT: a background job of a non-interactive shell ignores it,
    # and a shell cannot trap a signal that was ignored on entry.
    perl -e '$SIG{INT} = "DEFAULT"; exec @ARGV' \
      env PATH="$WORK/fakebin:$PATH" DRY_RUN=1 REPO_URL="$WORK/repos/signal" "$SH" "$SCRIPT" > /dev/null 2>&1 &
    pid=$!
    sleep 1
    kill "-$sig" "$pid"
    wait "$pid" 2> /dev/null
    rc=$?
    leftover="$(find "$TMPDIR" -mindepth 1 -maxdepth 1 | wc -l)"
    OUT="exit $rc (want $want), $leftover leftover in \$TMPDIR"
    if [ "$rc" = "$want" ] && [ "$leftover" = 0 ]; then ok "SIG$sig: exit $rc, workdir removed"; else bad "SIG$sig"; fi
  done
else
  skip "signal handling (perl not installed)"
fi

section "pull request via gh (stub)"
mkdir -p "$WORK/ghbin"
# Records its arguments and answers according to $GH_SCEN.
cat > "$WORK/ghbin/gh" <<'EOF'
#!/bin/sh
{ echo "---"; for a in "$@"; do printf '%s\n' "$a"; done; } >> "$GH_LOG"
case "$*" in
  *"state=open&head="*)
    [ "$GH_SCEN" = list-fails ] && { echo "gh: HTTP 401: Bad credentials" >&2; exit 1; }
    case "$GH_SCEN" in existing*) echo "https://github.com/o/r/pull/42" ;; esac
    exit 0 ;;
  *"-X PATCH"*)
    [ "$GH_SCEN" = existing-patch-fails ] && { echo "gh: HTTP 403" >&2; exit 1; }
    exit 0 ;;
  *"-X POST"*)
    [ "$GH_SCEN" = post-fails ] && { echo "gh: Validation Failed (HTTP 422): A pull request already exists" >&2; exit 1; }
    echo "https://github.com/o/r/pull/7"
    exit 0 ;;
esac
exit 0
EOF
chmod +x "$WORK/ghbin/gh"
GH_LOG="$WORK/gh.log"
new_repo pr
printf 'helm repo add foo https://example.com/charts\nhelm install real foo/real\n' > nefcloud/t/helm
commit_all

# pr_run <scenario> [VAR=value ...]: a real (not dry) run against a fresh bare remote.
pr_run() {
  local scenario="$1"
  shift
  : > "$GH_LOG"
  OUT="$(env "$@" OWNER=o REPO=r GH_SCEN="$scenario" GH_LOG="$GH_LOG" PATH="$WORK/ghbin:$PATH" \
    GITHUB_TOKEN=token REPO_URL="$(bare_of pr)" "$SH" "$SCRIPT" 2>&1)"
  RC=$?
}
logged() {
  grep -qx -- "$1" "$GH_LOG"
}
logged_text() {
  grep -q -- "$1" "$GH_LOG"
}
gh_not_called() {
  [ ! -s "$GH_LOG" ]
}
no_post() {
  ! logged_text POST
}

pr_run none PR_DRAFT=true
check "opens the PR" succeeds_with "opened PR https://github.com/o/r/pull/7"
check "draft is sent as a typed boolean" logged "draft=true"
check "warnings reach the PR description" logged_text "Warnings:"
pr_run none
check "draft defaults to false" logged "draft=false"

pr_run existing
check "open PR is reused" succeeds_with "PR already open: https://github.com/o/r/pull/42"
check "its base is reset through PATCH" logged "repos/o/r/pulls/42"
check "no second PR is opened" no_post

pr_run list-fails
check "a failed lookup stops the run with the API's message" fails_with "Bad credentials"
check "and never falls through to opening a PR" no_post

pr_run post-fails
check "a failed PR creation shows the API's reason" fails_with "A pull request already exists"

pr_run existing-patch-fails
check "a failed base reset is only a warning" succeeds_with "warning: could not set the PR base"

pr_run none OPEN_PR=0
check "OPEN_PR=0 pushes the branch" succeeds_with "pushed flux-helm-sync"
check "OPEN_PR=0 never calls gh" gh_not_called

OUT=""
echo
echo "[$SH] passed=$pass failed=$fail"
[ "$fail" = 0 ]
