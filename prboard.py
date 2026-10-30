#!/usr/bin/env python3
"""
prboard — read-only "what's ready to merge?" board for PRs you review.

Phase 1: DISCOVERS candidate PRs (GitHub review-requests + an optional Slack
channel feed), evaluates every safety gate, and prints a verdict per PR.
It NEVER approves, merges, or changes anything. Stdlib only — no pip install.

Usage:
    export GITHUB_TOKEN=$(gh auth token)      # or a fine-grained PAT
    ./prboard.py                              # GitHub review-requested feed
    ./prboard.py --slack                      # also scan the Slack channel
    ./prboard.py --repo vmockinc/cmc-student-view   # one repo's open PRs
    ./prboard.py --pr https://github.com/vmockinc/cmc-student-view/pull/1017
    ./prboard.py --json                       # machine-readable

Config (optional): ~/.config/prboard/config.json  — see config.example.json.
Env it reads: GITHUB_TOKEN, GITHUB_API_URL (default https://api.github.com),
              SLACK_TOKEN, PRBOARD_SLACK_CHANNEL.
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from fnmatch import fnmatch

# ----------------------------------------------------------------------------
# Defaults (override any of these in the JSON config file)
# ----------------------------------------------------------------------------
DEFAULT_CONFIG = {
    # Base branches that must ALWAYS be handled by a human — never auto later.
    "manual_base_branches": ["master", "prod", "production", "main"],
    # Globs that, if any changed file matches, force a manual review.
    "sensitive_paths": [
        ".github/workflows/*",
        ".github/*",
        "**/Dockerfile",
        "Dockerfile*",
        "**/*.tf",
        "**/migrations/*",
        "**/migration/*",
        "**/*.env*",
        "**/secrets*",
        "package.json",
        "**/package.json",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "**/deployment*.y*ml",
        "**/helm/**",
        "**/k8s/**",
    ],
    # Diff-size thresholds above which a PR is treated as too big to auto-trust.
    "max_changed_files": 40,
    "max_changed_lines": 800,
    # Slack channel to scan for PR links (id, not name). Default: cmc-deployments-team.
    "slack_channel": "C043639SVEY",
    # How many recent Slack messages to scan.
    "slack_scan_limit": 200,
    # Default required approvals when branch protection can't be read.
    "default_required_approvals": 1,

    # --- GATE RULES: the editable checklist -------------------------------
    # Each rule forces a PR to MANUAL (you handle it) or SKIP (ignore it) when
    # ALL predicates in its "when" match. Add/remove rules freely — no code
    # changes needed. Supported predicates (combine any; all must match):
    #   base_in / base_not_in         : [branch names]
    #   repo_in / repo_not_in         : ["owner/repo", ...]
    #   files_match                   : [glob, ...]  (any changed file matches)
    #   changed_files_gt              : integer
    #   changed_lines_gt              : integer  (additions + deletions)
    #   author_is_bot                 : true/false
    #   author_association_in         : ["NONE","FIRST_TIME_CONTRIBUTOR",...]
    #   labels_any                    : [label, ...]  (case-insensitive)
    #   title_regex                   : "regex"
    "gate_rules": [
        {"name": "targets protected branch", "action": "manual",
         "when": {"base_in": ["master", "prod", "production", "main"]}},
        {"name": "touches sensitive paths", "action": "manual",
         "when": {"files_match": [
             ".github/workflows/*", ".github/*", "**/Dockerfile", "Dockerfile*",
             "**/*.tf", "**/migrations/*", "**/migration/*", "**/*.env*",
             "**/secrets*", "package.json", "**/package.json",
             "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
             "**/deployment*.y*ml", "**/helm/**", "**/k8s/**"]}},
        {"name": "large diff (files)", "action": "manual",
         "when": {"changed_files_gt": 40}},
        {"name": "large diff (lines)", "action": "manual",
         "when": {"changed_lines_gt": 800}},
        {"name": "bot author", "action": "manual",
         "when": {"author_is_bot": True}},
        {"name": "external author", "action": "manual",
         "when": {"author_association_in":
                  ["NONE", "FIRST_TIMER", "FIRST_TIME_CONTRIBUTOR"]}},
        {"name": "hold / WIP label", "action": "skip",
         "when": {"labels_any":
                  ["wip", "do not merge", "do-not-merge", "hold", "on hold"]}},
    ],

    # --- action mode (only used with --apply) ---------------------------
    "enabled": True,                 # master kill switch; false = never act
    "enable_auto_merge": False,      # must be true before it will MERGE
    "merge_method": "squash",        # squash | merge | rebase
    "audit_log": "~/.local/share/prboard/audit.log",
    "wait_timeout_seconds": 1800,    # how long to wait for checks to go green
    "wait_interval_seconds": 30,
}

GITHUB_PR_URL_RE = re.compile(
    r"https?://github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)"
)

# Verdict labels and the order they sort in (most "needs you" first).
VERDICT_ORDER = [
    "READY_TO_MERGE",
    "NEEDS_YOUR_APPROVAL",
    "MANUAL_REVIEW",
    "CHANGES_REQUESTED",
    "CONFLICT",
    "CHECKS_FAILED",
    "CHECKS_PENDING",
    "APPROVED_WAITING",
    "MERGEABILITY_UNKNOWN",
    "SKIP_OWN_PR",
    "DRAFT_OR_HOLD",
    "ERROR",
]


# ----------------------------------------------------------------------------
# Small HTTP helpers (stdlib urllib — zero dependencies)
# ----------------------------------------------------------------------------
class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message


def _request(url, headers, method="GET", data=None):
    body = json.dumps(data).encode("utf-8") if data is not None else None
    hdrs = dict(headers)
    if body is not None:
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, headers=hdrs, method=method, data=body)
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode("utf-8")
            parsed = json.loads(raw) if raw else None
            return parsed, dict(resp.headers)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read().decode("utf-8")).get("message", "")
        except Exception:
            pass
        raise ApiError(e.code, detail or e.reason)
    except urllib.error.URLError as e:
        raise ApiError(0, str(e.reason))


class GitHub:
    def __init__(self, token, api_url):
        if not token:
            raise SystemExit(
                "No GitHub token. Set GITHUB_TOKEN (tip: export "
                "GITHUB_TOKEN=$(gh auth token))."
            )
        self.api = api_url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "prboard/1.0",
        }
        self._login = None

    def get(self, path, params=None):
        url = self.api + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data, _ = _request(url, self.headers)
        return data

    def get_paginated(self, path, params=None, cap=300):
        """Follow Link headers up to `cap` items."""
        params = dict(params or {})
        params.setdefault("per_page", 100)
        url = self.api + path + "?" + urllib.parse.urlencode(params)
        items = []
        while url and len(items) < cap:
            data, hdrs = _request(url, self.headers)
            if isinstance(data, dict) and "items" in data:
                items.extend(data["items"])
            elif isinstance(data, list):
                items.extend(data)
            else:
                break
            url = _next_link(hdrs.get("Link", ""))
        return items[:cap]

    def post(self, path, body):
        data, _ = _request(self.api + path, self.headers, method="POST", data=body)
        return data

    def put(self, path, body):
        data, _ = _request(self.api + path, self.headers, method="PUT", data=body)
        return data

    def login(self):
        if self._login is None:
            self._login = self.get("/user")["login"]
        return self._login


def _next_link(link_header):
    for part in link_header.split(","):
        seg = part.split(";")
        if len(seg) < 2:
            continue
        url = seg[0].strip().strip("<>")
        if 'rel="next"' in seg[1]:
            return url
    return None


# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
def load_config(path=None):
    cfg = dict(DEFAULT_CONFIG)
    candidates = []
    if path:
        candidates.append(path)
    candidates.append(
        os.path.expanduser("~/.config/prboard/config.json")
    )
    for p in candidates:
        if p and os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    cfg.update(json.load(fh))
            except Exception as e:
                print(f"warning: could not read config {p}: {e}", file=sys.stderr)
            break
    # env overrides
    if os.environ.get("PRBOARD_SLACK_CHANNEL"):
        cfg["slack_channel"] = os.environ["PRBOARD_SLACK_CHANNEL"]
    return cfg


# ----------------------------------------------------------------------------
# Discovery: where candidate PRs come from
# ----------------------------------------------------------------------------
def discover_from_github(gh, me):
    """PRs where you're a requested reviewer or mentioned, still open."""
    found = set()
    queries = [
        f"is:open is:pr review-requested:{me} archived:false",
        f"is:open is:pr mentions:{me} archived:false",
    ]
    for q in queries:
        try:
            items = gh.get_paginated("/search/issues", {"q": q}, cap=100)
        except ApiError as e:
            print(f"warning: GitHub search failed ({q}): {e}", file=sys.stderr)
            continue
        for it in items:
            m = GITHUB_PR_URL_RE.search(it.get("html_url", ""))
            if m:
                found.add((m.group(1), m.group(2), int(m.group(3))))
    return found


