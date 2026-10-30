"""
Mergeability tests: a red check only blocks when the base branch requires it.

GitHub applies branch protection when it computes mergeable_state, so that —
not our reading of the raw check list — decides whether a failure blocks.
Fixtures mirror real PRs observed on vmockinc/career-service-ui.
"""
import os, sys
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

ok = sum(res)
print(f"\n{ok}/{len(res)} passed")
sys.exit(0 if ok == len(res) else 1)
