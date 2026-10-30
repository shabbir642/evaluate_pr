"""
Mergeability tests: a red check only blocks when the base branch requires it.

GitHub applies branch protection when it computes mergeable_state, so that —
not our reading of the raw check list — decides whether a failure blocks.
Fixtures mirror real PRs observed on vmockinc/career-service-ui.
"""
import contextlib, io, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _loader import pb

ME = "shabbir"
FAILING = {"check_runs": [{"name": "guardrails", "status": "completed",
                           "conclusion": "failure"}]}
PASSING = {"check_runs": [{"name": "ci", "status": "completed",
                           "conclusion": "success"}]}
approve = lambda who: {"state": "APPROVED", "user": {"login": who}}

res = []
def chk(name, cond):
    res.append(bool(cond)); print(f"[{'PASS' if cond else 'FAIL'}] {name}")


def cfg(**over):
    c = dict(pb.DEFAULT_CONFIG); c["audit_log"] = ""; c.update(over); return c


def mkpr(state="unstable", mergeable=True, base="uat"):
    return {"number": 1, "state": "open", "merged": False, "title": "PR",
            "base": {"ref": base}, "head": {"sha": "abc"},
            "user": {"login": "alice", "type": "User"},
            "author_association": "MEMBER", "draft": False,
            "mergeable": mergeable, "mergeable_state": state,
            "changed_files": 3, "additions": 10, "deletions": 5, "labels": []}


UNREADABLE = object()      # distinct from "readable, and empty"


class FakeGH:
    def __init__(self, pr, checkruns=None, reviews=None,
                 protection=None, rules=None):
        self._pr = pr; self._cr = checkruns or FAILING
        self._reviews = list(reviews or []); self._prot = protection
        self._rules = [] if rules is None else rules
        self.posts = []; self.puts = []

    def login(self): return ME

    def get(self, path, params=None):
        if path.endswith("/pulls/1"): return self._pr
        if "/check-runs" in path: return self._cr
        if path.endswith("/status"): return {"statuses": []}
        if "/rules/branches/" in path:
            if self._rules is UNREADABLE: raise pb.ApiError(403, "no access")
            return self._rules
        if "/protection" in path:
            if self._prot is None: raise pb.ApiError(404, "Not Found")
            return self._prot
        raise pb.ApiError(404, path)

    def get_paginated(self, path, params=None, cap=300):
        if "/reviews" in path: return self._reviews
        if "/files" in path: return [{"filename": "src/a.js"}]
        return []

    def post(self, path, body): self.posts.append(body); return {}
    def put(self, path, body): self.puts.append(body); return {"merged": True}


def V(gh, c=None): return pb.evaluate_pr(gh, c or cfg(), "o", "r", 1, ME)["verdict"]


# --- the case that prompted this: PR #833 ----------------------------------
gh = FakeGH(mkpr("unstable"), checkruns=FAILING)
ev = pb.evaluate_pr(gh, cfg(), "o", "r", 1, ME)
chk("unstable + failing non-required -> CHECKS_ADVISORY",
    ev["verdict"] == "CHECKS_ADVISORY")
chk("advisory says it does not block",
    any("not blocked" in r for r in ev["reasons"]))
chk("advisory names the base branch", any("'uat'" in r for r in ev["reasons"]))

# --- but a required failure still blocks -----------------------------------
gh = FakeGH(mkpr("blocked"), checkruns=FAILING)
ev = pb.evaluate_pr(gh, cfg(), "o", "r", 1, ME)
chk("blocked + failing -> CHECKS_FAILED", ev["verdict"] == "CHECKS_FAILED")
chk("blocked says branch protection is the reason",
    any("branch protection" in r for r in ev["reasons"]))

chk("clean + failing -> CHECKS_FAILED (fail closed)",
    V(FakeGH(mkpr("clean"), checkruns=FAILING)) == "CHECKS_FAILED")
chk("unknown state + failing -> CHECKS_FAILED (fail closed)",
    V(FakeGH(mkpr("unknown"), checkruns=FAILING)) == "CHECKS_FAILED")