def discover_from_slack(cfg):
    """Scan a Slack channel's recent history for GitHub PR links."""
    token = os.environ.get("SLACK_TOKEN")
    channel = cfg.get("slack_channel")
    if not token:
        print("note: --slack given but SLACK_TOKEN not set; skipping Slack feed.",
              file=sys.stderr)
        return set()
    if not channel:
        print("note: no slack_channel configured; skipping Slack feed.",
              file=sys.stderr)
        return set()
    url = ("https://slack.com/api/conversations.history?"
           + urllib.parse.urlencode({"channel": channel,
                                     "limit": cfg.get("slack_scan_limit", 200)}))
    headers = {"Authorization": f"Bearer {token}", "User-Agent": "prboard/1.0"}
    try:
        data, _ = _request(url, headers)
    except ApiError as e:
        print(f"warning: Slack history failed: {e}", file=sys.stderr)
        return set()
    if not data or not data.get("ok"):
        print(f"warning: Slack API error: {data.get('error') if data else 'no data'}",
              file=sys.stderr)
        return set()
    found = set()
    for msg in data.get("messages", []):
        # PR links can live in text or in attachment/blocks; scan the raw blob.
        blob = json.dumps(msg)
        for m in GITHUB_PR_URL_RE.finditer(blob):
            found.add((m.group(1), m.group(2), int(m.group(3))))
    return found


