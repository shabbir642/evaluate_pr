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
import datetime
import itertools
import json
import os
import re
import sys
import threading
import time
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
    # Only look at PRs touched in the last N hours. Evaluating one candidate
    # costs ~6 GitHub calls, so narrowing discovery is what makes a run quick.
    # 0 disables the window (every open PR, however stale).
    "discovery_window_hours": 24,

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
    # A commit with no checks at all is NOT green. Set true only for repos
    # that genuinely have no CI.
    "allow_no_checks": False,
    # A red check only blocks a merge when the base branch requires it.
    # GitHub says so itself via mergeable_state == "unstable": mergeable, but
    # some NON-required check is red. prboard reports that as CHECKS_ADVISORY
    # rather than CHECKS_FAILED. Acting on one is a separate decision: leave
    # this false and advisory PRs are shown but never auto-approved.
    "act_on_advisory_checks": False,
    # Checks you never let slide, even when GitHub says they aren't required.
    # A failure matching one of these always blocks. Globs, matched on the
    # check's name (e.g. "security-scan", "*e2e*").
    "blocking_check_patterns": [],
    # Checks you consider known noise. A failure matching one of these is
    # advisory even when GitHub hasn't said the merge is unblocked — the one
    # way to relax the strict fallback, and only for checks you name.
    # blocking_check_patterns wins over this.
    "advisory_check_patterns": [],
}

# No timeout means urllib can block forever on a half-open socket, which in
# prwatch wedges the worker thread and leaves the PR stuck in _inflight.
HTTP_TIMEOUT = float(os.environ.get("PRBOARD_HTTP_TIMEOUT", "30"))

# GitHub's /files listing is paged; past this we can't be sure we saw every
# changed path, so the path gates can no longer be trusted.
FILES_CAP = 400

GITHUB_PR_URL_RE = re.compile(
    r"https?://github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)"
)


def parse_pr_ref(text):
    """
    Parse one PR reference typed on the command line.

    Deliberately more forgiving than GITHUB_PR_URL_RE, which stays strict
    because it scans free-form Slack messages where a loose pattern would
    match things that aren't PR links. Here you have named a PR on purpose.

    Accepts:
        https://github.com/owner/repo/pull/123   (plus /files, #anchor, ?query)
        github.com/owner/repo/pull/123           (no scheme)
        ghe.corp.example/owner/repo/pull/123     (other hosts)
        owner/repo/pull/123
        owner/repo#123
        owner/repo/123

    Returns (owner, repo, number), or None if it isn't a PR reference.
    """
    text = (text or "").strip()
    m = GITHUB_PR_URL_RE.search(text)
    if m:
        return m.group(1), m.group(2), int(m.group(3))

    body = re.sub(r"^[a-z][a-z0-9+.-]*://", "", text, flags=re.I)
    body = body.split("?", 1)[0]
    # The URL form is already handled above, so a '#' here is the owner/repo#N
    # separator rather than a fragment.
    parts = [p for p in body.replace("#", "/").split("/") if p]
    # A first segment containing a dot is a hostname, not an owner.
    if parts and ("." in parts[0] or parts[0] == "localhost"):
        parts = parts[1:]
    parts = [p for p in parts if p != "pull"]
    if len(parts) >= 3 and parts[2].isdigit():
        return parts[0], parts[1], int(parts[2])
    return None