# blocked with GREEN checks is just awaiting review, not a check failure
chk("blocked + passing -> NEEDS_YOUR_APPROVAL",
    V(FakeGH(mkpr("blocked"), checkruns=PASSING)) == "NEEDS_YOUR_APPROVAL")

# unchanged behaviour
chk("dirty -> CONFLICT",
    V(FakeGH(mkpr("dirty", mergeable=False), checkruns=PASSING)) == "CONFLICT")
chk("clean + passing + approved -> READY_TO_MERGE",
    V(FakeGH(mkpr("clean"), checkruns=PASSING,
             reviews=[approve("bob")])) == "READY_TO_MERGE")


# --- acting on an advisory PR is opt-in ------------------------------------
gh = FakeGH(mkpr("unstable"), checkruns=FAILING)
ev = pb.apply_pr(gh, cfg(enable_auto_merge=True), "o", "r", 1, ME)
chk("advisory: no writes by default", gh.posts == [] and gh.puts == [])
chk("advisory: action explains why", "no action (CHECKS_ADVISORY)" in ev["action"])

gh = FakeGH(mkpr("unstable"), checkruns=FAILING)
ev = pb.apply_pr(gh, cfg(act_on_advisory_checks=True), "o", "r", 1, ME)
chk("act_on_advisory_checks: approves", len(gh.posts) == 1)
chk("act_on_advisory_checks: still honours enable_auto_merge", gh.puts == [])

chk("actionable_verdicts excludes advisory by default",
    "CHECKS_ADVISORY" not in pb.actionable_verdicts(cfg()))
chk("actionable_verdicts includes it when opted in",
    "CHECKS_ADVISORY" in pb.actionable_verdicts(cfg(act_on_advisory_checks=True)))

# a red flag still wins over an advisory
gh = FakeGH(mkpr("unstable", base="master"), checkruns=FAILING)
chk("protected branch beats advisory",
    V(gh, cfg(act_on_advisory_checks=True)) == "MANUAL_REVIEW")


# --- phase 2: rulesets as a non-admin protection source --------------------
RULES = [{"type": "required_status_checks",
          "parameters": {"required_status_checks": [{"context": "ci"}]}},
         {"type": "pull_request",
          "parameters": {"required_approving_review_count": 2}}]

gh = FakeGH(mkpr("clean"), checkruns=PASSING, rules=RULES)
approvals, contexts, known = pb._branch_protection(gh, "o", "r", "uat")
chk("rulesets give required contexts", contexts == {"ci"})
chk("rulesets give required approvals", approvals == 2)
chk("rulesets count as known", known is True)

gh = FakeGH(mkpr("clean"), checkruns=PASSING, rules=[])
chk("empty rulesets + no classic -> no contexts",
    pb._branch_protection(gh, "o", "r", "uat")[1] is None)

gh = FakeGH(mkpr("clean"), checkruns=PASSING, rules=UNREADABLE)
chk("both sources unreadable -> known False",
    pb._branch_protection(gh, "o", "r", "uat")[2] is False)
gh = FakeGH(mkpr("clean"), checkruns=PASSING, rules=UNREADABLE)
chk("both unreadable -> no contexts, so checks stay strict",
    pb._branch_protection(gh, "o", "r", "uat")[1] is None)
# unreadable protection must not make a red check look optional
chk("unreadable protection + clean + failing still blocks",
    V(FakeGH(mkpr("clean"), checkruns=FAILING, rules=UNREADABLE)) == "CHECKS_FAILED")

# classic protection still wins where it is readable
CLASSIC = {"required_pull_request_reviews": {"required_approving_review_count": 1},
           "required_status_checks": {"contexts": ["build"]}}
gh = FakeGH(mkpr("clean"), checkruns=PASSING, protection=CLASSIC, rules=[])
approvals, contexts, known = pb._branch_protection(gh, "o", "r", "uat")
chk("classic protection is read", contexts == {"build"} and approvals == 1)

