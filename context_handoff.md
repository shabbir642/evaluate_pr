prboard — build context & handoff

This captures the full thinking behind this repo so it can be picked up in a fresh environment. Read this first, then README.md for usage.

Objective

Automate the mundane, repetitive part of the author's PR-review duty at VMock. Today he manually: watches #cmc-deployments-team for PRs that mention him → opens each → waits for GitHub Actions/checks → approves → then merges depending on how many approvals the repo requires. The goal is to make that hands-off while keeping the risky cases human.

The workflow being automated (the "approval ladder")

For each PR in scope:

Wait for required checks to pass (no conflicts, all green).
If not already approved by him → approve.
Merge decision:
repo requires 1 approval → after his approval, merge.
repo requires 2 approvals → if he is the 2nd approver, merge; if he's the 1st, approving is enough (leave it).
Key decisions made during design (and why)

Trigger = Slack, source of truth = GitHub. PRs are usually just dropped in #cmc-deployments-team for "whoever's free," often without a formal GitHub review request — so watching review-requested:@me alone misses them. The Slack channel is therefore the trigger; GitHub is queried for the real state.
Not the Jira route. There's a #jira-claude-update channel, but its posts are a Claude automation digest, not the Atlassian Slack app. For the Jira side (a separate idea, not built here) the real Jira API is the right source. Irrelevant to this PR tool; noted so nobody re-litigates it.
Auto-approve + merge the safe ones was the author's explicit choice (over "notify and one-tap confirm"). Accepted, but gated hard (see Safety).
master and prod (and main/production) are always MANUAL — never auto-approved or merged. Author's explicit rule.
Dropped the "no linked ticket / empty description" check — author said it's not a gate.
Gate checks are config-driven (gate_rules) so conditions can be added or removed by editing a list, without touching code.
Runtime: Python, stdlib only for the engine (no pip). prwatch.py needs slack_sdk. "Any env / portability" was explicitly dropped as a requirement.
No Claude attribution in commits/PRs (author preference).
Architecture

prboard.py — the engine. Three uses:
default: read-only board (verdict per PR, no actions).
--apply: approve/merge PRs that pass every gate.
imported by prwatch.py as a library. Talks to GitHub via REST (stdlib urllib). Discovers PRs from GitHub review-requested/mentions and optionally a Slack channel scan.
prwatch.py — Slack Socket Mode listener. On a channel message that mentions the author AND contains a GitHub PR link, it watches that one PR, waits for checks to go green, then calls the engine. DMs the author the result.
config.example.json — gate_rules (the editable checklist) + action settings.
deploy/ — launchd agent + wrapper + secrets.env.example.
tests/ — 33 tests, mocked GitHub, no network/tokens needed.
Verdicts (engine output)

READY_TO_MERGE, NEEDS_YOUR_APPROVAL, MANUAL_REVIEW, CHANGES_REQUESTED, CONFLICT, CHECKS_FAILED, CHECKS_PENDING, APPROVED_WAITING, MERGEABILITY_UNKNOWN, SKIP_OWN_PR, DRAFT_OR_HOLD.

The action engine (apply_pr) only ever acts on NEEDS_YOUR_APPROVAL, READY_TO_MERGE, APPROVED_WAITING. Everything else (incl. MANUAL_REVIEW, which covers master/prod, sensitive paths, big diffs, bots) is left untouched.

Safety model

master/prod → MANUAL, always.
gate_rules red flags force MANUAL: sensitive paths (.github/workflows, CI/CD, Dockerfiles, Terraform, migrations, secrets, dependency manifests/lockfiles, deploy/helm/k8s), large diff (files or lines), bot/external author. Editable.
Re-evaluates all gates at action time and pins the head SHA on both approve and merge, so a push landing mid-flight fails safe instead of acting on new code.
enable_auto_merge is false by default — with --apply it will approve but not merge until explicitly enabled.
Kill switch: enabled: false in config, or PRBOARD_DISABLE=1 env.
--dry-run prints intended actions, changes nothing.
Audit log of every approve/merge with timestamp + SHA.
gate_rules predicates (config-driven checklist)

base_in / base_not_in, repo_in / repo_not_in, files_match (globs), changed_files_gt, changed_lines_gt, author_is_bot, author_association_in, labels_any, title_regex. A rule fires when ALL its predicates match; action is manual or skip.

Status — DONE

Phase 1 (read-only board), Phase 2 (--apply approve/merge), Phase 3 (prwatch.py event trigger) + launchd deploy — all written.
Config-driven gate engine.
33 tests passing: tests/test_verdicts.py (13), tests/test_actions.py (14), tests/test_rules.py (6).
Status — PENDING / to verify (pick up here)

Never run against live GitHub. Built in a session with no GitHub connection, so the real approve/merge REST calls are untested end-to-end. First live run should be --apply --dry-run, then a throwaway PR.
Slack app not created yet. prwatch.py needs a Socket Mode app: app-level token (xapp-, connections:write), bot token (xoxb-, channels:history/groups:history + chat:write), event sub message.channels, and the bot invited to #cmc-deployments-team. Channel id is C043639SVEY; author's Slack id is U02LS4K29KQ.
launchd plist paths are placeholders (/Users/USERNAME/tools/prboard) — edit before installing.
Branch protection read needs repo admin; without it the engine falls back to default_required_approvals (1). Verify against a real repo whether the required-approval counts and required-check lists are being read correctly.
Module detection: VMock PRs encode module as [Career Fit][New Admin] bracket tags in the summary (the Jira components field is empty). Any module-based rule should match those tags, not components.
Behavior to confirm: the engine will merge a PR the author was only mentioned in if it already has enough approvals from others (verdict READY_TO_MERGE without his approval). Decide if that's wanted or should be restricted to PRs he personally approved.
Laptop sleep: launchd/the listener only run while the Mac is awake and logged in. True 24/7 would need a server.
Possible overlap with an existing jira-fix / jira_fix CLI the author already has — he never shared what it does. Worth checking for overlap before investing further (he rejected folder access to inspect it; ask him to paste jira-fix --help or the source).
Rollout order (do not skip)

./prboard.py read-only for a few days — confirm verdicts match judgment.
./prboard.py --apply --dry-run — confirm it would act on the right PRs.
prwatch.py foreground with PRBOARD_DRY_RUN=1 — confirm the trigger fires.
Set enable_auto_merge: true, drop PRBOARD_DRY_RUN, install the launchd agent.
Run / test

export GITHUB_TOKEN=$(gh auth token)
python3 prboard.py                 # read-only board
python3 prboard.py --apply --dry-run
python3 tests/test_verdicts.py && python3 tests/test_actions.py && python3 tests/test_rules.py
Copy