# ----------------------------------------------------------------------------
# Evaluation: the safety checklist, per PR
# ----------------------------------------------------------------------------
def _latest_reviews(reviews):
    """Latest non-comment review state per user."""
    latest = {}
    for r in reviews:
        state = r.get("state")
        if state in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            user = (r.get("user") or {}).get("login")
            if user:
                latest[user] = state
    return latest


def _check_state(gh, owner, repo, sha, required_contexts):
    """
    Summarize checks for a commit. Returns (state, detail) where state is one
    of 'passing' | 'pending' | 'failing' | 'none'.
    Considers GitHub Actions check-runs AND legacy commit statuses.
    If required_contexts is given, only those gate the state.
    """
    runs = []
    try:
        cr = gh.get(f"/repos/{owner}/{repo}/commits/{sha}/check-runs")
        runs = cr.get("check_runs", []) if cr else []
    except ApiError:
        pass
    statuses = {}
    try:
        st = gh.get(f"/repos/{owner}/{repo}/commits/{sha}/status")
        for s in (st.get("statuses", []) if st else []):
            statuses[s.get("context")] = s.get("state")  # success/pending/failure/error
    except ApiError:
        pass

    def classify(name, status, conclusion):
        if status and status != "completed":
            return "pending"
        if conclusion in ("success", "neutral", "skipped"):
            return "passing"
        if conclusion in ("failure", "timed_out", "cancelled", "action_required",
                          "startup_failure", "stale"):
            return "failing"
        return "pending"

    results = {}  # name -> passing/pending/failing
    for r in runs:
        results[r.get("name")] = classify(r.get("name"), r.get("status"),
                                          r.get("conclusion"))
    for ctx, state in statuses.items():
        if state in ("success",):
            results[ctx] = "passing"
        elif state in ("pending",):
            results[ctx] = "pending"
        else:
            results[ctx] = "failing"

    if not results:
        return "none", "no checks reported"

    if required_contexts:
        relevant = {n: s for n, s in results.items() if n in required_contexts}
        # required check that hasn't reported at all -> pending
        for rc in required_contexts:
            if rc not in relevant:
                relevant[rc] = "pending"
        scope = relevant or results
    else:
        scope = results

    vals = list(scope.values())
    if any(v == "failing" for v in vals):
        n = sum(1 for v in vals if v == "failing")
        return "failing", f"{n} check(s) failing"
    if any(v == "pending" for v in vals):
        n = sum(1 for v in vals if v == "pending")
        return "pending", f"{n} check(s) pending"
    return "passing", f"{len(vals)} check(s) passing"


