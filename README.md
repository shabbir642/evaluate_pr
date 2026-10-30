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
                             test_progress / test_mergeability
```

## Running tests

```bash
for t in verdicts actions rules failsafe trigger window progress mergeability; do python3 tests/test_$t.py || break; done
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
./prboard.py --pr vmockinc/cmc-student-view#1017   # same thing, shorthand
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

It never applies to an explicit `--pr`: if you name a PR, you get it.

### Acting on one named PR

`--pr` skips discovery entirely and works on just what you name — pair it with
`--apply` to act on a single PR:

```bash
./prboard.py --apply --dry-run --pr vmockinc/career-service-ui#833
```

Naming a PR this way **implies `--wait`**: if its checks are still running it
polls until they finish (up to `wait_timeout_seconds`, default 30 min) and then
acts. Without that, a run landing mid-CI prints `no action (CHECKS_PENDING)`
and nothing ever comes back for that PR. Pass `--no-wait` to get the old
one-shot behaviour.

Feed- and `--repo`-driven runs do **not** imply it — those can be dozens of
PRs, and the wait loop is sequential and costs ~6 API calls per poll, so one
stuck PR would block everything behind it. Use `--wait` explicitly there, or
let `prwatch` handle it: it waits on every PR, one thread each, in parallel.

It accepts a full URL (including `/files`, `#anchor` and `?query` suffixes), a
bare `github.com/owner/repo/pull/123`, another host for GitHub Enterprise, or
the `owner/repo#123` shorthand. The Slack scanner stays strict about full URLs
on purpose — it reads free-form messages, where loose matching would pick up
things that aren't PR links.

`--act-on-advisory` turns on `act_on_advisory_checks` for one run only, so you
can try it without changing stored config. `blocking_check_patterns` still
wins over it.

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

### Failed checks don't always block

A red check only blocks a merge when the base branch **requires** it. GitHub
applies branch protection when it computes `mergeable_state`, so prboard trusts
that rather than its own reading of the check list:

| `mergeable_state` | meaning | verdict |
| --- | --- | --- |
| `blocked` | a required check or review is unsatisfied | `CHECKS_FAILED` |
| `unstable` | mergeable; a **non-required** check is red | `CHECKS_ADVISORY` |
| `dirty` | merge conflict | `CONFLICT` |
| `clean` / `has_hooks` | green | proceeds |

`CHECKS_ADVISORY` means GitHub would let the merge through. The board names the
failing check in a `RED (ADVISORY)` column — which only appears when there is
something to put in it — and the action engine **ignores it** unless you set
`act_on_advisory_checks: true`.

Two globs, matched on the check's name, override that per check:

```json
"blocking_check_patterns": ["security-scan", "*e2e*"],
"advisory_check_patterns": ["guardrails", "*lint*"]
```

- `blocking_check_patterns` — never let this slide. Blocks even when GitHub
  says the check isn't required, and beats `act_on_advisory_checks`.
- `advisory_check_patterns` — known noise. Treated as advisory even when GitHub
  hasn't said the merge is unblocked. It only applies when **every** failing
  check matches, so one unexpected red check still blocks.

`blocking` wins over `advisory`. With neither set, the strict fallback is
unchanged.

Required checks are read from classic branch protection (needs admin) and, 
failing that, from repository rulesets (`/rules/branches/{branch}`, which needs
only read access). If neither can be read, a red check keeps blocking — the
strict reading, not the permissive one.

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
3. **OAuth & Permissions** → add **Bot Token Scopes**: `groups:history` and
   `chat:write` (for the DM it sends you). Install to workspace → copy the
   `xoxb-...` bot token.
4. **Event Subscriptions** → enable → subscribe to the bot event
   `message.groups`.
5. **Invite the bot to the channel**: in #cmc-deployments-team, `/invite @YourApp`.

> `#cmc-deployments-team` (C043639SVEY) is a **private** channel, which is why
> the scope is `groups:history` and the event is `message.groups` — the
> `channels:*` equivalents are for public channels and would leave the bot
> connected but never triggered. For a public channel use `channels:history`
> and `message.channels` instead.

### Who can trigger it

A message only starts a run if it is from a **human** in the configured channel
(messages carrying `bot_id`/`app_id` are ignored, so the GitHub or Jira Slack
app posting a PR link that mentions you will not set anything off), is **not
from you**, and **@-mentions you** with a PR link in it.

Set `PRBOARD_ALLOWED_SENDERS` to a comma-separated list of Slack user ids to
narrow that to specific people. Unset means any human in the channel.

### Check the setup first

A misconfigured Socket Mode app does not error — it connects, authenticates,
logs `prwatch connecting` and then stays silent. `deploy/preflight.py` checks
each link separately so a failure names itself:

```bash
set -a; . ~/.config/prboard/secrets.env; set +a; .venv/bin/python3 deploy/preflight.py
```

It verifies both tokens are the right kind, that the bot authenticates, that it
holds `groups:history` and `chat:write`, that it is actually a member of the
channel, that Socket Mode opens, and that GitHub auth works — then prints what
the process would do once triggered. It never prints a token.

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
dry-run behaviour looks right, set `PRBOARD_DRY_RUN=0` (or comment the line
out) and set `enable_auto_merge: true` in config.

The startup line says exactly what the process will do, so check it before
walking away:

```
prwatch connecting — channel C043639SVEY, reacting to <@U02LS4K29KQ> from any human [DRY-RUN]
prwatch connecting — channel C043639SVEY, reacting to <@U02LS4K29KQ> from any human [LIVE, auto-merge ON, acting on PRs with non-required checks RED]
```

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
