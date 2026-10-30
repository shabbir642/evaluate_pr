# prboard

Automates the mundane half of PR review: find the PR, wait for checks, and —
only when every safety gate passes — approve and merge it. Risky PRs
(`master`/`prod`, sensitive files, big diffs, bots) are always left for you.

It has three layers you adopt in order:

- **Read-only board** (`prboard.py`) — shows a verdict per PR; takes no action.
- **Action mode** (`prboard.py --apply`) — approves/merges the safe ones.
- **Event-driven** (`prwatch.py`) — reacts the moment a PR is dropped in Slack
  mentioning you; no polling, nothing to launch.

The gate checklist is **config-driven** (`gate_rules` in `config.json`): add or
remove conditions by editing a list, no code changes.

## Repo layout

```
prboard/
  prboard.py                 engine: verdicts + approve/merge
  prwatch.py                 Slack Socket Mode listener (event trigger)
  config.example.json        copy to ~/.config/prboard/config.json and edit
  deploy/
    run-prwatch.sh           launchd wrapper (loads secrets)
    com.vmock.prwatch.plist  launchd agent
    secrets.env.example       token template
  tests/                     test_verdicts / test_actions / test_rules
                             test_failsafe / test_trigger / test_window
                             test_progress
```

## Running tests

```bash
for t in verdicts actions rules failsafe trigger window progress; do python3 tests/test_$t.py || break; done
```

No network or tokens needed — they run against a mocked GitHub (and, for
`test_trigger`, a stubbed `slack_sdk`, so no pip install either).

`test_failsafe` and `test_trigger` cover the degraded paths specifically: an
unreadable GitHub endpoint, a commit with no checks yet, a push landing between
gating and acting, a typo'd gate rule, and who is allowed to trigger a run.

## What it does

1. **Finds candidate PRs** from two feeds (union of both):
   - GitHub: open PRs where you're a requested reviewer or mentioned.
   - Slack (optional): scans `#cmc-deployments-team` for GitHub PR links —
     this catches the "someone just dropped the PR in the group" case that a
     formal review-request would miss.
2. **Evaluates each PR** against the safety checklist.
3. **Prints a verdict** per PR, worst-needs-you first, with reasons.

## Verdicts

| Verdict | Meaning |
|---|---|
| `READY → merge` | Approvals satisfied, checks green, no conflict. |
| `NEEDS YOU → review` | Green and waiting on your approval; nothing risky. |
| `MANUAL (red flag)` | Green but a red flag forces a human: `master`/`prod` base, sensitive paths, large diff, or bot/external author. |
| `CHANGES REQUESTED` | Someone requested changes. |
| `CONFLICT` | Merge conflict with base. |
| `CHECKS FAILED` | A required check failed. |
| `CHECKS PENDING` | Checks still running — wait. |
| `you approved, waiting` | You've approved; waiting on others. |
| `computing…` | GitHub hasn't finished computing mergeability; re-run shortly. |
| `skip (your PR)` | You authored it. |
| `draft/hold` | Draft or WIP/hold label. |

## The safety gates (what it checks)

- **`master` / `prod` → always MANUAL**, never flagged as auto-ready.
- Required checks green (uses branch protection's *required* checks when it can
  read them; otherwise all checks).
- No merge conflict (`mergeable` / `mergeable_state`).
- Required approval count (from branch protection) vs current approvals.
- No outstanding "changes requested".
- **Red flags force manual**: sensitive paths (`.github/workflows`, CI/CD,
  Dockerfiles, Terraform, DB migrations, secrets, dependency manifests/lockfiles,
  deploy/helm/k8s), oversized diff, bot or external author.

All thresholds and lists live in `config.json` — tune them to your repos.

## Setup

Requires Python 3.8+. No pip install (standard library only).