# the two sources combine rather than one silently winning
gh = FakeGH(mkpr("clean"), checkruns=PASSING, protection=CLASSIC, rules=RULES)
approvals, contexts, known = pb._branch_protection(gh, "o", "r", "uat")
chk("classic and rulesets union their contexts", contexts == {"build", "ci"})
chk("the stricter approval count wins", approvals == 2)


# --- protection is looked up once per (repo, base), not once per PR --------
class CountingGH(FakeGH):
    def __init__(self, **kw):
        super().__init__(mkpr("clean"), **kw); self.prot_calls = 0
    def get(self, path, params=None):
        if "/protection" in path or "/rules/branches/" in path:
            self.prot_calls += 1
        return super().get(path, params)


gh = CountingGH(checkruns=PASSING, rules=[])
for _ in range(3):
    pb._branch_protection(gh, "o", "r", "uat")
chk("protection lookups are cached per branch", gh.prot_calls <= 2)
pb._branch_protection(gh, "o", "r", "other-branch")
chk("a different branch is looked up fresh", gh.prot_calls > 2)

# --- phase 3: say WHICH check is red --------------------------------------
gh = FakeGH(mkpr("unstable"), checkruns=FAILING)
ev = pb.evaluate_pr(gh, cfg(), "o", "r", 1, ME)
chk("advisory names the failing check", ev["advisory"] == "guardrails")
chk("reason names the failing check",
    any(r.startswith("guardrails failing") for r in ev["reasons"]))

MANY = {"check_runs": [{"name": n, "status": "completed", "conclusion": "failure"}
                       for n in ("a", "b", "c", "d", "e")]}
ev = pb.evaluate_pr(FakeGH(mkpr("unstable"), checkruns=MANY), cfg(), "o", "r", 1, ME)
chk("many failures are summarised, not listed in full",
    "(+2 more)" in ev["advisory"] and ev["advisory"].startswith("a, b, c"))
chk("_names keeps short lists intact", pb._names(["x", "y"]) == "x, y")
chk("_names truncates long ones", pb._names(["a", "b", "c", "d"]) == "a, b, c (+1 more)")


# --- phase 4: blocking_check_patterns overrides GitHub --------------------
c = cfg(blocking_check_patterns=["guardrails"])
ev = pb.evaluate_pr(FakeGH(mkpr("unstable"), checkruns=FAILING), c, "o", "r", 1, ME)
chk("blocking pattern blocks even when unstable", ev["verdict"] == "CHECKS_FAILED")
chk("blocking pattern says why",
    any("blocking_check_patterns" in r for r in ev["reasons"]))

c = cfg(blocking_check_patterns=["*guard*"])
chk("blocking pattern accepts globs",
    V(FakeGH(mkpr("unstable"), checkruns=FAILING), c) == "CHECKS_FAILED")

c = cfg(blocking_check_patterns=["security-scan"])
chk("a non-matching blocking pattern changes nothing",
    V(FakeGH(mkpr("unstable"), checkruns=FAILING), c) == "CHECKS_ADVISORY")

gh = FakeGH(mkpr("unstable"), checkruns=FAILING)
ev = pb.apply_pr(gh, cfg(blocking_check_patterns=["guardrails"],
                        act_on_advisory_checks=True,
                        enable_auto_merge=True), "o", "r", 1, ME)
chk("blocking pattern beats act_on_advisory_checks",
    gh.posts == [] and gh.puts == [])


# --- phase 4: advisory_check_patterns relaxes the strict fallback ---------
c = cfg(advisory_check_patterns=["guardrails"])
ev = pb.evaluate_pr(FakeGH(mkpr("clean"), checkruns=FAILING), c, "o", "r", 1, ME)
chk("advisory pattern relaxes a non-unstable failure",
    ev["verdict"] == "CHECKS_ADVISORY")
chk("advisory pattern says why",
    any("advisory_check_patterns" in r for r in ev["reasons"]))

MIXED = {"check_runs": [
    {"name": "guardrails", "status": "completed", "conclusion": "failure"},
    {"name": "unit-tests", "status": "completed", "conclusion": "failure"}]}