# Verdict labels and the order they sort in (most "needs you" first).
VERDICT_ORDER = [
    "READY_TO_MERGE",
    "NEEDS_YOUR_APPROVAL",
    "CHECKS_ADVISORY",
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
# Progress (stderr only, and only when someone's watching)
# ----------------------------------------------------------------------------
class Progress:
    """
    A one-line spinner on stderr saying what we're waiting on.

    A run is mostly blocking network calls — ~6 per PR at roughly 0.7s each —
    so without this it looks hung. It writes to stderr, never stdout, so
    --json and pipes stay clean, and it disables itself when stderr isn't a
    terminal (pipes, launchd, CI) rather than spraying escape codes into a log.
    """

    FRAMES = "|/-\\"
    INTERVAL = 0.1

    def __init__(self, enabled=True, stream=None):
        self.stream = stream or sys.stderr
        try:
            tty = self.stream.isatty()
        except Exception:
            tty = False
        self.enabled = bool(enabled and tty)
        self._label = ""
        self._width = 0
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------
    def start(self, label=""):
        self._label = label
        if not self.enabled or self._thread is not None:
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()
        return self

    def update(self, label):
        """Change the message in place; also works while stopped."""
        with self._lock:
            self._label = label
        if self.enabled and self._thread is None:
            self.start(label)

    def stop(self):
        if self._thread is not None:
            self._stop.set()
            self._thread.join(timeout=1.0)
            self._thread = None
        self._clear()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()
        return False

    # -- internals ---------------------------------------------------------
    def _spin(self):
        for frame in itertools.cycle(self.FRAMES):
            if self._stop.is_set():
                return
            with self._lock:
                label = self._label
            self._write(f"{frame} {label}" if label else frame)
            if self._stop.wait(self.INTERVAL):
                return

    def _write(self, text):
        try:
            self.stream.write("\r" + text.ljust(self._width))
            self.stream.flush()
            self._width = max(self._width, len(text))
        except Exception:
            # A broken stderr must never take the run down with it.
            self.enabled = False

    def _clear(self):
        if not self.enabled or not self._width:
            return
        try:
            self.stream.write("\r" + " " * self._width + "\r")
            self.stream.flush()
        except Exception:
            pass
        self._width = 0


# ----------------------------------------------------------------------------
# Small HTTP helpers (stdlib urllib — zero dependencies)
# ----------------------------------------------------------------------------
class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message


def _request(url, headers, method="GET", data=None, timeout=None):
    body = json.dumps(data).encode("utf-8") if data is not None else None
    hdrs = dict(headers)
    if body is not None:
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, headers=hdrs, method=method, data=body)
    try:
        with urllib.request.urlopen(req, timeout=timeout or HTTP_TIMEOUT) as resp:
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
    except (TimeoutError, OSError) as e:
        # socket.timeout and friends can surface outside URLError; a hung
        # request must become a normal ApiError so the gates fail closed.
        raise ApiError(0, f"{type(e).__name__}: {e}")


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


def window_start(cfg, hours=None):
    """
    Start of the discovery window as an aware UTC datetime, or None when the
    window is off. `hours` overrides the configured value.
    """
    if hours is None:
        hours = cfg.get("discovery_window_hours", 24)
    try:
        hours = float(hours)
    except (TypeError, ValueError):
        return None
    if hours <= 0:
        return None
    return (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(hours=hours))


