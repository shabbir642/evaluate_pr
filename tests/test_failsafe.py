"""
Degraded-path tests: every gate must fail CLOSED.

The happy paths are covered by test_verdicts / test_actions / test_rules.
These cover what happens when an input is missing, unreadable or truncated,
and when a push lands between gating and acting.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _loader import pb

ME = "shabbir"
PASSING = {"check_runs": [{"name": "ci", "status": "completed", "conclusion": "success"}]}
PROT1 = {"required_pull_request_reviews": {"required_approving_review_count": 1},
         "required_status_checks": {"contexts": []}}
approve = lambda who: {"state": "APPROVED", "user": {"login": who}}


def cfg(**over):
    c = dict(pb.DEFAULT_CONFIG); c["audit_log"] = ""; c.update(over); return c


def mkpr(number=1, base="develop", author="alice", sha="abc", changed_files=3):
    return {"number": number, "state": "open", "merged": False, "title": f"PR {number}",
            "base": {"ref": base}, "head": {"sha": sha},
            "user": {"login": author, "type": "User"}, "author_association": "MEMBER",
            "draft": False, "mergeable": True, "mergeable_state": "clean",
            "changed_files": changed_files, "additions": 10, "deletions": 5, "labels": []}


class FakeGH:
    """GitHub double with per-endpoint failure injection."""

    def __init__(self, pr, reviews=None, checkruns=None, files=None, fail=()):
        self._pr = pr; self._reviews = list(reviews or [])
        self._cr = PASSING if checkruns is None else checkruns
        self._files = files if files is not None else [{"filename": "src/util.js"}]
        self._fail = set(fail); self.posts = []; self.puts = []

    def login(self): return ME

    def get(self, path, params=None):
        if path.endswith(f"/pulls/{self._pr['number']}"): return self._pr
        if "/check-runs" in path:
            if "checkruns" in self._fail: raise pb.ApiError(502, "bad gateway")
            return self._cr
        if path.endswith("/status"):
            if "status" in self._fail: raise pb.ApiError(502, "bad gateway")
            return {"statuses": []}
        if "/protection" in path: return PROT1
        raise pb.ApiError(404, path)

    def get_paginated(self, path, params=None, cap=300):
        if "/reviews" in path:
            if "reviews" in self._fail: raise pb.ApiError(502, "bad gateway")
            return self._reviews
        if "/files" in path:
            if "files" in self._fail: raise pb.ApiError(502, "bad gateway")
            return self._files
        return []

    def post(self, path, body):
        self.posts.append(body)
        if body.get("event") == "APPROVE":
            self._reviews.append(approve(ME))
        return {}

    def put(self, path, body): self.puts.append(body); return {"merged": True}


res = []
def chk(name, cond):
    res.append(bool(cond)); print(f"[{'PASS' if cond else 'FAIL'}] {name}")


def verdict(gh, c=None):
    return pb.evaluate_pr(gh, c or cfg(), "o", "r", 1, ME)["verdict"]


# --- 1. '**/' globs must match at the repository root -----------------------
for path in ("main.tf", "migrations/001_add_col.sql", ".env",
             "secrets.yaml", "k8s/deploy.yaml", "helm/values.yaml"):
    gh = FakeGH(mkpr(), reviews=[approve("bob")], files=[{"filename": path}])
    chk(f"root-level {path} -> MANUAL_REVIEW", verdict(gh) == "MANUAL_REVIEW")

# ...and must still match when nested, and still ignore innocent files.
gh = FakeGH(mkpr(), reviews=[approve("bob")], files=[{"filename": "infra/main.tf"}])
chk("nested infra/main.tf -> MANUAL_REVIEW", verdict(gh) == "MANUAL_REVIEW")
gh = FakeGH(mkpr(), reviews=[approve("bob")], files=[{"filename": "src/components/Card.jsx"}])
chk("ordinary source file -> READY_TO_MERGE", verdict(gh) == "READY_TO_MERGE")
chk("_match_path: '**/x' matches bare x", pb._match_path("x", "**/x"))
chk("_match_path: '**/x' still matches a/b/x", pb._match_path("a/b/x", "**/x"))
chk("_match_path: no false positive", not pb._match_path("xy", "**/x"))

# --- 2. absent checks are not green -----------------------------------------
gh = FakeGH(mkpr(), reviews=[approve("bob")], checkruns={"check_runs": []})
chk("no checks reported -> CHECKS_PENDING", verdict(gh) == "CHECKS_PENDING")
gh = FakeGH(mkpr(), reviews=[approve("bob")], checkruns={"check_runs": []})
chk("no checks + allow_no_checks -> READY_TO_MERGE",
    verdict(gh, cfg(allow_no_checks=True)) == "READY_TO_MERGE")

gh = FakeGH(mkpr(), reviews=[approve("bob")], checkruns={"check_runs": []})
ev = pb.apply_pr(gh, cfg(enable_auto_merge=True), "o", "r", 1, ME)
chk("no checks: never approves or merges", gh.posts == [] and gh.puts == [])

# --- 3. unreadable gate inputs fail closed ----------------------------------
gh = FakeGH(mkpr(), reviews=[approve("bob")], fail=("files",))
chk("files fetch 502 -> MANUAL_REVIEW", verdict(gh) == "MANUAL_REVIEW")
gh = FakeGH(mkpr(), reviews=[approve("bob")], fail=("files",))
ev = pb.apply_pr(gh, cfg(enable_auto_merge=True), "o", "r", 1, ME)
chk("files fetch 502: no writes", gh.posts == [] and gh.puts == [])

gh = FakeGH(mkpr(), reviews=[approve("bob")], fail=("checkruns", "status"))
chk("both check endpoints 502 -> MANUAL_REVIEW", verdict(gh) == "MANUAL_REVIEW")
gh = FakeGH(mkpr(), reviews=[approve("bob")], fail=("reviews",))
chk("reviews fetch 502 -> MANUAL_REVIEW", verdict(gh) == "MANUAL_REVIEW")

# one endpoint up, the other down, and the live one reports a real result
gh = FakeGH(mkpr(), reviews=[approve("bob")], fail=("status",))
chk("check-runs readable, statuses 502 -> READY_TO_MERGE", verdict(gh) == "READY_TO_MERGE")

# truncation means we cannot know every changed path
gh = FakeGH(mkpr(changed_files=900), reviews=[approve("bob")],
            files=[{"filename": f"src/f{i}.js"} for i in range(pb.FILES_CAP)])
ev = pb.evaluate_pr(gh, cfg(gate_rules=[]), "o", "r", 1, ME)
chk("truncated file list -> MANUAL_REVIEW", ev["verdict"] == "MANUAL_REVIEW")
chk("truncation reason mentions the cap",
    any("truncated" in r for r in ev["reasons"]))

# --- 4. a push landing mid-flight must not be acted on ----------------------
class RacingGH(FakeGH):
    """Serves the gated commit once, then a newer, never-gated commit."""

    def __init__(self, **kw):
        super().__init__(mkpr(sha="AAAAAAAcommitA"), **kw)
        self._reads = 0
        self._after = mkpr(sha="BBBBBBBcommitB", changed_files=99)

    def get(self, path, params=None):
        if path.endswith("/pulls/1"):
            self._reads += 1
            return self._pr if self._reads == 1 else self._after
        return super().get(path, params)


gh = RacingGH()
ev = pb.apply_pr(gh, cfg(enable_auto_merge=True), "o", "r", 1, ME, dry_run=False)
chk("approve pins the SHA the gates ran on",
    gh.posts and gh.posts[0]["commit_id"] == "AAAAAAAcommitA")
chk("no merge after the head moves", gh.puts == [])
chk("action says the head moved", "head moved" in ev["action"])

# the 2nd-approver path: READY_TO_MERGE on entry, push lands before the merge
gh = RacingGH(reviews=[approve("bob")])
ev = pb.apply_pr(gh, cfg(enable_auto_merge=True), "o", "r", 1, ME, dry_run=False)
chk("2nd-approver merge pins the gated SHA",
    gh.puts and gh.puts[0]["sha"] == "AAAAAAAcommitA")

# and the ordinary no-race case still approves + merges
gh = FakeGH(mkpr(sha="deadbeefcafe"))
ev = pb.apply_pr(gh, cfg(enable_auto_merge=True), "o", "r", 1, ME, dry_run=False)
chk("no race: approves and merges the same SHA",
    gh.posts[0]["commit_id"] == "deadbeefcafe" and gh.puts[0]["sha"] == "deadbeefcafe")

# --- 5. a gate rule we cannot evaluate is not a gate we can ignore ---------
typo = [{"name": "targets protected branch", "action": "manual",
         "when": {"base_in_typo": ["master"]}}]
gh = FakeGH(mkpr(base="master"), reviews=[approve("bob")])
chk("typo'd manual rule -> MANUAL_REVIEW", verdict(gh, cfg(gate_rules=typo)) == "MANUAL_REVIEW")

# a broken 'skip' rule must also fail closed: a held PR slipping through and
# getting approved is just as bad as a protected branch slipping through.
typo_skip = [{"name": "hold label", "action": "skip",
              "when": {"labels_anyy": ["hold"]}}]
gh = FakeGH(mkpr(), reviews=[approve("bob")])
chk("typo'd skip rule -> MANUAL_REVIEW", verdict(gh, cfg(gate_rules=typo_skip)) == "MANUAL_REVIEW")

gh = FakeGH(mkpr(base="master"), reviews=[approve("bob")])
ev = pb.apply_pr(gh, cfg(gate_rules=typo, enable_auto_merge=True), "o", "r", 1, ME)
chk("typo'd rule: no writes", gh.posts == [] and gh.puts == [])

chk("validate_gate_rules flags the typo",
    any("base_in_typo" in p for p in pb.validate_gate_rules(typo)))
chk("validate_gate_rules flags a bad action",
    any("action" in p for p in pb.validate_gate_rules(
        [{"name": "x", "action": "merge", "when": {"base_in": ["dev"]}}])))
chk("validate_gate_rules flags an empty when",
    any("never fire" in p for p in pb.validate_gate_rules([{"name": "x", "when": {}}])))
chk("validate_gate_rules is quiet on the shipped defaults",
    pb.validate_gate_rules(pb.DEFAULT_CONFIG["gate_rules"]) == [])

# --- 6. a hung request becomes an ApiError, so the gates fail closed -------
class TimingOutGH(FakeGH):
    def get_paginated(self, path, params=None, cap=300):
        if "/files" in path:
            raise pb.ApiError(0, "TimeoutError: timed out")
        return super().get_paginated(path, params, cap)


gh = TimingOutGH(mkpr(), reviews=[approve("bob")])
chk("timed-out files fetch -> MANUAL_REVIEW", verdict(gh) == "MANUAL_REVIEW")
chk("HTTP_TIMEOUT is set and finite", 0 < pb.HTTP_TIMEOUT < 600)

# --- 7. an explicit --config that isn't there is an error, not a fallback --
try:
    pb.load_config("/nonexistent/prboard-config.json")
    chk("missing --config raises", False)
except SystemExit as e:
    chk("missing --config raises", "no such file" in str(e))
chk("load_config(None) still works", pb.load_config(None)["merge_method"] == "squash")

# --- 8. APPROVED_WAITING never claims a negative number of reviewers -------
class BehindGH(FakeGH):
    def __init__(self, **kw):
        pr = mkpr(); pr["mergeable_state"] = "behind"
        super().__init__(pr, **kw)


gh = BehindGH(reviews=[approve("bob"), approve(ME)])
ev = pb.evaluate_pr(gh, cfg(), "o", "r", 1, ME)
chk("behind + over-approved -> APPROVED_WAITING", ev["verdict"] == "APPROVED_WAITING")
chk("no negative 'waiting on' count",
    not any("-1" in r for r in ev["reasons"]))

ok = sum(res)
print(f"\n{ok}/{len(res)} passed")
sys.exit(0 if ok == len(res) else 1)