c = cfg(advisory_check_patterns=["guardrails"])
chk("a partial match does NOT relax (unit-tests still blocks)",
    V(FakeGH(mkpr("clean"), checkruns=MIXED), c) == "CHECKS_FAILED")
chk("all-matching does relax",
    V(FakeGH(mkpr("clean"), checkruns=MIXED),
      cfg(advisory_check_patterns=["guardrails", "unit-tests"])) == "CHECKS_ADVISORY")

chk("no patterns configured -> strict fallback unchanged",
    V(FakeGH(mkpr("clean"), checkruns=FAILING)) == "CHECKS_FAILED")


# --- split_failing_checks ---------------------------------------------------
b, a = pb.split_failing_checks(["x", "y"], cfg(blocking_check_patterns=["x"],
                                               advisory_check_patterns=["y"]))
chk("split sorts each name to its list", b == ["x"] and a == ["y"])
b, a = pb.split_failing_checks(["x"], cfg(blocking_check_patterns=["x"],
                                          advisory_check_patterns=["x"]))
chk("blocking wins when a name matches both", b == ["x"] and a == [])
b, a = pb.split_failing_checks(["x"], cfg())
chk("unnamed checks go in neither list", b == [] and a == [])


# --- the board only grows a column when it has something to show ----------
def render(results):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        pb.render_table(results)
    return buf.getvalue()

base_row = {"repo": "o/r", "number": 1, "title": "t", "base": "uat",
            "approvals": "0/1", "checks": "passing", "reasons": [],
            "url": "u", "verdict": "NEEDS_YOUR_APPROVAL"}
out = render([dict(base_row)])
chk("no advisory anywhere -> no extra column", "RED (ADVISORY)" not in out)

adv = dict(base_row, verdict="CHECKS_ADVISORY", checks="failing",
           advisory="guardrails")
out = render([adv])
chk("advisory present -> column appears", "RED (ADVISORY)" in out)
chk("column shows the check name", "guardrails" in out)

out = render([dict(base_row), dict(adv, number=2)])
chk("mixed rows still render one table", out.count("RED (ADVISORY)") == 1)

# --- an approved advisory PR must actually be able to merge ---------------
# Regression: ready was computed as (verdict == "READY_TO_MERGE") alone, so
# enabling act_on_advisory_checks AND enable_auto_merge still merged nothing.
ADV = dict(act_on_advisory_checks=True, enable_auto_merge=True)

gh = FakeGH(mkpr("unstable"), checkruns=FAILING, reviews=[approve(ME)])
ev = pb.evaluate_pr(gh, cfg(**ADV), "o", "r", 1, ME)
chk("approved advisory PR reports approvals_met", ev["approvals_met"] is True)
chk("approved advisory PR records mergeable_state",
    ev["mergeable_state"] == "unstable")

gh = FakeGH(mkpr("unstable"), checkruns=FAILING, reviews=[approve(ME)])
ev = pb.apply_pr(gh, cfg(**ADV), "o", "r", 1, ME)
chk("approved advisory PR merges", len(gh.puts) == 1)
chk("merge pins the gated sha", gh.puts and gh.puts[0]["sha"] == "abc")
chk("it does not re-approve what you already approved", gh.posts == [])

# unapproved: approve first, then merge once the count is satisfied
gh = FakeGH(mkpr("unstable"), checkruns=FAILING)
ev = pb.apply_pr(gh, cfg(**ADV), "o", "r", 1, ME)
chk("unapproved advisory PR is approved", len(gh.posts) == 1)

# and every existing brake still holds
gh = FakeGH(mkpr("unstable"), checkruns=FAILING, reviews=[approve(ME)])
pb.apply_pr(gh, cfg(act_on_advisory_checks=True, enable_auto_merge=False),
            "o", "r", 1, ME)
chk("enable_auto_merge=False still blocks the merge", gh.puts == [])