def _branch_protection(gh, owner, repo, base):
    """Return (required_approvals, required_contexts, known: bool)."""
    try:
        prot = gh.get(f"/repos/{owner}/{repo}/branches/{base}/protection")
    except ApiError:
        return None, None, False
    req_reviews = (prot.get("required_pull_request_reviews") or {})
    required_approvals = req_reviews.get("required_approving_review_count")
    rsc = prot.get("required_status_checks") or {}
    contexts = set(rsc.get("contexts") or [])
    for chk in (rsc.get("checks") or []):
        if chk.get("context"):
            contexts.add(chk["context"])
    return required_approvals, (contexts or None), True


def _sensitive_hits(files, patterns):
    hits = []
    for f in files:
        path = f.get("filename", "")
        for pat in patterns:
            if fnmatch(path, pat):
                hits.append(path)
                break
    return hits


def _predicate_match(key, val, ctx):
    """Evaluate one predicate against the PR context dict."""
    if key == "base_in":
        return ctx["base"] in val
    if key == "base_not_in":
        return ctx["base"] not in val
    if key == "repo_in":
        return ctx["repo"] in val
    if key == "repo_not_in":
        return ctx["repo"] not in val
    if key == "files_match":
        return bool(_sensitive_hits(ctx["files"], val))
    if key == "changed_files_gt":
        return ctx["changed_files"] > val
    if key == "changed_lines_gt":
        return ctx["changed_lines"] > val
    if key == "author_is_bot":
        return ctx["is_bot"] == bool(val)
    if key == "author_association_in":
        return ctx["author_association"] in val
    if key == "labels_any":
        want = {str(x).lower() for x in val}
        return bool(want & set(ctx["labels"]))
    if key == "title_regex":
        return re.search(val, ctx["title"] or "") is not None
    # Unknown predicate: ignore it (don't accidentally match).
    print(f"warning: unknown gate predicate '{key}' — ignored", file=sys.stderr)
    return False


def apply_gate_rules(rules, ctx):
    """
    Returns (manual_reasons, skip_reasons). A rule fires only when ALL its
    predicates match. 'manual' -> force human review; 'skip' -> ignore the PR.
    """
    manual, skip = [], []
    for rule in rules or []:
        when = rule.get("when", {})
        if when and all(_predicate_match(k, v, ctx) for k, v in when.items()):
            reason = rule.get("name", "rule")
            # Add a little detail for the common cases.
            if "files_match" in when:
                hits = _sensitive_hits(ctx["files"], when["files_match"])
                if hits:
                    reason += ": " + ", ".join(hits[:3]) + (
                        f" (+{len(hits)-3})" if len(hits) > 3 else "")
            if rule.get("action") == "skip":
                skip.append(reason)
            else:
                manual.append(reason)
    return manual, skip


def gate_rules_for(cfg):
    """Use gate_rules if present; otherwise synthesize from legacy keys."""
    if cfg.get("gate_rules") is not None:
        return cfg["gate_rules"]
    rules = [
        {"name": "targets protected branch", "action": "manual",
         "when": {"base_in": cfg.get("manual_base_branches", [])}},
        {"name": "touches sensitive paths", "action": "manual",
         "when": {"files_match": cfg.get("sensitive_paths", [])}},
        {"name": "large diff (files)", "action": "manual",
         "when": {"changed_files_gt": cfg.get("max_changed_files", 40)}},
        {"name": "large diff (lines)", "action": "manual",
         "when": {"changed_lines_gt": cfg.get("max_changed_lines", 800)}},
        {"name": "bot author", "action": "manual", "when": {"author_is_bot": True}},
        {"name": "external author", "action": "manual",
         "when": {"author_association_in":
                  ["NONE", "FIRST_TIMER", "FIRST_TIME_CONTRIBUTOR"]}},
    ]
    return rules