```bash
# 1. GitHub auth — easiest is to reuse your gh login:
export GITHUB_TOKEN=$(gh auth token)
# ...or a fine-grained PAT with: Contents (read), Pull requests (read),
#    on the vmockinc repos you review.

# 2. (optional) config
mkdir -p ~/.config/prboard
cp config.example.json ~/.config/prboard/config.json

# 3. (optional) Slack feed — a user token with channels:history / groups:history
export SLACK_TOKEN=xoxp-...
# channel id is prefilled to #cmc-deployments-team (C043639SVEY) in the config.

chmod +x prboard.py
```

## Usage

```bash
./prboard.py                 # GitHub review-requested + mentions feed
./prboard.py --slack         # also scan the Slack channel for PR links
./prboard.py --repo vmockinc/cmc-student-view     # one repo's open PRs
./prboard.py --pr https://github.com/vmockinc/cmc-student-view/pull/1017
./prboard.py --slack --json  # machine-readable, for piping
```

The read-only board shows both the `review-requested:` and `mentions:` feeds.
`--apply` uses **review-requested only** — being @-mentioned in a thread is a
question, not a request to merge. Pass `--include-mentions` to opt back in.

Discovery is limited to PRs **updated in the last 24 hours** (`updated:`, not
`created:` — a PR opened last week and pushed to an hour ago is still live).
Evaluating one PR costs ~6 GitHub calls, so this is the main lever on run time
once the feed is large. Change it with `discovery_window_hours` in config, or
per run:

```bash
./prboard.py --since-hours 72    # widen to three days
./prboard.py --since-hours 0     # no window at all
```

It never applies to an explicit `--pr URL`: if you name a PR, you get it.

A run is mostly blocking network calls, so it draws a spinner on **stderr**
saying what it is waiting on — `[2/5] vmockinc/career-service-ui#786 —
evaluating…`. It turns itself off when stderr isn't a terminal, so pipes,
`--json`, launchd and CI logs stay clean; `--no-progress` forces it off.

## Notes & limitations

- **Branch protection** may be unreadable without admin rights on the repo. If
  so, prboard falls back to `default_required_approvals` (1) and still works —
  it just can't confirm the exact required-check list. The verdicts stay
  conservative (a PR is only `READY` when approvals and checks both pass).
- `mergeable` is computed asynchronously by GitHub; a brand-new PR may show
  `computing…` for a few seconds — just re-run.
- Rate limits: each PR costs ~5 API calls. The review-requested feed is usually
  a handful of PRs, so this is well within limits.

---

# Action mode (Phase 2) — approve + merge

The same engine can now *act*, not just report. It only ever acts on PRs whose
verdict is `NEEDS_YOUR_APPROVAL` / `READY_TO_MERGE` — anything `MANUAL` (master/
prod, sensitive paths, big diff, bot/external) is **never touched**. It
re-checks every gate at action time and pins the head commit, so a push that
lands mid-flight fails safely instead of merging new code.

```bash
# ALWAYS start here: shows what it WOULD do, changes nothing.
./prboard.py --apply --dry-run

# Live: approve the safe ones. Merging also requires enable_auto_merge in config.
./prboard.py --apply

# Wait for pending checks to finish, then act:
./prboard.py --apply --wait --pr https://github.com/vmockinc/cmc-student-view/pull/1017
```

Safety controls (in `config.json`):

- `enable_auto_merge` — **false by default.** With `--apply` it will *approve*
  but not *merge* until you set this true. Flip it on only once dry-runs look right.
- `merge_method` — `squash` | `merge` | `rebase`.
- `enabled` — master kill switch; `false` = never act. Env `PRBOARD_DISABLE=1`
  does the same instantly.
- `audit_log` — every approve/merge is appended here with timestamp + SHA.
- `allow_no_checks` — **false by default.** A commit with no checks reported is
  treated as `CHECKS_PENDING`, not green; on a fresh push CI needs a moment to
  register. Set true only for repos that genuinely have no CI.

Things that deliberately fail *closed* — each resolves to `MANUAL_REVIEW`, which
the action engine never touches:

- a GitHub endpoint the gates depend on (changed files, reviews, checks) can't be read
- the changed-file list is truncated, so the path gates can't have seen everything
- a gate rule names a predicate that doesn't exist (run with `--json` to see it;
  startup also prints a warning, and `validate_gate_rules` lists every problem)

Approve and merge are pinned to the SHA the gates actually ran against, so a push
landing mid-run is rejected by GitHub rather than silently acted on.

---

# Event-driven (Phase 3) — `prwatch.py`

Instead of you running anything, `prwatch.py` reacts the moment a PR is dropped
in #cmc-deployments-team **and you're mentioned**. It then watches that one PR,
waits for its checks to go green, and runs the action engine. No polling.

It uses Slack **Socket Mode** (an outbound connection), so no public URL or
webhook is needed — it works from your Mac behind NAT. It only runs while your
Mac is awake and you're logged in; `launchd` keeps it alive and restarts it.

### Slack app setup (one time)

1. api.slack.com/apps → **Create New App** → From scratch → your VMock workspace.
2. **Socket Mode** → enable it. Generate an **App-Level Token** with scope
   `connections:write` → copy the `xapp-...` token.
3. **OAuth & Permissions** → add **Bot Token Scopes**: `channels:history` (or
   `groups:history` if the channel is private) and `chat:write` (for the DM
   it sends you). Install to workspace → copy the `xoxb-...` bot token.
4. **Event Subscriptions** → enable → subscribe to the bot event
   `message.channels` (or `message.groups` for a private channel).
5. **Invite the bot to the channel**: in #cmc-deployments-team, `/invite @YourApp`.

### Who can trigger it

A message only starts a run if it is from a **human** in the configured channel
(messages carrying `bot_id`/`app_id` are ignored, so the GitHub or Jira Slack
app posting a PR link that mentions you will not set anything off), is **not
from you**, and **@-mentions you** with a PR link in it.

Set `PRBOARD_ALLOWED_SENDERS` to a comma-separated list of Slack user ids to
narrow that to specific people. Unset means any human in the channel.

### Run it

A venv in the repo keeps the dependency somewhere launchd can reliably find it:

```bash
python3 -m venv .venv && .venv/bin/pip install slack_sdk
mkdir -p ~/.config/prboard
cp deploy/secrets.env.example ~/.config/prboard/secrets.env   # fill in tokens; chmod 600
chmod 600 ~/.config/prboard/secrets.env

# test in the foreground first (PRBOARD_DRY_RUN=1 is set in the example secrets):
export $(grep -v '^#' ~/.config/prboard/secrets.env | xargs) && ./prwatch.py
```

Drop a PR link mentioning yourself in the channel and watch the log. Once the
dry-run behaviour looks right, remove `PRBOARD_DRY_RUN` from secrets and set
`enable_auto_merge: true` in config.

### Keep it running (launchd)

Edit the paths in `deploy/com.vmock.prwatch.plist`, then:

```bash
cp deploy/com.vmock.prwatch.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.vmock.prwatch.plist
# logs: ~/Library/Logs/prwatch.out  /  .err
# stop: launchctl unload ~/Library/LaunchAgents/com.vmock.prwatch.plist
```

`deploy/run-prwatch.sh` picks the interpreter itself: `$PRBOARD_PYTHON` if set,
otherwise `$PRBOARD_DIR/.venv/bin/python3`, otherwise the first `python3` on
PATH that can actually `import slack_sdk`. If none can, it exits with
instructions rather than letting launchd restart it every 30 seconds forever.
(launchd's PATH is minimal, so the wrapper also puts `/opt/homebrew/bin` and
`/usr/local/bin` back.)

## Recommended rollout

1. `./prboard.py` for a few days — confirm the verdicts match your judgment.
2. `./prboard.py --apply --dry-run` — confirm it would act on the right PRs.
3. `prwatch.py` in the foreground with `PRBOARD_DRY_RUN=1` — confirm the trigger fires.
4. Turn on `enable_auto_merge`, drop `PRBOARD_DRY_RUN`, install the launchd agent.

Don't skip to step 4. Each step is cheap and catches a different mistake.