gh = FakeGH(mkpr("unstable"), checkruns=FAILING, reviews=[approve(ME)])
pb.apply_pr(gh, cfg(enable_auto_merge=True), "o", "r", 1, ME)
chk("act_on_advisory_checks=False still blocks the merge", gh.puts == [])

gh = FakeGH(mkpr("unstable"), checkruns=FAILING, reviews=[approve(ME)])
pb.apply_pr(gh, cfg(blocking_check_patterns=["guardrails"], **ADV),
            "o", "r", 1, ME)
chk("blocking_check_patterns still blocks the merge", gh.puts == [])

gh = FakeGH(mkpr("unstable", base="master"), checkruns=FAILING,
            reviews=[approve(ME)])
pb.apply_pr(gh, cfg(**ADV), "o", "r", 1, ME)
chk("protected base still blocks the merge", gh.puts == [])

# a state GitHub has NOT called mergeable must not merge, even when advisory
gh = FakeGH(mkpr("blocked"), checkruns=FAILING, reviews=[approve(ME)])
pb.apply_pr(gh, cfg(advisory_check_patterns=["guardrails"], **ADV),
            "o", "r", 1, ME)
chk("blocked state never merges even via advisory patterns", gh.puts == [])

# approvals not yet met -> approve, but do not merge on someone else's behalf
two = {"required_pull_request_reviews": {"required_approving_review_count": 2},
       "required_status_checks": {"contexts": []}}
gh = FakeGH(mkpr("unstable"), checkruns=FAILING, protection=two,
            reviews=[approve(ME)])
ev = pb.evaluate_pr(gh, cfg(**ADV), "o", "r", 1, ME)
chk("1 of 2 approvals -> approvals_met False", ev["approvals_met"] is False)
gh = FakeGH(mkpr("unstable"), checkruns=FAILING, protection=two,
            reviews=[approve(ME)])
pb.apply_pr(gh, cfg(**ADV), "o", "r", 1, ME)
chk("1 of 2 approvals -> no merge", gh.puts == [])

# --- a check still running always wins over acting ------------------------
# Regression: _check_state ranks "failing" above "pending", so once one check
# failed the still-running ones stopped counting and an advisory PR merged
# while CI was mid-flight.
R = lambda n, st, c=None: {"name": n, "status": st, "conclusion": c}
ADV2 = dict(act_on_advisory_checks=True, enable_auto_merge=True)

def runs(*rs): return {"check_runs": list(rs)}

for label, cr in [
        ("single check in_progress", runs(R("ci", "in_progress"))),
        ("single check queued", runs(R("ci", "queued"))),
        ("one green, one running", runs(R("ci", "completed", "success"),
                                        R("g", "in_progress"))),
        ("one FAILED, one running", runs(R("ci", "completed", "failure"),
                                         R("g", "in_progress")))]:
    gh = FakeGH(mkpr("unstable"), checkruns=cr, reviews=[approve(ME)])
    chk(f"{label} -> CHECKS_PENDING",
        pb.evaluate_pr(gh, cfg(**ADV2), "o", "r", 1, ME)["verdict"] == "CHECKS_PENDING")
    gh = FakeGH(mkpr("unstable"), checkruns=cr, reviews=[approve(ME)])
    pb.apply_pr(gh, cfg(**ADV2), "o", "r", 1, ME)
    chk(f"{label} -> no writes", gh.posts == [] and gh.puts == [])

gh = FakeGH(mkpr("unstable"),
            checkruns=runs(R("ci", "completed", "failure"), R("g", "in_progress")),
            reviews=[approve(ME)])
ev = pb.evaluate_pr(gh, cfg(**ADV2), "o", "r", 1, ME)
chk("pending-with-failure names both",
    any("ci failing" in r and "g still running" in r for r in ev["reasons"]))

# once everything has reported, the advisory path resumes
gh = FakeGH(mkpr("unstable"),
            checkruns=runs(R("ci", "completed", "success"),
                           R("g", "completed", "failure")),
            reviews=[approve(ME)])
