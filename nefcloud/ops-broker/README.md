# ops-broker: break-glass

Some fixes can't wait for a PR, or can't be made by one: a pod stuck after a strategy change, a suspended Kustomization, a release that needs a nudge. In v0 the agent (claude-remote) stops and tells you the command, and you run it in Headlamp. v1 keeps you as the one who decides, but turns "find it in Headlamp and do it" into "read one page, press Approve".

v2 does the same for GitHub. The agent's own App can push branches and open PRs, but it deliberately can't change CI workflows, repo settings or workflow runs (claude-remote `docs/design.md` §1, §3). Those were the remaining things that needed your PC. Now the agent asks, and the broker does them with a separate App whose key only the broker's token minter holds ([GitHub verbs](#github-verbs-v2)).

v3 covers what GitHub only lets *you* do, not an App: creating a repo. Approving one also sends you through GitHub's sign-in, and the action runs once with your token ([Acting as you](#acting-as-you-v3)).

## How it works

1. The agent creates an `OpsRequest` in the `ops-broker` namespace:

   ```yaml
   apiVersion: ops.nefcloud.top/v1alpha1
   kind: OpsRequest
   metadata:
     generateName: restart-
     namespace: ops-broker
   spec:
     verb: rollout-restart
     target: {kind: Deployment, namespace: valheim, name: valheim}
     reason: Stuck on the old ReplicaSet after the strategy change in valheim#12.
   ```

2. The broker checks the target exists (and, for `delete-pod`, that a controller will recreate the pod), marks it `Pending`, and sends an ntfy notification with a **Review** button.
3. The button opens `https://ops.nefcloud.top/r/<name>`, behind the same Google login as Headlamp. It shows the verb, the target and the agent's reason, with **Approve** and **Deny**.
4. On Approve the broker runs the action and writes the result to the request's status. The agent watches it with `kubectl -n ops-broker get opsrequest <name> -w`.