def _parse_gh_time(value):
    """Parse a GitHub ISO-8601 timestamp into an aware UTC datetime."""
    try:
        return datetime.datetime.strptime(
            value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return None


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
        if not os.path.isfile(path):
            raise SystemExit(f"--config: no such file: {path}")
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
def discover_from_github(gh, me, include_mentions=True, since=None):
    """
    PRs where you're a requested reviewer, still open.

    `mentions:` is much broader — it catches any PR where someone @-mentioned
    you in a comment, which is a question, not a request to approve and merge.
    Callers that act on the result should leave it out.

    `since` (aware UTC datetime) restricts the search to PRs updated inside the
    window. GitHub applies `updated:` server-side, so this cuts the work before
    we ever spend the ~6 calls it takes to evaluate a candidate. It matches on
    *updated*, not created: a PR opened last week and pushed to an hour ago is
    still live and still yours to review.
    """
    found = set()
    window = f" updated:>={since.strftime('%Y-%m-%dT%H:%M:%SZ')}" if since else ""
    queries = [f"is:open is:pr review-requested:{me} archived:false{window}"]
    if include_mentions:
        queries.append(f"is:open is:pr mentions:{me} archived:false{window}")
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


def discover_from_slack(cfg, since=None):
    """
    Scan a Slack channel's recent history for GitHub PR links.

    `since` becomes conversations.history's `oldest`, so Slack trims the page
    for us rather than us pulling slack_scan_limit messages and discarding the
    stale ones.
    """
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
    params = {"channel": channel, "limit": cfg.get("slack_scan_limit", 200)}
    if since:
        params["oldest"] = f"{since.timestamp():.6f}"
    url = "https://slack.com/api/conversations.history?" + urllib.parse.urlencode(params)
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
    of 'passing' | 'pending' | 'failing' | 'none' | 'error', plus the failing
    and the still-running check names, so the caller can decide which ones
    actually matter and whether the picture is even complete yet.
    'none' means the commit genuinely has no checks; 'error' means we could not
    find out. Neither is ever treated as green by the caller.
    Considers GitHub Actions check-runs AND legacy commit statuses.
    If required_contexts is given, only those gate the state.
    """
    runs, runs_err = [], None
    try:
        cr = gh.get(f"/repos/{owner}/{repo}/commits/{sha}/check-runs")
        runs = cr.get("check_runs", []) if cr else []
    except ApiError as e:
        runs_err = e
    statuses, statuses_err = {}, None
    try:
        st = gh.get(f"/repos/{owner}/{repo}/commits/{sha}/status")
        for s in (st.get("statuses", []) if st else []):
            statuses[s.get("context")] = s.get("state")  # success/pending/failure/error
    except ApiError as e:
        statuses_err = e

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
        # An endpoint we could not read might have been hiding a failing check.
        err = runs_err or statuses_err
        if err is not None:
            return "error", f"could not read checks ({err})", [], []
        return "none", "no checks reported", [], []

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
    failing = sorted(n for n, v in scope.items() if v == "failing" and n)
    pending = sorted(n for n, v in scope.items() if v == "pending" and n)
    if failing:
        # 'failing' outranks 'pending' for the headline state — a failed
        # required check will not un-fail — but the pending names go back too,
        # so the caller can tell the picture is still incomplete.
        return "failing", f"{_names(failing)} failing", failing, pending
    if any(v == "pending" for v in vals):
        n = sum(1 for v in vals if v == "pending")
        return "pending", f"{n} check(s) pending", [], pending
    return "passing", f"{len(vals)} check(s) passing", [], []


def _rules_for_branch(gh, owner, repo, base):
    """
    Required approvals/contexts from repository *rulesets*.

    Unlike classic branch protection, this endpoint only needs read access, so
    it works with an ordinary PAT. Returns (approvals, contexts, known).
    """
    try:
        rules = gh.get(f"/repos/{owner}/{repo}/rules/branches/{base}")
    except ApiError:
        return None, None, False
    if not isinstance(rules, list):
        return None, None, False
    approvals, contexts = None, set()
    for rule in rules:
        params = rule.get("parameters") or {}
        if rule.get("type") == "pull_request":
            count = params.get("required_approving_review_count")
            if count is not None:
                approvals = max(approvals or 0, count)
        elif rule.get("type") == "required_status_checks":
            for chk in (params.get("required_status_checks") or []):
                if chk.get("context"):
                    contexts.add(chk["context"])
    # An empty list is a real answer: this branch has no rulesets on it.
    return approvals, (contexts or None), True


def _names(items, limit=3):
    """'a, b (+2 more)' — keep reasons readable when a dozen checks are red."""
    items = list(items)
    head = ", ".join(items[:limit])
    return head + (f" (+{len(items) - limit} more)" if len(items) > limit else "")


def split_failing_checks(failing, cfg):
    """
    Split failing check names into (blocking, advisory) using config globs.

    Anything not named by either list is left for the mergeable_state logic to
    judge, so this only ever overrides GitHub in the direction the user asked
    for. blocking wins over advisory when a name matches both.
    """
    blocking_pats = cfg.get("blocking_check_patterns") or []
    advisory_pats = cfg.get("advisory_check_patterns") or []
    blocking = [n for n in failing if any(fnmatch(n, p) for p in blocking_pats)]
    advisory = [n for n in failing
                if n not in blocking
                and any(fnmatch(n, p) for p in advisory_pats)]
    return blocking, advisory


def _branch_protection(gh, owner, repo, base):
    """
    Return (required_approvals, required_contexts, known: bool).

    `known` False means we could not find out what the branch requires — the
    caller must stay strict rather than assume nothing is required.
    """
    cache = getattr(gh, "_prot_cache", None)
    if cache is None:
        cache = gh._prot_cache = {}
    key = (owner, repo, base)
    if key in cache:
        return cache[key]

    classic_known = False
    approvals, contexts = None, set()
    try:
        prot = gh.get(f"/repos/{owner}/{repo}/branches/{base}/protection")
    except ApiError:
        # Classic protection needs admin; a plain PAT gets 403/404 here. That
        # is not evidence the branch is unprotected, so fall through to
        # rulesets rather than concluding anything.
        prot = None
    if prot:
        classic_known = True
        req_reviews = (prot.get("required_pull_request_reviews") or {})
        approvals = req_reviews.get("required_approving_review_count")
        rsc = prot.get("required_status_checks") or {}
        contexts = set(rsc.get("contexts") or [])
        for chk in (rsc.get("checks") or []):
            if chk.get("context"):
                contexts.add(chk["context"])

    rs_approvals, rs_contexts, rs_known = _rules_for_branch(gh, owner, repo, base)
    if rs_known:
        if rs_approvals is not None:
            approvals = rs_approvals if approvals is None else max(approvals, rs_approvals)
        if rs_contexts:
            contexts |= rs_contexts

    result = (approvals, (contexts or None), (classic_known or rs_known))
    cache[key] = result
    return result


def _match_path(path, pat):
    """
    fnmatch has no '**' concept, so a pattern like '**/*.tf' only matches when
    there IS a leading directory — root-level files silently escape. Try the
    pattern as written, then with each '**/' collapsed to nothing, so '**/'
    means "zero or more directories" the way everyone writing these expects.
    """
    if fnmatch(path, pat):
        return True
    while "**/" in pat:
        pat = pat.replace("**/", "", 1)
        if fnmatch(path, pat):
            return True
    return False


def _sensitive_hits(files, patterns):
    hits = []
    for f in files:
        path = f.get("filename", "")
        for pat in patterns:
            if _match_path(path, pat):
                hits.append(path)
                break
    return hits


KNOWN_PREDICATES = frozenset({
    "base_in", "base_not_in", "repo_in", "repo_not_in", "files_match",
    "changed_files_gt", "changed_lines_gt", "author_is_bot",
    "author_association_in", "labels_any", "title_regex",
})


class UnknownPredicate(Exception):
    """A gate rule referenced a predicate we don't implement."""


def validate_gate_rules(rules):
    """Return a list of human-readable problems with a gate_rules list."""
    problems = []
    for i, rule in enumerate(rules or []):
        name = rule.get("name", f"rule #{i + 1}")
        action = rule.get("action", "manual")
        if action not in ("manual", "skip"):
            problems.append(f"{name}: unknown action {action!r} (expected manual/skip)")
        when = rule.get("when") or {}
        if not when:
            problems.append(f"{name}: empty 'when' — rule can never fire")
        for key in when:
            if key not in KNOWN_PREDICATES:
                problems.append(f"{name}: unknown predicate {key!r}")
    return problems


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
    # A predicate we don't understand means this rule cannot be evaluated.
    # Silently ignoring it would disable the gate, so fail closed instead.
    raise UnknownPredicate(key)


def apply_gate_rules(rules, ctx):
    """
    Returns (manual_reasons, skip_reasons). A rule fires only when ALL its
    predicates match. 'manual' -> force human review; 'skip' -> ignore the PR.
    """
    manual, skip = [], []
    for rule in rules or []:
        when = rule.get("when", {})
        if not when:
            continue
        try:
            fired = all(_predicate_match(k, v, ctx) for k, v in when.items())
        except UnknownPredicate as e:
            # Force MANUAL whatever the rule's declared action: a broken
            # 'skip' rule would otherwise let a held PR through just as a
            # broken 'manual' rule would let a protected branch through.
            manual.append(f"{rule.get('name', 'rule')}: unknown predicate "
                          f"'{e}' — rule could not be evaluated")
            continue
        if fired:
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
        "head_sha": "",
        "advisory": "",
        "approvals_met": False,
        "mergeable_state": "",
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
    out["head_sha"] = head_sha
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
    gate_errors = []
    try:
        reviews = gh.get_paginated(
            f"/repos/{owner}/{repo}/pulls/{number}/reviews", cap=200)
    except ApiError as e:
        reviews = []
        gate_errors.append(f"could not read reviews ({e}) — approval state unknown")
    latest = _latest_reviews(reviews)
    approvals = sum(1 for s in latest.values() if s == "APPROVED")
    changes_requested = any(s == "CHANGES_REQUESTED" for s in latest.values())
    you_approved = latest.get(me) == "APPROVED"

    req_approvals, req_contexts, prot_known = _branch_protection(
        gh, owner, repo, base)
    if req_approvals is None:
        req_approvals = cfg.get("default_required_approvals", 1)
    out["approvals"] = f"{approvals}/{req_approvals}"
    # Recorded separately from the verdict: a CHECKS_ADVISORY PR can still have
    # satisfied its approval requirement, and the action engine needs to know.
    out["approvals_met"] = approvals >= req_approvals
    out["mergeable_state"] = mergeable_state

    check_state, check_detail, failing_checks, pending_checks = _check_state(
        gh, owner, repo, head_sha, req_contexts)
    if check_state == "error":
        gate_errors.append(check_detail)

    # --- gate rules (the editable checklist) -------------------------------
    try:
        files = gh.get_paginated(
            f"/repos/{owner}/{repo}/pulls/{number}/files", cap=FILES_CAP)
    except ApiError as e:
        files = []
        gate_errors.append(f"could not read changed files ({e}) — path gates skipped")
    else:
        if len(files) >= FILES_CAP:
            gate_errors.append(
                f"changed-file list truncated at {FILES_CAP} — path gates incomplete")
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
    # A gate whose input we could not load is a red flag, never a free pass.
    red_flags = gate_errors + red_flags

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
    # A red check only blocks when the base branch requires it, and GitHub has
    # already applied branch protection when computing mergeable_state:
    #   blocked  = something required is unsatisfied
    #   unstable = mergeable, with non-required checks red
    # So trust that over our own reading of the raw check list, which cannot
    # see which contexts are required when protection is unreadable.
    advisory = None
    if check_state == "failing":
        forced_blocking, named_advisory = split_failing_checks(failing_checks, cfg)
        if forced_blocking:
            # You listed these as never-ignore, so GitHub's opinion doesn't matter.
            out["verdict"] = "CHECKS_FAILED"
            reasons.append(f"{_names(forced_blocking)} failing "
                           "(listed in blocking_check_patterns)")
            return out
        if pending_checks:
            # Something is still running. Whatever it reports could change the
            # answer, so wait for it rather than merging on a partial picture.
            out["verdict"] = "CHECKS_PENDING"
            reasons.append(f"{_names(failing_checks)} failing, but "
                           f"{_names(pending_checks)} still running")
            return out
        if mergeable_state == "unstable":
            advisory = _names(failing_checks)
        elif named_advisory and len(named_advisory) == len(failing_checks):
            # Every red check is one you've named as noise.
            advisory = _names(named_advisory)
            reasons.append("all failing checks matched advisory_check_patterns")
        else:
            # blocked, clean, or unknown with a red check: stay strict.
            out["verdict"] = "CHECKS_FAILED"
            reasons.append(check_detail)
            if mergeable_state == "blocked":
                reasons.append("branch protection is blocking the merge")
            return out
    # Deliberately no 'elif mergeable_state == "unstable"' here: GitHub can
    # report unstable over something we cannot see, and raising an advisory we
    # can't name would both read as a blank RED column and quietly demote a PR
    # that used to be READY_TO_MERGE. An advisory needs a named failing check.
    if advisory:
        out["advisory"] = advisory

    # If a red flag is present, it's manual no matter how green it is.
    if red_flags:
        out["verdict"] = "MANUAL_REVIEW"
        reasons.extend(red_flags)
        if check_state in ("pending", "none"):
            reasons.append(check_detail)
        if advisory:
            reasons.append(f"{advisory} failing (not required — does not block)")
        return out

    # 'none' means CI has not reported yet — on a fresh push that is the normal
    # state for the first few seconds. Waiting is correct; calling it green is not.
    if check_state == "pending" or (
            check_state == "none" and not cfg.get("allow_no_checks", False)):
        out["verdict"] = "CHECKS_PENDING"
        reasons.append(check_detail)
        return out
    if mergeable is None or mergeable_state in ("unknown", ""):
        out["verdict"] = "MERGEABILITY_UNKNOWN"
        reasons.append("GitHub still computing mergeability — re-run shortly")
        return out

    # Mergeable per GitHub, but something is red. Not a failure and not clean
    # either — surface it as its own verdict so the call is yours to make.
    if advisory:
        out["verdict"] = "CHECKS_ADVISORY"
        reasons.append(f"{advisory} failing — not required on '{base}', "
                       "merge not blocked")
        reasons.append(f"{approvals}/{req_approvals} approvals, state={mergeable_state}")
        if req_contexts:
            reasons.append("required checks on '%s' (%s) are green"
                           % (base, ", ".join(sorted(req_contexts)[:3])))
        else:
            reasons.append("no required status checks found on '%s'; "
                           "GitHub reports the merge as unblocked" % base)
        return out

    # Green zone.
    if approvals >= req_approvals and mergeable_state in ("clean", "unstable", "has_hooks"):
        out["verdict"] = "READY_TO_MERGE"
        reasons.append(f"{approvals}/{req_approvals} approvals, {check_detail}, "
                       f"state={mergeable_state}")
        return out

    if you_approved:
        out["verdict"] = "APPROVED_WAITING"
        outstanding = max(0, req_approvals - approvals)
        if outstanding:
            reasons.append(f"you approved; waiting on {outstanding} more")
        else:
            reasons.append(f"you approved; {approvals}/{req_approvals} approvals "
                           f"but state={mergeable_state}")
        return out

    out["verdict"] = "NEEDS_YOUR_APPROVAL"
    reasons.append(f"green, {approvals}/{req_approvals} approvals, {check_detail}")
    if mergeable_state == "behind":
        reasons.append("branch is behind base (would need update before merge)")
    return out


# ----------------------------------------------------------------------------
# Action engine (only runs under --apply; writes to GitHub)
# ----------------------------------------------------------------------------
ACTIONABLE_VERDICTS = ("NEEDS_YOUR_APPROVAL", "READY_TO_MERGE", "APPROVED_WAITING")


def actionable_verdicts(cfg):
    """
    Verdicts the action engine is allowed to touch.

    CHECKS_ADVISORY means GitHub would let the merge through but something is
    red, so it is opt-in: seeing one on the board is useful, acting on one
    unattended is a separate decision.
    """
    if cfg.get("act_on_advisory_checks", False):
        return ACTIONABLE_VERDICTS + ("CHECKS_ADVISORY",)
    return ACTIONABLE_VERDICTS


def _audit(cfg, line):
    path = os.path.expanduser(cfg.get("audit_log") or "")
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {line}\n")
    except Exception as e:
        print(f"warning: could not write audit log: {e}", file=sys.stderr)


def wait_until_actionable(gh, cfg, owner, repo, number, me):
    """Poll a single PR until its verdict is terminal (not pending/unknown)."""
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
    if verdict not in actionable_verdicts(cfg):
        ev["action"] = f"no action ({verdict})"
        _audit(cfg, f"{owner}/{repo}#{number} SKIP {verdict}")
        return ev

    # Pin the exact commit the gates were evaluated against — deliberately NOT
    # a fresh read. If a push landed mid-flight, GitHub rejects the stale SHA
    # and we fail safe, instead of acting on code no gate ever saw.
    head_sha = ev.get("head_sha", "")
    if not head_sha:
        ev["action"] = "no action (head SHA unknown)"
        _audit(cfg, f"{owner}/{repo}#{number} SKIP no-head-sha")
        return ev

    # Approve if it's green, flag-free, and you haven't approved yet.
    approved_this_run = False
    if verdict in ("NEEDS_YOUR_APPROVAL", "CHECKS_ADVISORY") and not ev.get("you_approved"):
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
        # An advisory PR that has its approvals is merge-ready too — GitHub
        # says the merge is unblocked, and you opted in twice (once for
        # advisory checks, once for auto-merge). Without this, enabling both
        # still never merged anything.
        if (not ready and verdict == "CHECKS_ADVISORY"
                and cfg.get("act_on_advisory_checks", False)
                and ev.get("approvals_met")
                and ev.get("mergeable_state") in ("clean", "unstable", "has_hooks")):
            ready = True

    # The re-evaluation above may have seen a newer commit than the one we just
    # approved. Never merge a head the approval does not cover, and say so —
    # a branch moving under you mid-run is worth knowing about either way.
    if ev.get("head_sha", head_sha) != head_sha:
        actions.append(f"head moved to {ev.get('head_sha', '')[:7]} after approval;"
                       " not merging this run")
        ready = False

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
    "CHECKS_ADVISORY": "MERGEABLE (checks red)",
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
    # Only draw the ADVISORY column when something is actually in it —
    # otherwise it's a column of blanks on every ordinary run.
    show_advisory = any(r.get("advisory") for r in results)
    rows = []
    for r in results:
        pr_id = f"{r['repo']}#{r['number']}"
        title = (r["title"][:46] + "…") if len(r["title"]) > 47 else r["title"]
        row = [
            VERDICT_LABEL.get(r["verdict"], r["verdict"]),
            pr_id,
            r.get("base", ""),
            r.get("approvals", ""),
            r.get("checks", ""),
        ]
        if show_advisory:
            adv = r.get("advisory", "")
            row.append((adv[:24] + "…") if len(adv) > 25 else adv)
        row.append(title)
        rows.append(row)
    headers = ["VERDICT", "PR", "BASE", "APPR", "CHECKS"]
    if show_advisory:
        headers.append("RED (ADVISORY)")
    headers.append("TITLE")
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
                 ("READY_TO_MERGE", "NEEDS_YOUR_APPROVAL", "CHECKS_ADVISORY",
                  "MANUAL_REVIEW", "CHANGES_REQUESTED", "CONFLICT", "CHECKS_FAILED")]
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
    ap.add_argument("--pr", action="append", default=[], metavar="REF",
                    help="act on this PR only — no searching. Takes a full URL, "
                         "a bare github.com/owner/repo/pull/123, or the "
                         "owner/repo#123 shorthand. Repeatable.")
    ap.add_argument("--slack", action="store_true",
                    help="also scan the configured Slack channel for PR links")
    ap.add_argument("--no-github", action="store_true",
                    help="skip the GitHub review-requested/mentions feed")
    ap.add_argument("--include-mentions", action="store_true",
                    help="with --apply: also act on PRs that merely @-mention you "
                         "(off by default — a mention is a question, not a request "
                         "to merge)")
    ap.add_argument("--since-hours", type=float, default=None, metavar="N",
                    help="only consider PRs updated in the last N hours "
                         "(default 24, from config discovery_window_hours; "
                         "0 = no window). Does not apply to an explicit --pr.")
    ap.add_argument("--no-progress", action="store_true",
                    help="don't draw the progress spinner (it is off "
                         "automatically when stderr isn't a terminal)")
    ap.add_argument("--json", action="store_true", help="output JSON")
    ap.add_argument("--config", help="path to a config.json")
    ap.add_argument("--apply", action="store_true",
                    help="ACT: approve/merge PRs that pass every gate (writes to GitHub)")
    ap.add_argument("--wait", action="store_true",
                    help="with --apply: wait for pending checks to finish, then "
                         "act. Implied when you name PRs with --pr.")
    ap.add_argument("--no-wait", action="store_true",
                    help="don't wait for pending checks, even with --pr; a PR "
                         "mid-CI is simply left alone")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --apply: print what it WOULD do, change nothing")
    ap.add_argument("--act-on-advisory", action="store_true",
                    help="with --apply: also act on CHECKS_ADVISORY — PRs GitHub "
                         "would let through with a non-required check red. "
                         "One run only; config stays as it is.")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    if args.act_on_advisory:
        # Per-run override, so trying this once doesn't mean loosening the
        # stored config. blocking_check_patterns still wins over it.
        cfg["act_on_advisory_checks"] = True
    progress = Progress(enabled=not args.no_progress)
    since = window_start(cfg, args.since_hours)
    hours = args.since_hours if args.since_hours is not None else cfg.get(
        "discovery_window_hours", 24)
    window_desc = f"{hours:g}h"
    scope_desc = f"last {window_desc}" if since else "all time"
    for problem in validate_gate_rules(gate_rules_for(cfg)):
        print(f"warning: gate_rules — {problem}", file=sys.stderr)
    gh = GitHub(os.environ.get("GITHUB_TOKEN"),
                os.environ.get("GITHUB_API_URL", "https://api.github.com"))
    progress.start("authenticating with GitHub…")
    try:
        me = gh.login()
    finally:
        progress.stop()

    candidates = set()
    used_feed = False          # the window only describes feed-driven discovery
    if args.pr:
        for u in args.pr:
            ref = parse_pr_ref(u)
            if ref:
                candidates.add(ref)
            else:
                print(f"warning: not a PR reference: {u}\n"
                      "  expected a URL, github.com/owner/repo/pull/123, "
                      "or owner/repo#123", file=sys.stderr)
    for rp in args.repo:
        if "/" not in rp:
            print(f"warning: --repo expects owner/repo, got {rp}", file=sys.stderr)
            continue
        owner, repo = rp.split("/", 1)
        try:
            # /pulls has no date filter, but sorting by recency lets us stop at
            # the first PR past the window instead of evaluating the whole list.
            used_feed = True
            progress.update(f"listing open PRs in {rp}…")
            prs = gh.get_paginated(
                f"/repos/{owner}/{repo}/pulls",
                {"state": "open", "sort": "updated", "direction": "desc"},
                cap=100)
            skipped = 0
            for i, p in enumerate(prs):
                updated = _parse_gh_time(p.get("updated_at")) if since else None
                if updated is not None and updated < since:
                    skipped = len(prs) - i
                    break
                candidates.add((owner, repo, p["number"]))
            if skipped:
                print(f"note: {rp}: skipped {skipped} PR(s) not updated in the "
                      f"last {window_desc}.", file=sys.stderr)
        except ApiError as e:
            print(f"warning: could not list {rp}: {e}", file=sys.stderr)

    if not args.no_github and not args.pr and not args.repo:
        # Acting on a bare @-mention would mean merging a PR someone only asked
        # you a question about, so --apply drops that feed unless asked.
        include_mentions = args.include_mentions or not args.apply
        if args.apply and not include_mentions:
            print("note: --apply uses the review-requested feed only; pass "
                  "--include-mentions to also act on @-mentions.", file=sys.stderr)
        used_feed = True
        progress.update(f"searching GitHub for PRs ({scope_desc})…")
        candidates |= discover_from_github(
            gh, me, include_mentions=include_mentions, since=since)
    elif not args.no_github and not args.repo and args.pr:
        pass  # explicit PRs only
    if args.slack:
        used_feed = True
        progress.update(f"scanning Slack {cfg.get('slack_channel', '')} ({scope_desc})…")
        candidates |= discover_from_slack(cfg, since=since)

    progress.stop()
    if not used_feed:
        scope_desc = "named PRs"
    if not candidates:
        if not used_feed:
            print("No usable PR references given.")
        elif since:
            print(f"No candidate PRs updated in the {scope_desc}. "
                  "(Try --since-hours 0, --slack, --repo owner/repo, or --pr URL.)")
        else:
            print("No candidate PRs found. (Try --slack, --repo owner/repo, or --pr URL.)")
        return 0

    # Naming a PR with --pr is a deliberate choice of that one PR, so waiting
    # for its checks is almost always what you meant — without it a run that
    # lands mid-CI silently abandons the PR and nothing comes back for it.
    # Feed- and --repo-driven runs stay opt-in: they can be dozens of PRs, and
    # the wait loop is sequential and costs ~6 API calls per poll.
    wait = args.wait
    if args.apply and args.pr and not args.repo and not args.no_wait:
        wait = True
    if wait and not args.wait:
        print(f"note: waiting up to {cfg.get('wait_timeout_seconds', 1800)}s for "
              "checks on the PR(s) you named (--no-wait to skip).",
              file=sys.stderr)

    results = []
    todo = sorted(candidates)
    # Each PR costs ~6 sequential GitHub calls, so this is the slow part and
    # the only place a counter is worth showing.
    try:
        for i, (owner, repo, number) in enumerate(todo, 1):
            where = f"[{i}/{len(todo)}] {owner}/{repo}#{number}"
            if args.apply:
                if wait:
                    progress.update(f"{where} — waiting for checks…")
                    ev = wait_until_actionable(gh, cfg, owner, repo, number, me)
                    if ev["verdict"] in actionable_verdicts(cfg):
                        progress.update(f"{where} — applying…")
                        ev = apply_pr(gh, cfg, owner, repo, number, me,
                                      dry_run=args.dry_run)
                    else:
                        # apply_pr never ran, so say why rather than a bare
                        # "no action" — it already reads as "it did nothing".
                        ev.setdefault("action", f"no action ({ev['verdict']})")
                else:
                    progress.update(f"{where} — applying…")
                    ev = apply_pr(gh, cfg, owner, repo, number, me,
                                  dry_run=args.dry_run)
            else:
                progress.update(f"{where} — evaluating…")
                ev = evaluate_pr(gh, cfg, owner, repo, number, me)
            results.append(ev)
    finally:
        progress.stop()

    if args.json:
        print(json.dumps(results, indent=2))
    elif args.apply:
        mode = "DRY-RUN" if args.dry_run else "LIVE"
        advisory_note = (" +advisory" if cfg.get("act_on_advisory_checks") else "")
        print(f"\nPR apply for @{me}  —  {len(results)} PR(s)  "
              f"({scope_desc})  [{mode}{advisory_note}]\n")
        for r in results:
            print(f"  {r['repo']}#{r['number']}  [{VERDICT_LABEL.get(r['verdict'], r['verdict'])}]"
                  f"  ->  {r.get('action', 'no action')}")
            print(f"     {r['url']}")
    else:
        print(f"\nPR board for @{me}  —  {len(results)} PR(s)  "
              f"({scope_desc}, read-only, no actions taken)\n")
        render_table(results)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