chk("all reported, one failed -> CHECKS_ADVISORY",
    pb.evaluate_pr(gh, cfg(**ADV2), "o", "r", 1, ME)["verdict"] == "CHECKS_ADVISORY")


# --- unstable with nothing visibly red is still just ready ----------------
# GitHub can report unstable over something we cannot see. Raising an advisory
# we cannot name would blank the RED column and demote a ready PR.
gh = FakeGH(mkpr("unstable"), checkruns=PASSING, reviews=[approve(ME)])
ev = pb.evaluate_pr(gh, cfg(), "o", "r", 1, ME)
chk("unstable + all green -> READY_TO_MERGE", ev["verdict"] == "READY_TO_MERGE")
chk("unstable + all green raises no advisory", not ev["advisory"])

gh = FakeGH(mkpr("unstable"), checkruns=PASSING, reviews=[approve(ME)])
pb.apply_pr(gh, cfg(enable_auto_merge=True), "o", "r", 1, ME)
chk("unstable + all green merges on enable_auto_merge alone", len(gh.puts) == 1)

# --- wait_until_actionable: the --wait / prwatch polling loop -------------
# Untested until now, and it can block for wait_timeout_seconds in production.
import time

class PollingGH(FakeGH):
    """Checks stay in_progress for `flips` polls, then settle."""

    def __init__(self, flips=2, settle="success", **kw):
        super().__init__(mkpr("clean"), reviews=[approve("bob")], **kw)
        self._flips = flips; self._settle = settle; self.polls = 0

    def get(self, path, params=None):
        if path.endswith("/pulls/1"):
            self.polls += 1
        if "/check-runs" in path:
            if self.polls <= self._flips:
                return {"check_runs": [R("ci", "in_progress")]}
            return {"check_runs": [R("ci", "completed", self._settle)]}
        return super().get(path, params)


FAST = dict(wait_interval_seconds=0.01, wait_timeout_seconds=5, audit_log="")

gh = PollingGH(flips=2)
ev = pb.wait_until_actionable(gh, cfg(**FAST), "o", "r", 1, ME)
chk("waits, then returns the settled verdict", ev["verdict"] == "READY_TO_MERGE")
chk("polled more than once", gh.polls > 1)

gh = PollingGH(flips=2, settle="failure")
ev = pb.wait_until_actionable(gh, cfg(**FAST), "o", "r", 1, ME)
chk("a failure after waiting is reported, not merged",
    ev["verdict"] == "CHECKS_FAILED")

gh = PollingGH(flips=0)
ev = pb.wait_until_actionable(gh, cfg(**FAST), "o", "r", 1, ME)
chk("already actionable -> returns at once", gh.polls == 1)

# never settles: must give up rather than spin forever
SLOW = dict(wait_interval_seconds=0.01, wait_timeout_seconds=0.05, audit_log="")
gh = PollingGH(flips=10**6)
t0 = time.time()
ev = pb.wait_until_actionable(gh, cfg(**SLOW), "o", "r", 1, ME)
chk("gives up at the timeout", time.time() - t0 < 2)
chk("timeout keeps the pending verdict", ev["verdict"] == "CHECKS_PENDING")
chk("timeout says so in the action",
    "timeout" in ev.get("action", ""))
chk("a timed-out PR is not actionable",
    ev["verdict"] not in pb.actionable_verdicts(cfg()))

# a terminal non-actionable verdict must not be polled for 30 minutes
class ConflictGH(FakeGH):
    def __init__(self):
        super().__init__(mkpr("dirty", mergeable=False), checkruns=PASSING)
        self.polls = 0
    def get(self, path, params=None):
        if path.endswith("/pulls/1"): self.polls += 1
        return super().get(path, params)


gh = ConflictGH()
ev = pb.wait_until_actionable(gh, cfg(**FAST), "o", "r", 1, ME)
chk("a conflict returns immediately", ev["verdict"] == "CONFLICT" and gh.polls == 1)

ok = sum(res)
print(f"\n{ok}/{len(res)} passed")
sys.exit(0 if ok == len(res) else 1)