| Verb | Kinds | What it does |
|---|---|---|
| `delete-pod` | Pod | Deletes that exact pod (by the UID seen when the request came in, so a StatefulSet's same-named replacement is safe). Only pods a ReplicaSet, StatefulSet or DaemonSet controls; bare and Job pods may not come back. |
| `rollout-restart` | Deployment, StatefulSet, DaemonSet | What `kubectl rollout restart` does: bumps `restartedAt` in the pod template. |
| `flux-reconcile` | Kustomization, HelmRelease, GitRepository, HelmRepository | What `flux reconcile` does: bumps `reconcile.fluxcd.io/requestedAt`. No `--with-source`; request the source separately. |
| `flux-suspend` / `flux-resume` | same | Sets `spec.suspend`. |
| `create-sops-age-key` | Secret `flux-system/<app>-sops-age` | Makes a new Flux app's age key in the broker and stores it there, as cluster-infra's CLAUDE.md (SOPS) says. The request's status gives the public key, for the app's `.sops.yaml`. See [SOPS keys](#sops-keys). |
| `github-*` | a repo | See [GitHub verbs](#github-verbs-v2). |

A request is `Pending` → `Succeeded` / `Failed` / `Unknown` / `Denied` / `Expired`, or `Rejected` straight away (target missing, a pod that may not come back, too many pending). `Unknown` means the action may or may not have taken effect: check the target before asking again. Requests expire after 4 hours unless they ask for something else (`spec.ttlSeconds`, up to 12 hours). Each requester named in `PENDING_CAPS` has its own pending slots (the interactive agent: 3), and everyone else shares 3. Finished ones are kept 90 days, or 7 for `Rejected` and `Expired`, which decided nothing (`config.env`). Approving checks the target again first. If it has gone, been replaced, or moved on (a branch that has new commits, a rule someone already set), the request ends `Rejected` and nothing runs.

A request that will still be waiting at 07:00 doesn't notify you between 22:00 and 07:00 (London time); one digest at 07:00 lists them all. See [Async requesters](#async-requesters).

## GitHub verbs (v2)

GitHub requests take `spec.github` instead of `spec.target`. `repo` is a repo name under `GITHUB_OWNER` (`config.env`).

| Verb | `spec.github` | What it does |
|---|---|---|
| `github-commit-workflows` | `repo`, `branch`, `baseSHA`, `message`, `files: [{path, content}` or `{path, delete: true}]` | Commits those files, all under `.github/workflows/`, onto `branch`, as a child of `baseSHA`. `branch` must be an unprotected work branch whose head is still `baseSHA`, never the default branch. The PR and the merge stay yours. |
| `github-protect-branch` | `repo` | Protects the repo's default branch, if it has no rule yet, with the standard one: PRs only, one approval, admins exempt (so you can still merge). |
| `github-rerun-workflow` | `repo`, `runID` | Re-runs the failed jobs of a finished, failed run. |
| `github-dispatch-workflow` | `repo`, `workflow` (file name), `inputs` | Runs a `workflow_dispatch` workflow on the default branch. |
| `github-create-repo` | `repo`, `private` (default `true`), `description` | Creates `GITHUB_OWNER/<repo>` with a README as its first commit, so it has a default branch to protect. Rejected if the name is taken. |
| `github-human-review` | `repo`, `pr`, `headSHA` | Signs off a PR an agent flagged as needing a human: its `human-review` status on exactly `headSHA` turns success. Rejected at approval if the PR has moved on. See [Human review required](#human-review-required). |
| `github-require-human-review` | `repo` | Adds the standard `human-review` ruleset, which makes that check required on the default branch. Rejected if the repo has a ruleset of that name, or is in `HUMAN_REVIEW_EXCLUDE`. The broker files these itself; see [Human review required](#human-review-required). |

The page shows what approving would do, read live from GitHub: for a commit, the diff against `baseSHA`; for a run, which run it is and how it ended; for protection, the exact rule. If it can't load that, it offers only Deny.

```yaml
apiVersion: ops.nefcloud.top/v1alpha1
kind: OpsRequest
metadata: {generateName: github-commit-workflows-, namespace: ops-broker}
spec:
  verb: github-commit-workflows
  reason: CI should build arm64 too; the rest of the change is in the PR.
  github:
    repo: cv-current
    branch: ci-arm64
    baseSHA: 3f0c...   # git rev-parse origin/ci-arm64
    message: "ci: build arm64 images"
    files:
      - path: .github/workflows/image.yml
        content: |
          name: Image
          ...
```

**Setup, from the phone, once.** Open <https://ops.nefcloud.top/github> and press **Create the App on GitHub**. GitHub shows a pre-filled `martin-nef-ops`, private to `martin-nef`, with `actions`, `administration`, `contents` and `workflows` write. Confirm it there, and it sends you back to the broker, which hands GitHub's one-time code to the minter. Then install it on `martin-nef` from the link the page shows; *All repositories* is fine (below). Until then, GitHub requests are `Rejected` with a link to the setup page.

## Async requesters

The night agents (roadmap: *Night shift*) file requests while you sleep, and keep working on other things while they wait. The v1 rules were made for a human at the phone: a 15-minute expiry, and 3 pending slots shared by everyone. They don't fit that.

```yaml
spec:
  verb: rollout-restart
  target: {kind: Deployment, namespace: valheim, name: valheim}
  reason: ...
  ttlSeconds: 36000   # wait until morning; at most MAX_TTL (12 h)
```

- **Who asked comes from the API server, not the request.** A `MutatingAdmissionPolicy` (`admission.yaml`) sets `spec.requester` to `request.userInfo.username` on create, overwriting anything the request said. The validating policy then refuses any request where it isn't the creator, so if the mutating policy is ever missing or broken, requests fail closed instead of being taken at their word. The page, the list, the audit lines and `kubectl get opsrequests -o wide` show it.
- **The TTL is the requester's choice, within a cap.** `spec.ttlSeconds` (60 s to 24 h in the schema), capped by `MAX_TTL` (12 h) in the broker. Above the cap it's clamped rather than refused, and `status.expiresAt` says when it actually expires. A night agent asks for long enough to reach the morning. The interactive agent uses the default, 4 hours (`REQUEST_TTL`). It was 15 minutes, on the idea that you're at the phone, but requests kept expiring before you saw them. Approve re-checks the target, which covers most of what goes stale in 4 hours.
- **Pending caps per requester.** `PENDING_CAPS` gives named requesters slots of their own, and everyone else shares `MAX_PENDING`. A night agent with a dozen blocked streams can't take the slots the interactive agent needs, and the reverse. When a night agent gets its own ServiceAccount, it gets a line here. That's a PR to this repo: a requester can't raise its own cap.
- **Quiet hours and a morning digest.** Between `QUIET_HOURS` (in `QUIET_TZ`), a request that will still be waiting when they end is admitted but not announced (`status.notifiedAt` stays unset). When they end, one notification lists them all and links to the list. A request that would expire before morning is announced right away, since it's only worth anything now. So with the 4-hour default, a request filed before 03:00 still reaches you at night, and one filed later waits for the digest. There's no list of "async" requesters to keep in sync: the TTL a request asks for decides. Each held request is marked before the digest is sent, so a lost digest costs time (the list shows them anyway), and a repeated one can't happen.
- **Approval re-checks the world.** A request approved 8 hours after it was made may describe a world that's gone. Approve now runs the same check as admission before claiming the request. For cluster verbs, the target's UID must match the one recorded on admission, so a Deployment deleted and recreated, or a pod replaced, is a different target. For GitHub verbs, the branch must still be at `baseSHA`, the rule still absent, the run still failed, the new repo's name still free. If any of that changed, the request ends `Rejected` with the reason, and nothing runs. If the check itself can't complete (an API or GitHub outage, the minter down), nothing is decided: the request stays `Pending`, and you can press Approve again. The checks at run time stay in place for the race between this check and the action: the pod UID precondition, the non-forced ref update, the second protection read.

What it keeps from v1: approval still needs the Google login. A long TTL only means the request can wait longer for one. `spec` is still immutable and hash-checked. A request past its TTL is still `Expired`, never approved late. What a long TTL does add is a longer window for you to approve something that's no longer what you'd want. The re-check covers what the broker can see, but not every change of intent: a reason that was true at 2am may not be by 9. That's why the TTL is capped, and why the page shows when each request was made.

Decided against:

- **A separate approval page or verb set for async requesters.** Same verbs, same page. Only the timing differs.
- **Setting `requester` by a webhook, or by the broker from managedFields.** A webhook is a new service in the admission path. `managedFields.manager` is the client's own claim (any string the requester sends as `fieldManager`). The MAP is in-tree (GA in 1.36), so it adds nothing to run.
- **Quiet hours by requester.** That would be one more list to keep in sync. The TTL already says whether a request can wait.
- **Refusing a TTL above the cap.** Refusing would make an agent's request fail at 2am over a config value it can't see. Clamping, with `expiresAt` in the status, tells it the outcome instead.

Can't be tested without a cluster: the mutating policy itself. After the merge, create a request and check `kubectl -n ops-broker get opsrequest <name> -o jsonpath='{.spec.requester}'` is `system:serviceaccount:claude-remote:claude-remote`. Also check that a request with a `requester` of something else still comes back with yours. If the policy fails, every create is refused (`failurePolicy: Fail`), and Headlamp is the fallback until it's fixed.

## SOPS keys

A new Flux app needs its own age key: the private half in `flux-system/<app>-sops-age` for Flux, the public half in the app's `.sops.yaml`. Making it on someone's machine means the private key passed through that machine. Making it in an agent's container would let the agent decrypt the app's secrets. `create-sops-age-key` makes it here instead:

```yaml
spec:
  verb: create-sops-age-key
  target: {kind: Secret, namespace: flux-system, name: <app>-sops-age}
  reason: <app>'s secrets
```

On approval, the broker generates the key (`agekey.py`: X25519 and bech32 in pure Python, checked against `age-keygen` and RFC 7748), creates the Secret, and ends the request `Succeeded` with the public key in `status.message`. The private half is never logged, returned or kept. Encrypting with `sops -e` only needs the public key, so nobody needs the private one, except to decrypt a file locally later. Then `kubectl -n flux-system get secret <app>-sops-age` restores it, as before.

- **Create only.** The broker's Role in `flux-system` is `create` on Secrets, with no `get` or `list`, so it can't read the other apps' keys. An existing key is never replaced: the create fails, and the request ends `Failed` with nothing changed.
- **Pinned by admission.** RBAC can't limit `create` to a name, so `ops-broker-sops-keys-only` (`admission.yaml`) does: from the broker, only `<app>-sops-age`, with exactly one key, `age.agekey`, never an update. The CRD and the broker check the same.
- **Trade-off:** the broker can now add Secrets to `flux-system`, if they look like SOPS keys. A compromised broker could plant a key for an app that has none yet, and Flux would then decrypt that app's files with an attacker's key. That only matters for files encrypted to that key's public half, which you'd see in the app's `.sops.yaml` on a PR.

## Human review required

Agents merge their own PRs once the PR reviewer approves (roadmap: *Autonomous merge*). This is how any of them pulls you in instead, and how a flagged PR stays unmerged until you've looked.

- **Flag:** a `github-human-review` request with the PR's current head and a one-sentence reason. The interactive agent and the night shift file it like any request. The PR reviewer can't (its Jobs have no API access), so it puts `needs_human` in its review's state marker, and the broker files the request for it, as itself. Only reviews the API says are `REVIEWER_LOGIN`'s count, and only its latest one, of the current head. **You're asked once, when the PR is ready:** while the marker says `"ready": false` (the author still has required changes, or CI is red), the check is held `pending` ("human review after the required changes") but nothing is filed, so you get no ntfy. The broker files the request from the first review that says ready. A marker without `ready` (an older reviewer) files straight away.
- **The status:** every `HUMAN_REVIEW_SYNC_SECONDS` (60), the broker sets a `human-review` commit status on every open PR head of **every repo the ops App is installed on**, except `HUMAN_REVIEW_EXCLUDE`. It's `success` when the PR was never flagged, or when its newest flag was signed off for exactly this head. Otherwise it's `pending`, with why: waiting for you, denied, new commits since the sign-off, or the flag expired or was rejected unanswered. A status is only written when it changes.
- **Sign-off:** approving on the page sets `success` on that head straight away, and approves the PR on GitHub as the ops App, for exactly that head. GitHub requires one approval, and a flagged PR has none: the reviewer comments instead of approving, and the author can't approve its own PR. Without this, a sign-off on the page still left the PR unmergeable until you approved on GitHub as well. The Approve uses its own token (`pull_requests: write`, minted only for this). If it fails, the sign-off stands and the request's message says why. Approving the PR on GitHub yourself still works too. Approving re-checks the PR first: if it has moved on, nothing is signed off and the request ends `Rejected`. Deny keeps it `pending`.
- **Or approve the PR on GitHub.** If someone in `HUMAN_REVIEWERS` (you) approves a held PR's current head, the next sync (within a minute) sets `success` and marks the waiting request `Succeeded`, decided by "`<login>` (GitHub review)". The agent that asked sees the same outcome as an approval on the page, so you don't approve twice. Some details:
  - Only your latest Approve or Request changes counts; comments don't. Your Request changes after an Approve undoes it.
  - A push makes the approval stale: it's of another commit.
  - Approving overrides an earlier Deny, and a reviewer flag you've already approved isn't filed at all.
  - Only reviews the API says are yours count, and the agents' Apps review as `…[bot]`, so they can't approve as you.
- **Once flagged, always your call.** A push after your sign-off goes back to `pending`, and only a new flag and a new sign-off clear it. An expired flag is filed again if the reviewer's flag still stands. A rejected one isn't, so a full queue can't loop.
- **The guard is GitHub's.** One `human-review` ruleset makes the check required on the default branch, pinned to the ops App (`martin-nef-ops`) as its only source, so no other App or user can set it. Then no agent can merge a flagged PR, whatever it does. Repo admins bypass it, so you can always merge. The broker holds the ruleset's one definition (`githubops.ruleset`) and asks you to add it where it's missing (below).

### Every repo, with exceptions

There's no list of repos to keep up to date. Any agent can flag a PR on any repo, so the default is everything the ops App is installed on: a status on every open PR, the reviewer's flags filed (ntfy), and the ruleset, which the broker asks you to add. The one exception is `HUMAN_REVIEW_EXCLUDE` in `config.env`, for repos that are **fully automatic**. They get no status and no ruleset, the reviewer's flags aren't filed, and an agent's own `github-human-review` request is rejected at once ("its PRs merge without human review"), so it never pings you. The agent learns straight away that nobody will look, instead of waiting on a review that holds nothing. A request to add the ruleset there is rejected too. Considered: letting agents still ask on an excluded repo. But nothing would hold the PR while you looked, so one agent could wait on you while another merged.

Archived repos are skipped. So are repos with no open issues or PRs (`open_issues_count` is 0): nothing to set, and no calls spent on them.

**The ruleset, the same everywhere.** Every 10th sync pass (`RULESET_EVERY`, about every 10 minutes), the broker lists each covered repo's rulesets. For each repo with no ruleset named `human-review` and no `github-require-human-review` request yet, it files one. You approve it from the ntfy link like anything else, and the page shows the exact ruleset. That's both the rollout and how a new repo gets covered. Some limits:

- At most `RULESET_BATCH` (5) of them wait at once, so the broker's 10 pending slots stay free for flags. The rest come in the next round.
- A repo with any such request already, in any phase, isn't asked about again until retention deletes it. A **Denied** one means "not here" for 90 days, Expired or Rejected for 7. To say no for good, add the repo to `HUMAN_REVIEW_EXCLUDE`.
- It files only after a pass that set every status it had to. It also first mints (and revokes) a status-write token for that repo, which fails if the App can't write statuses there. Without that permission, a required check would hold every PR, so nothing is filed.
- Only the name is compared. A `human-review` ruleset you've edited by hand is left alone, and a fix to the definition reaches only repos that don't have it yet. To roll a change out, delete the old rulesets and let the broker ask again.

### Rollout

1. **Install the ops App on all repos, and accept its permissions.** Open <https://github.com/settings/installations>, click **Configure** next to `martin-nef-ops`. Under *Repository access*, choose **All repositories** and **Save**. If a yellow banner asks you to review new permissions (*Pull requests: Read and write*, *Commit statuses: Read and write*), accept them. Without this the sync logs a 403 for each repo, and no statuses are set, so nothing is held.
2. **Merge the PR that adds this.** Flux deploys it within a few minutes. A minute later, every open PR on every repo shows `human-review: success`. Check one:

   ```bash
   gh pr checks <pr> -R martin-nef/<repo>    # human-review  pass  not flagged
   ```

   And the broker's log:

   ```bash
   kubectl -n ops-broker logs deploy/ops-broker -c broker --since=10m | grep -i human-review
   ```

   No `403` lines. `filed ... github-require-human-review` lines are the next step starting.
3. **Approve the rulesets.** You get an ntfy per repo, up to 5 at a time and more as you approve. During quiet hours they arrive as one digest at 07:00. Each page shows the ruleset. Approve each one. To see what's waiting: <https://ops.nefcloud.top/>.
4. **Check a repo:**

   ```bash
   gh api repos/martin-nef/<repo>/rulesets -q '.[].name'    # human-review
   ```

To exempt a repo, add it to `HUMAN_REVIEW_EXCLUDE` before step 3, or delete its ruleset on GitHub afterwards (*Settings → Rules → Rulesets*).

Considered and rejected:

- **A `needs-human` label.** Anyone with `issues: write` can remove a label, the agents included.
- **The reviewer's Request changes.** The author agent has `pull_requests: write`, and can dismiss it.
- **The reviewer approving flagged PRs too**, with `human-review` alone holding them. That would also give a flagged PR its approval. But it makes the reviewer's Approve mean "fine, if a human agrees", and a mistake in the check (a repo without the ruleset) would let a flagged PR merge. An approval that only exists after your sign-off can't be ahead of it.
- **GitHub auto-merge as the gate.** It can merge before a non-Actions check has started, and it only waits for required checks, which are hard to keep right when checks are conditional. Here there's exactly one required check, the same on every repo and never conditional.
- **A list of repos to cover (`HUMAN_REVIEW_REPOS`, until #43).** It was empty after the merge of #39, so the reviewer's flags were never filed and you got no ntfy. Opting in per repo made the safe default "off". Now the default is "on" wherever the App is installed, and `HUMAN_REVIEW_EXCLUDE` is only for exceptions.
- **One ruleset for every repo at once.** GitHub only has rulesets across repos for organizations and enterprises. These repos are under a personal account, which has per-repo rulesets only. Moving them to the `nefcloud` org would allow one org ruleset, with exceptions by name or custom property. But it would mean re-pointing Flux, both Apps' installations and every clone, for what the broker's one definition already gives. If the repos ever move to the org, make this an org ruleset and drop `github-require-human-review`.
- **The broker adding rulesets by itself, without asking.** It changes a repo's settings, and every write the ops App makes is one you approved. One tap per repo, once, keeps that rule.
- **Adding the ruleset in `github-protect-branch`.** New repos would get it in the same approval, but repos that are already protected wouldn't, and that verb only ever acts where there's no rule. The broker asking for missing rulesets covers both.
- **A webhook instead of the sync (for now).** It would be quicker: a flag or your GitHub approval would land in seconds, not up to a minute. It needs the ops App's webhook pointed at a path outside the Google login (like pr-reviewer's receiver at `pr-reviewer.nefcloud.top`), with a webhook secret the broker checks. Then `pull_request` and `pull_request_review` events would each start a sync of their one repo. The minute's sync would stay as the backstop, since GitHub doesn't redeliver missed events. A minute's lag is fine meanwhile: a flagged PR is never approved by the reviewer, so it can't merge in the meantime.

Trade-offs:

- **Tokens are minted as they're needed**, each for one repo, and revoked when the step is done. The only token for every repo is the one that lists them (`metadata: read`), which can't read or change anything inside a repo. A repo with open issues or PRs gets a read-only token (`pull_requests: read`, `statuses: read`). A token that can write statuses is minted only when a status has to change, which is rare: when a PR opens, gets a new head, or is flagged or signed off. Repos with nothing open get no token at all.
- **One sync per minute** costs about 3 calls for the list token, 3 per repo with open issues or PRs (mint, PRs, revoke), and about 2 per open PR (its status, and its reviews if it isn't flagged). A status change adds a mint, a write and a revoke. The ruleset round adds a few calls per repo every 10 minutes. With a handful of active repos that's well under the App's 5,000 calls an hour.
- **A flag outlives its retention while its PR is open.** Retention deletes finished requests after 7 or 90 days, and a deleted flag would make the PR read as never flagged, so a `github-human-review` request is kept until the sync has seen its PR closed. A repo that's excluded or uninstalled keeps its flags. They're few, and deleting them is a decision for you, not a timer. (From the PR reviewer's review of #39.)
- **Every page** of open PRs and of a PR's reviews is read. A sign-off that lands during a sync pass isn't overwritten: the requests are read again before a `success` would turn `pending`.
- **The broker can create requests now**, for the reviewer's flags. That's notifications only: every request still needs you to approve it, and it has its own `PENDING_CAPS` slots (10).

## Design decisions

**The approval lives behind your login, not in the notification.** The agent has the internet on 443, so it can read and post to any ntfy topic it learns. If a notification carried an approve link or token, the agent could approve itself. Instead the notification only links to a page, and only a request carrying oauth2-proxy's `X-Auth-Request-Email` for an address in `ALLOWED_APPROVERS` can approve. The agent can reach `ops.nefcloud.top` through Cloudflare like anyone else, but it can't sign in to your Google account. Claude Code's permission prompt was never an option: it runs inside the pod the broker is meant to check.

**Requests are a CRD, not an HTTP API.** The agent already talks to the API server and to nothing else in the cluster (its NetworkPolicy refuses 10.0.0.0/8). A CRD gets authentication, authorisation, validation and storage from the API server, with no new network path and no auth code in the broker:

- The schema is the verb list. CEL rules tie each verb to its kinds, so `kubectl create` refuses anything else before the broker sees it. The broker checks again, in case the CRD is ever loosened.
- `spec` is immutable (`self == oldSelf`), so what you read on the page is what runs. The page also posts the spec's hash back, and a mismatch is refused.
- `status` is a subresource. The agent's Role is `create/get/list/watch` on `opsrequests` only, so it can't mark its own request approved, edit it, or delete it from the audit trail.
- An admission policy (`admission.yaml`) refuses `ownerReferences` and `finalizers` on new requests. Either would let a requester undo the audit trail: an owner, even a made-up one, hands the record to the garbage collector, and a finalizer blocks its cleanup and holds quota.
- A ResourceQuota caps the namespace at 200 requests, even if the broker is down. Junk (`Rejected`, `Expired`) is deleted after 7 days, so filling it takes sustained, visible effort, and the fallback is Headlamp.

**It lives in cluster-infra, code in a ConfigMap.** The broker holds cluster-wide delete and patch rights. If it lived in a repo the agent can open PRs to and Flux deploys without impersonation, one rushed merge could widen what it does. Here it sits with the sandbox it extends, on the same protected `main`. `broker.py` is stdlib Python on the stock `python:3.13-slim` image (pinned by digest, the one cv-current uses), mounted from a hashed ConfigMap, so there is no image to build and no workflow to change. An edit rolls the pod.

**Fail closed, and say so honestly.** An approved action runs once, right away, or never:

- Every status write carries the resourceVersion the broker read. `Recreate` doesn't stop a deleted pod's replacement from starting while the old one shuts down. If two brokers overlap, only one can claim an approval, and the other stops before running anything.
- If the outcome isn't known, the request ends `Unknown`, not `Failed`. That covers a timeout or 5xx from the API server, or the broker stopping after Approve but before recording the result. "Failed, request it again" could mean deleting or restarting twice. A stranded `Approved` is marked `Unknown` after a minute, and never run late.
- A 429 or 5xx while checking a new request leaves it for the next scan instead of rejecting it. Stale requests are expired before new ones are admitted, so after an outage they don't take the pending slots.

A lost ntfy message only costs time: the page lists every waiting request first. Without the ntfy Secret the broker still runs; requests just wait unannounced.

**The page trusts a header, so only Traefik may reach it.** `X-Auth-Request-Email` is set by the oauth2-proxy forwardAuth. A NetworkPolicy lets only Traefik's pods connect, as for headlamp-auth. POSTs must also carry `Origin: https://ops.nefcloud.top`, which stops cross-site form posts. The oauth2-proxy cookie is `SameSite=Lax`, which mostly does too. The page escapes everything the agent wrote and sends a strict CSP.

**GitHub: a third App, its key born in the minter.** The agent's App (`martin-nef-agent`) lacks `workflows` on purpose: with it, any branch it pushes could change a workflow and run it with the repo's secrets before anyone looks. `administration` would let it lift branch protection. Rather than widening that App, a third one holds those permissions, and its key never enters the agent's pod:

- **The broker creates the App.** The setup page posts GitHub's [App manifest](https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest), and GitHub's redirect brings a one-time code back to the broker, which passes it to the minter. The minter trades it for the key and stores it in the `ops-broker-github-app` Secret. So setup works from a phone, and the key never touches git, your PC, the agent or the broker's own process. The callback only runs behind the Google login, with a single-use `state` the same approver started within 10 minutes. The minter also checks the App GitHub made has exactly the permissions it asked for and belongs to `GITHUB_OWNER`, and stores nothing otherwise.
- **One step, one repo, least permission.** Every call gets an installation token for the request's repo only, with only what that step needs (`contents: read` to check a commit, `contents` + `workflows` write to make it), and revokes it when the step ends. Checks and the page only ever get read tokens. Installing the App on *All repositories* is fine because of this: a token can't reach a repo the request didn't name, and new repos work without another install.
- **What you see is what lands.** `spec` is immutable and hash-checked as before. The commit is built on `baseSHA`'s tree with `baseSHA` as its only parent, so its diff against its parent is exactly the page's diff. The branch update isn't forced, so if the branch moved since the request, it fails and nothing lands. Content travels in the request, not as a commit the agent pushed: the agent can't push a commit touching `.github/workflows/` at all, which is the point.
- **The merge stays yours.** Commits only go to unprotected, non-default branches. The workflow change reaches `main` through the PR you merge, like everything else. The approval matters anyway: a workflow on a branch can run on `push` with the repo's secrets as soon as it lands, before any review, and the page says so.
- **Verbs that only tighten or repeat.** `github-protect-branch` only adds the standard rule where there is none, and checks again just before writing: GitHub replaces a rule wholesale, so it never writes over one set in the meantime. A rerun repeats a run that already happened, with its commit and privileges. A dispatch runs on the default branch only, which is reviewed code where it's protected. Dispatching on a work branch was left out: it would run the branch's own scripts with the repo's secrets.
- **Same failure rules.** A 5xx or timeout on the step that changes GitHub (the ref update, the protection `PUT`, the run `POST`) ends `Unknown`. A failure before it ends `Failed`: a tree or commit that nothing points at changes nothing and is garbage-collected. Outages while checking a new request leave it for the next scan.
- **The key lives in a sidecar** ([below](#the-token-minter)).
- **Pure-Python RS256.** The stock python image has no `openssl` CLI, which the token broker in claude-remote uses to sign the App JWT. Building an image for this would mean a registry and a workflow change; signing PKCS#1 v1.5 is a few lines of `pow()`. The test compares it byte for byte with `openssl dgst -sign`. It isn't constant-time. It only signs the broker's own JWTs, and nothing outside the pod can time it.

### Acting as you (v3)

GitHub creates a repo under a user account only for that user, so `github-create-repo` runs with your token, not the App's. Approve opens a "Continue to GitHub" link to the ops App's OAuth flow. GitHub sends you back to `/github/authorize` with a one-time code, and only then is the request claimed and run.

- **The minter holds your token, briefly.** The broker only relays the code. The minter trades it using the App's client secret, which only it can read (`ops-broker-github-oauth`). It checks the token belongs to `GITHUB_OWNER`, makes the one call (`POST /user/repos`), and revokes the token in a `finally`. No user token is stored, and none reaches the broker. A user token can't be scoped to one repo, so it stays in the process that already holds the key.
- **The approval needs both logins.** The Google one for the page, and your GitHub session for the code. The `state` is single-use, tied to the approver, the request and its spec hash, and lasts 10 minutes. If you stop on GitHub, the request stays `Pending` and expires. A plain Approve, or `decide()` without a code, runs nothing.
- **Same failure rules.** Anything before the create call (code refused, wrong account, minter down) ends `Failed`. A 5xx or timeout on the create call ends `Unknown`.
- **The check can see every repo's metadata.** The name doesn't exist yet, so the scan's token can't be scoped to it. It's `metadata: read` across the installation, used for one `GET`.
- **No new App permission.** `POST /user/repos` takes the App's existing `administration: write`.

Decided against:

- **Installation tokens.** GitHub's docs list them for `POST /user/repos` too. Not used: the point is that you're the actor. It's also untested whether that creates the repo under your account.
- **Adding repos to the agent App's installation.** Not needed: `martin-nef-agent` is installed on *All repositories*, so a new repo is covered at once. The endpoint (`PUT /user/installations/{id}/repositories/{repo}`) needs a separate "installation repository access" permission. It's also unclear whether one App's user token can change another App's installation. Revisit if the agent App moves to selected repos.
- **Device flow.** It needs no redirect URI, but you'd type a code on github.com/login/device, which is worse on a phone than a link that returns by itself.

**Setup for an App made before v3.** New setups get the client secret from GitHub along with the key. An existing App needs two changes in its settings on GitHub, done once. Under *Identifying and authorizing users*, add the redirect URI `https://ops.nefcloud.top/github/authorize`, then generate a client secret and paste it on <https://ops.nefcloud.top/github>. The minter checks it with GitHub before storing it. Trade-off: the pasted secret passes through the broker's HTTP handler once, as setup's one-time code does. Alone, it gets nothing without a code from your GitHub sign-in.

### The token minter

The process that serves the approval page, parses agent-written requests and runs the cluster verbs is the part most exposed to input it doesn't control. It shouldn't also hold a key that can mint anything the ops App can do on every repo. So the App side is split out into `minter.py`, a native sidecar (an init container with `restartPolicy: Always`) in the same pod, the same pattern as claude-remote's `github-token-broker`:

- **Only the minter has the key.** It's the only container that mounts the `ops-broker-github-app` Secret. The broker's Role no longer has `get` on it, so the broker can't read it through the API either. Its code is its own ConfigMap (`ops-broker-minter`), so the minter container doesn't even mount the broker's code.
- **Tokens by verb and step, not by permission.** The broker asks for "a token for repo *R*, to *check* (or *run*) verb *V*". The minter looks the permissions up in its fixed table (`minter.TOKENS`): read-only for every check, the write set for a run. Unknown verbs or steps, and repo names that aren't one plain name, are refused. So a bug in the broker can at worst get the tokens some verb already gets, one repo at a time, valid for at most an hour. Each token mint is logged by the minter (`kubectl -n ops-broker logs deploy/ops-broker -c minter`).
- **A Unix socket, not a port.** The minter listens on `/run/minter/minter.sock` in an in-memory `emptyDir` only the two containers mount. There's nothing new for the NetworkPolicy to cover, and nothing outside the pod can reach it. One JSON request per connection, one JSON reply. The broker revokes each token itself when its step ends, with the token (`DELETE /installation/token` needs nothing else).
- **Setup moves too.** The code-for-key exchange, the check of what GitHub made, and storing the Secret are in the minter. The broker only relays GitHub's one-time code and shows the result. The manifest also comes from the minter, so what's asked for and what's accepted are defined in one place.
- **The key works right after setup.** The Secret volume is `optional`, so the pod runs before the App exists. The kubelet fills an optional Secret volume in on its next sync after the Secret appears, but rather than depend on when, the minter keeps the key it just stored in memory until the file is there. If the pod restarts first, the volume has it by then.
- **Fails closed.** If the minter is down, new GitHub requests wait for the next scan (as for any GitHub outage), approved ones end `Failed` (no token, so nothing changed), and the page offers only Deny. The broker container only starts once the socket exists (the sidecar's startup probe).

Decided against, for now:

- **Its own Deployment and ServiceAccount.** Containers in a pod share one ServiceAccount, so the API server can't tell the minter from the broker. The minter needs `create` on Secrets to store the key, so the broker keeps that permission. That's accepted: `create` isn't read, and a Secret created in this namespace reaches nothing (below). A separate Deployment would separate them fully, but costs a Service, a NetworkPolicy and a second pod for one call path, and the minter would then need its own auth instead of a socket only the pod can open.
- **Sharing code with claude-remote's token broker.** It does the same JWT and minting, but writes files on a timer for a different App, and lives in a repo the agent can open PRs to. Sharing would mean an image or a cross-repo dependency for ~100 lines of stdlib. Two small copies, each reviewed where it's used, are simpler. The RS256 test pins this copy byte for byte against `openssl`.

## Trade-offs and limits

- **`patch` is broader than the verbs.** RBAC can't say "only this annotation", so the broker's ServiceAccount can patch any field of Deployments, StatefulSets, DaemonSets and the four Flux kinds cluster-wide, and delete any pod. The code only sends the fixed patches above, and nothing but an approved request reaches that code. A ValidatingAdmissionPolicy limiting this ServiceAccount's updates to those fields would close it. It's left for later because it can't be tested without a cluster.
- **Approvals are as strong as your Google session.** oauth2-proxy cookies last 7 days. Anyone with your unlocked phone can approve. There's no second factor at approve time.
- **Who asked is stamped by admission** (see [Async requesters](#async-requesters)). Anything allowed to create OpsRequests can still file one. Today that is the agent and cluster admins.
- **The ntfy topic is a shared secret, but not a security one.** It's random and SOPS-encrypted (`secrets.yaml`), so outsiders can't read what the agent asks for. Knowing it lets someone send you fake notifications. Those can only link somewhere else, so check the link is `ops.nefcloud.top` before you sign in to anything. The agent generated this topic, so it knows it. A reserved topic with an access token on ntfy, or a self-hosted ntfy, would take that away.
- **Polling, not watching.** The broker lists requests every 5 seconds. A watch would be quicker, but it's more code to get right for no real gain at this volume.
- **Audit lives in etcd for 90 days.** Each request's status records who decided what and when, and the broker prints one JSON line per transition (`kubectl -n ops-broker logs deploy/ops-broker`). Logs go when the pod does, and there is no API server audit log on k3s here.
- **The pod may create any Secret in its namespace.** RBAC can't limit `create` to a name, so storing the App key means `create` on Secrets in `ops-broker`, and both containers share that ServiceAccount. Neither can read a Secret through the API; only the minter sees the key, through its volume. Nothing else runs in that namespace, so a Secret created there reaches nothing. The one thing the broker could do with it is create `ops-broker-github-app` before setup, with a key of its own choosing. That gets it nothing: the minter's JWTs would name an App that key isn't registered for, and GitHub refuses them.
- **Setup is the one moment the broker could get the key.** GitHub's redirect, carrying the one-time code, comes back through the broker's HTTP handler, since the minter has no port. A broker compromised at that moment could trade the code itself. Setup happens once, from a page you opened, and the code is single-use, so a broker that stole it leaves the minter with a failed setup you'd see. Routing the callback straight to the minter would need it to serve HTTP behind Traefik, which is the exposure this split removes.
- **A compromised broker could swap the request behind your GitHub sign-in.** It serves the page and the link, so it could show one repo and relay your code to the minter for another user action. The minter does only what's in `USER_ACTIONS` (today: create one repo), with the name the broker passes. So the worst case is a repo with a different name.
- **The broker still gets tokens.** A compromised broker can ask for any verb's run token on any repo the App is installed on, without an approval: the approval is checked in the broker, not in the minter. That's still much less than the key: one repo and one verb's permissions per token, an hour at most, and every mint is logged by a process the broker can't change. Having the minter check approvals itself (read the request, check it's `Approved` with a matching hash) would close it, at the cost of the minter reading the queue too.
- **The App key isn't in git.** Like the apps' `<name>-sops-age` keys, it lives only in the cluster. If it's lost (namespace deleted, cluster rebuilt), delete the App on GitHub and run the setup again. That is three taps, and safer than a restore copy somewhere.
- **The ops App is powerful.** It can change any workflow, protection rule or setting on every repo it's installed on. Only the minter holds the key, and only approved requests reach the code that writes. A bug in the broker gets scoped tokens (above); a compromise of the minter container, or of the node, would have the App's full permissions. Its commits and changes show up on GitHub as `martin-nef-ops[bot]`, and each request's status says who approved it.
- **The diff is as good as your reading of it on a phone.** Long workflow changes are hard to review on a small screen. Keep them small: the agent can split them across requests.
- **The verb list is short on purpose.** Things like removing a finalizer or scaling to zero are left out. They can lose data or take a service down, they're rare, and Headlamp still covers them. Adding a verb means a PR here that touches the CRD, the ClusterRole and `broker.py` (for a GitHub verb: `githubops.py` and its token permissions in `minter.py`; for a user action, `minter.USER_ACTIONS`).

## Later

- **An org ruleset**, if the repos move to the `nefcloud` org (see *Considered and rejected* under *Human review required*).

- More user actions (e.g. adding a repo to the agent App's installation, if it ever moves off *All repositories*): one entry in `minter.USER_ACTIONS` each.
- **Repo secrets and variables** (`secrets: write`): useful for CI that needs a token. The value must not travel through the request, which the agent can read. It needs a page where you paste it, or one the broker generates.
- **Discord** instead of, or beside, ntfy. It's planned as its own project: a generic, secure integration for the whole cluster. When it exists, `notify()` becomes one more sender. The approval stays on this page either way, for the same reason as with ntfy.
- An admission policy limiting the broker's patches (above), and its Secret creation to the one name.
- The minter checking approvals itself before it hands out a `run` token (above), so a compromised broker gets only read tokens.
- Approve from inside the ntfy app with a signed one-time link that still needs the login. Not worth it while the page is one tap away.

## Setup and checks

After merge, subscribe to the topic in the ntfy app (the PR says where to find it), and set up the GitHub App (above). Then:

```bash
kubectl -n ops-broker logs deploy/ops-broker          # "Listening ...; ntfy on"
kubectl -n ops-broker logs deploy/ops-broker -c minter   # "Listening on /run/minter/minter.sock; App set up", then one line per token
kubectl -n ops-broker get opsrequests                 # the queue and its history
cd nefcloud/ops-broker/broker && python3 -I -m unittest -v   # broker and minter against fake Kubernetes and GitHub APIs
```