def evaluate_pr(gh, cfg, owner, repo, number, me):
    """Return a dict describing the PR and its verdict + reasons."""
    out = {
        "repo": f"{owner}/{repo}",
        "number": number,
        "url": f"https://github.com/{owner}/{repo}/pull/{number}",
        "verdict": "ERROR",
        "reasons": [],
        "title": "",
        "base": "",
        "approvals": "",
    }
    try:
        pr = gh.get(f"/repos/{owner}/{repo}/pulls/{number}")
    except ApiError as e:
        out["reasons"].append(f"could not fetch PR: {e}")
        return out

    out["title"] = pr.get("title", "")
    base = (pr.get("base") or {}).get("ref", "")
    out["base"] = base
    head_sha = (pr.get("head") or {}).get("sha", "")
    author = (pr.get("user") or {}).get("login", "")
    assoc = pr.get("author_association", "")
    is_draft = pr.get("draft", False)
    mergeable = pr.get("mergeable")  # True / False / None(computing)
    mergeable_state = pr.get("mergeable_state", "unknown")
    changed_files = pr.get("changed_files", 0)
    changed_lines = pr.get("additions", 0) + pr.get("deletions", 0)
    labels = [l.get("name", "").lower() for l in (pr.get("labels") or [])]

    reasons = out["reasons"]

    # --- hard skips -------------------------------------------------------
    if pr.get("state") != "open" or pr.get("merged"):
        out["verdict"] = "SKIP_OWN_PR" if author == me else "DRAFT_OR_HOLD"
        reasons.append("PR is not open / already merged")
        return out

    if author == me:
        out["verdict"] = "SKIP_OWN_PR"
        reasons.append("you authored this PR (self-approval not allowed)")
        return out

    if is_draft:
        out["verdict"] = "DRAFT_OR_HOLD"
        reasons.append("draft PR")
        return out

    # --- gather review + protection + checks ------------------------------
    try:
        reviews = gh.get_paginated(
            f"/repos/{owner}/{repo}/pulls/{number}/reviews", cap=200)
    except ApiError:
        reviews = []
    latest = _latest_reviews(reviews)
    approvals = sum(1 for s in latest.values() if s == "APPROVED")
    changes_requested = any(s == "CHANGES_REQUESTED" for s in latest.values())
    you_approved = latest.get(me) == "APPROVED"

    req_approvals, req_contexts, prot_known = _branch_protection(
        gh, owner, repo, base)
    if req_approvals is None:
        req_approvals = cfg.get("default_required_approvals", 1)
    out["approvals"] = f"{approvals}/{req_approvals}"

    check_state, check_detail = _check_state(gh, owner, repo, head_sha, req_contexts)

    # --- gate rules (the editable checklist) -------------------------------
    try:
        files = gh.get_paginated(
            f"/repos/{owner}/{repo}/pulls/{number}/files", cap=400)
    except ApiError:
        files = []
    is_bot = ((pr.get("user") or {}).get("type") == "Bot"
              or author.endswith("[bot]"))
    ctx = {
        "base": base,
        "repo": f"{owner}/{repo}",
        "files": files,
        "changed_files": changed_files,
        "changed_lines": changed_lines,
        "is_bot": is_bot,
        "author_association": assoc,
        "labels": labels,
        "title": out["title"],
    }
    red_flags, skip_reasons = apply_gate_rules(gate_rules_for(cfg), ctx)

    if skip_reasons:
        out["verdict"] = "DRAFT_OR_HOLD"
        reasons.extend(skip_reasons)
        return out

    out["red_flags"] = red_flags
    out["checks"] = check_state
    out["you_approved"] = you_approved

    # --- verdict ----------------------------------------------------------
    # Blocking conditions first (these need attention regardless of flags).
    if changes_requested:
        out["verdict"] = "CHANGES_REQUESTED"
        reasons.append("a reviewer requested changes")
        return out
    if mergeable is False or mergeable_state == "dirty":
        out["verdict"] = "CONFLICT"
        reasons.append("merge conflict with base")
        return out
    if check_state == "failing":
        out["verdict"] = "CHECKS_FAILED"
        reasons.append(check_detail)
        return out

    # If a red flag is present, it's manual no matter how green it is.
    if red_flags:
        out["verdict"] = "MANUAL_REVIEW"
        reasons.extend(red_flags)
        if check_state == "pending":
            reasons.append(check_detail)
        return out

    if check_state == "pending":
        out["verdict"] = "CHECKS_PENDING"
        reasons.append(check_detail)
        return out
    if mergeable is None or mergeable_state in ("unknown", ""):
        out["verdict"] = "MERGEABILITY_UNKNOWN"
        reasons.append("GitHub still computing mergeability — re-run shortly")
        return out

    # Green zone.
    if approvals >= req_approvals and mergeable_state in ("clean", "unstable", "has_hooks"):
        out["verdict"] = "READY_TO_MERGE"
        reasons.append(f"{approvals}/{req_approvals} approvals, {check_detail}, "
                       f"state={mergeable_state}")
        return out

    if you_approved:
        out["verdict"] = "APPROVED_WAITING"
        reasons.append(f"you approved; waiting on {req_approvals - approvals} more")
        return out

    out["verdict"] = "NEEDS_YOUR_APPROVAL"
    reasons.append(f"green, {approvals}/{req_approvals} approvals, {check_detail}")
    if mergeable_state == "behind":
        reasons.append("branch is behind base (would need update before merge)")
    return out


# ----------------------------------------------------------------------------
# Action engine (only runs under --apply; writes to GitHub)
# ----------------------------------------------------------------------------
def _audit(cfg, line):
    path = os.path.expanduser(cfg.get("audit_log") or "")
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        import datetime
        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {line}\n")
    except Exception as e:
        print(f"warning: could not write audit log: {e}", file=sys.stderr)


def wait_until_actionable(gh, cfg, owner, repo, number, me):
    """Poll a single PR until its verdict is terminal (not pending/unknown)."""
    import time
    timeout = cfg.get("wait_timeout_seconds", 1800)
    interval = cfg.get("wait_interval_seconds", 30)
    deadline = time.time() + timeout
    while True:
        ev = evaluate_pr(gh, cfg, owner, repo, number, me)
        if ev["verdict"] not in ("CHECKS_PENDING", "MERGEABILITY_UNKNOWN"):
            return ev
        if time.time() >= deadline:
            ev["action"] = "timeout waiting for checks to finish"
            return ev
        time.sleep(interval)


def apply_pr(gh, cfg, owner, repo, number, me, dry_run=False):
    """
    Approve and/or merge a PR — but ONLY if it passes every gate. Re-evaluates
    at action time and pins the head SHA so a race (new push) fails safely.
    Returns the evaluation dict with an added 'action' field.
    """
    # Kill switches.
    if os.environ.get("PRBOARD_DISABLE"):
        ev = evaluate_pr(gh, cfg, owner, repo, number, me)
        ev["action"] = "disabled (PRBOARD_DISABLE set)"
        return ev
    if not cfg.get("enabled", True):
        ev = evaluate_pr(gh, cfg, owner, repo, number, me)
        ev["action"] = "disabled (config.enabled = false)"
        return ev

    ev = evaluate_pr(gh, cfg, owner, repo, number, me)
    verdict = ev["verdict"]
    actions = []

    # The ONLY verdicts we ever act on. MANUAL_REVIEW (incl. master/prod,
    # sensitive paths, big diffs, bots) is deliberately excluded — never acted on.
    if verdict not in ("NEEDS_YOUR_APPROVAL", "READY_TO_MERGE", "APPROVED_WAITING"):
        ev["action"] = f"no action ({verdict})"
        _audit(cfg, f"{owner}/{repo}#{number} SKIP {verdict}")
        return ev

    # Pin the exact commit we're acting on.
    pr = gh.get(f"/repos/{owner}/{repo}/pulls/{number}")
    head_sha = (pr.get("head") or {}).get("sha", "")

    # Approve if it's green, flag-free, and you haven't approved yet.
    approved_this_run = False
    if verdict == "NEEDS_YOUR_APPROVAL" and not ev.get("you_approved"):
        if dry_run:
            actions.append(f"would APPROVE @ {head_sha[:7]}")
            approved_this_run = True
        else:
            try:
                gh.post(f"/repos/{owner}/{repo}/pulls/{number}/reviews",
                        {"event": "APPROVE", "commit_id": head_sha})
                actions.append(f"APPROVED @ {head_sha[:7]}")
                _audit(cfg, f"{owner}/{repo}#{number} APPROVE {head_sha}")
            except ApiError as e:
                ev["action"] = f"approve failed: {e}"
                _audit(cfg, f"{owner}/{repo}#{number} APPROVE_FAIL {e}")
                return ev
            # Re-evaluate: approval may now satisfy the required count.
            ev = evaluate_pr(gh, cfg, owner, repo, number, me)
            verdict = ev["verdict"]

    # Decide merge-readiness. In dry-run we can't re-read live state (nothing
    # was posted), so simulate the effect of the approval we would have made.
    if dry_run and approved_this_run:
        try:
            x, y = ev.get("approvals", "0/1").split("/")
            would_approvals = int(x) + (0 if ev.get("you_approved") else 1)
            ready = would_approvals >= int(y)
        except Exception:
            ready = False
    else:
        ready = (verdict == "READY_TO_MERGE")

    # Merge only if genuinely ready AND auto-merge is explicitly enabled.
    if ready:
        if not cfg.get("enable_auto_merge", False):
            actions.append("ready to merge; auto-merge disabled in config")
        elif dry_run:
            actions.append(f"would MERGE ({cfg.get('merge_method', 'squash')})")
        else:
            try:
                gh.put(f"/repos/{owner}/{repo}/pulls/{number}/merge",
                       {"merge_method": cfg.get("merge_method", "squash"),
                        "sha": head_sha})
                actions.append(f"MERGED ({cfg.get('merge_method', 'squash')})")
                _audit(cfg, f"{owner}/{repo}#{number} MERGE {head_sha}")
            except ApiError as e:
                actions.append(f"merge failed: {e}")
                _audit(cfg, f"{owner}/{repo}#{number} MERGE_FAIL {e}")
    elif verdict == "APPROVED_WAITING":
        actions.append("approved; waiting on other reviewer(s)")

    ev["action"] = "; ".join(actions) or "no action"
    return ev


# ----------------------------------------------------------------------------
# Output
# ----------------------------------------------------------------------------
VERDICT_LABEL = {
    "READY_TO_MERGE": "READY → merge",
    "NEEDS_YOUR_APPROVAL": "NEEDS YOU → review",
    "MANUAL_REVIEW": "MANUAL (red flag)",
    "CHANGES_REQUESTED": "CHANGES REQUESTED",
    "CONFLICT": "CONFLICT",
    "CHECKS_FAILED": "CHECKS FAILED",
    "CHECKS_PENDING": "CHECKS PENDING",
    "APPROVED_WAITING": "you approved, waiting",
    "MERGEABILITY_UNKNOWN": "computing…",
    "SKIP_OWN_PR": "skip (your PR)",
    "DRAFT_OR_HOLD": "draft/hold",
    "ERROR": "ERROR",
}


def render_table(results):
    if not results:
        print("No candidate PRs found.")
        return
    order = {v: i for i, v in enumerate(VERDICT_ORDER)}
    results.sort(key=lambda r: (order.get(r["verdict"], 99), r["repo"], r["number"]))
    rows = []
    for r in results:
        pr_id = f"{r['repo']}#{r['number']}"
        title = (r["title"][:46] + "…") if len(r["title"]) > 47 else r["title"]
        rows.append([
            VERDICT_LABEL.get(r["verdict"], r["verdict"]),
            pr_id,
            r.get("base", ""),
            r.get("approvals", ""),
            r.get("checks", ""),
            title,
        ])
    headers = ["VERDICT", "PR", "BASE", "APPR", "CHECKS", "TITLE"]
    widths = [max(len(headers[i]), max((len(row[i]) for row in rows), default=0))
              for i in range(len(headers))]
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    print(line)
    print("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        print("  ".join(str(row[i]).ljust(widths[i]) for i in range(len(row))))
    print()
    # Reasons block for anything needing attention.
    attention = [r for r in results if r["verdict"] in
                 ("READY_TO_MERGE", "NEEDS_YOUR_APPROVAL", "MANUAL_REVIEW",
                  "CHANGES_REQUESTED", "CONFLICT", "CHECKS_FAILED")]
    if attention:
        print("Details:")
        for r in attention:
            print(f"  {r['repo']}#{r['number']}  [{VERDICT_LABEL.get(r['verdict'])}]")
            for reason in r.get("reasons", []):
                print(f"     - {reason}")
            print(f"     {r['url']}")


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Read-only board of PRs you review (no actions taken).")
    ap.add_argument("--repo", action="append", default=[],
                    help="owner/repo — list that repo's open PRs (repeatable)")
    ap.add_argument("--pr", action="append", default=[],
                    help="a single PR URL to evaluate (repeatable)")
    ap.add_argument("--slack", action="store_true",
                    help="also scan the configured Slack channel for PR links")
    ap.add_argument("--no-github", action="store_true",
                    help="skip the GitHub review-requested/mentions feed")
    ap.add_argument("--json", action="store_true", help="output JSON")
    ap.add_argument("--config", help="path to a config.json")
    ap.add_argument("--apply", action="store_true",
                    help="ACT: approve/merge PRs that pass every gate (writes to GitHub)")
    ap.add_argument("--wait", action="store_true",
                    help="with --apply: wait for pending checks to finish, then act")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --apply: print what it WOULD do, change nothing")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    gh = GitHub(os.environ.get("GITHUB_TOKEN"),
                os.environ.get("GITHUB_API_URL", "https://api.github.com"))
    me = gh.login()

    candidates = set()
    if args.pr:
        for u in args.pr:
            m = GITHUB_PR_URL_RE.search(u)
            if m:
                candidates.add((m.group(1), m.group(2), int(m.group(3))))
            else:
                print(f"warning: not a PR url: {u}", file=sys.stderr)
    for rp in args.repo:
        if "/" not in rp:
            print(f"warning: --repo expects owner/repo, got {rp}", file=sys.stderr)
            continue
        owner, repo = rp.split("/", 1)
        try:
            prs = gh.get_paginated(f"/repos/{owner}/{repo}/pulls",
                                   {"state": "open"}, cap=100)
            for p in prs:
                candidates.add((owner, repo, p["number"]))
        except ApiError as e:
            print(f"warning: could not list {rp}: {e}", file=sys.stderr)

    if not args.no_github and not args.pr and not args.repo:
        candidates |= discover_from_github(gh, me)
    elif not args.no_github and not args.repo and args.pr:
        pass  # explicit PRs only
    if args.slack:
        candidates |= discover_from_slack(cfg)

    if not candidates:
        print("No candidate PRs found. (Try --slack, --repo owner/repo, or --pr URL.)")
        return 0

    results = []
    for owner, repo, number in sorted(candidates):
        if args.apply:
            if args.wait:
                ev = wait_until_actionable(gh, cfg, owner, repo, number, me)
                if ev["verdict"] in ("NEEDS_YOUR_APPROVAL", "READY_TO_MERGE",
                                     "APPROVED_WAITING"):
                    ev = apply_pr(gh, cfg, owner, repo, number, me,
                                  dry_run=args.dry_run)
            else:
                ev = apply_pr(gh, cfg, owner, repo, number, me,
                              dry_run=args.dry_run)
        else:
            ev = evaluate_pr(gh, cfg, owner, repo, number, me)
        results.append(ev)

    if args.json:
        print(json.dumps(results, indent=2))
    elif args.apply:
        mode = "DRY-RUN" if args.dry_run else "LIVE"
        print(f"\nPR apply for @{me}  —  {len(results)} PR(s)  [{mode}]\n")
        for r in results:
            print(f"  {r['repo']}#{r['number']}  [{VERDICT_LABEL.get(r['verdict'], r['verdict'])}]"
                  f"  ->  {r.get('action', 'no action')}")
            print(f"     {r['url']}")
    else:
        print(f"\nPR board for @{me}  —  {len(results)} PR(s)  "
              f"(read-only, no actions taken)\n")
        render_table(results)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
