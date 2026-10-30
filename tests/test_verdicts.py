import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _loader import pb

ME = "shabbir"
CFG = dict(pb.DEFAULT_CONFIG)

class FakeGH:
    def __init__(self, pr, reviews=None, checkruns=None, status=None,
                 protection=None, files=None):
        self._pr=pr; self._reviews=reviews or []; self._checkruns=checkruns or {"check_runs":[]}
        self._status=status or {"statuses":[]}; self._protection=protection; self._files=files or []
    def login(self): return ME
    def get(self, path, params=None):
        if path.endswith(f"/pulls/{self._pr['number']}"): return self._pr
        if "/check-runs" in path: return self._checkruns
        if path.endswith("/status"): return self._status
        if "/protection" in path:
            if self._protection is None: raise pb.ApiError(403, "no access")
            return self._protection
        raise pb.ApiError(404, path)
    def get_paginated(self, path, params=None, cap=300):
        if "/reviews" in path: return self._reviews
        if "/files" in path: return self._files
        return []

def mkpr(number=1, base="develop", author="alice", draft=False, mergeable=True,
         mergeable_state="clean", changed_files=3, additions=10, deletions=5,
         labels=None, assoc="MEMBER", utype="User"):
    return {"number":number,"state":"open","merged":False,"title":f"PR {number}",
            "base":{"ref":base},"head":{"sha":"abc"},"user":{"login":author,"type":utype},
            "author_association":assoc,"draft":draft,"mergeable":mergeable,
            "mergeable_state":mergeable_state,"changed_files":changed_files,
            "additions":additions,"deletions":deletions,"labels":labels or []}

def verdict(gh): return pb.evaluate_pr(gh, CFG, "o", "r", gh._pr["number"], ME)["verdict"]

passing = {"check_runs":[{"name":"ci","status":"completed","conclusion":"success"}]}
prot1 = {"required_pull_request_reviews":{"required_approving_review_count":1},"required_status_checks":{"contexts":[]}}
prot2 = {"required_pull_request_reviews":{"required_approving_review_count":2},"required_status_checks":{"contexts":[]}}
approve = lambda who: {"state":"APPROVED","user":{"login":who}}

cases = [
 ("master base -> MANUAL_REVIEW", FakeGH(mkpr(base="master"), checkruns=passing, protection=prot1, reviews=[approve("bob")]), "MANUAL_REVIEW"),
 ("conflict -> CONFLICT", FakeGH(mkpr(mergeable=False, mergeable_state="dirty"), checkruns=passing, protection=prot1), "CONFLICT"),
 ("green unapproved -> NEEDS_YOUR_APPROVAL", FakeGH(mkpr(), checkruns=passing, protection=prot1), "NEEDS_YOUR_APPROVAL"),
 ("approved+clean -> READY_TO_MERGE", FakeGH(mkpr(), checkruns=passing, protection=prot1, reviews=[approve("bob")]), "READY_TO_MERGE"),
 ("own PR -> SKIP_OWN_PR", FakeGH(mkpr(author=ME), checkruns=passing, protection=prot1), "SKIP_OWN_PR"),
 ("workflow file -> MANUAL_REVIEW", FakeGH(mkpr(), checkruns=passing, protection=prot1, files=[{"filename":".github/workflows/deploy.yml"}]), "MANUAL_REVIEW"),
 ("failing check -> CHECKS_FAILED", FakeGH(mkpr(), checkruns={"check_runs":[{"name":"ci","status":"completed","conclusion":"failure"}]}, protection=prot1), "CHECKS_FAILED"),
 ("pending check -> CHECKS_PENDING", FakeGH(mkpr(), checkruns={"check_runs":[{"name":"ci","status":"in_progress","conclusion":None}]}, protection=prot1), "CHECKS_PENDING"),
 ("2-approver you-only -> APPROVED_WAITING", FakeGH(mkpr(), checkruns=passing, protection=prot2, reviews=[approve(ME)]), "APPROVED_WAITING"),
 ("2-approver satisfied -> READY_TO_MERGE", FakeGH(mkpr(), checkruns=passing, protection=prot2, reviews=[approve("bob"), approve(ME)]), "READY_TO_MERGE"),
 ("changes requested -> CHANGES_REQUESTED", FakeGH(mkpr(), checkruns=passing, protection=prot1, reviews=[{"state":"CHANGES_REQUESTED","user":{"login":"bob"}}]), "CHANGES_REQUESTED"),
 ("large diff -> MANUAL_REVIEW", FakeGH(mkpr(changed_files=99), checkruns=passing, protection=prot1, reviews=[approve("bob")]), "MANUAL_REVIEW"),
 ("bot author -> MANUAL_REVIEW", FakeGH(mkpr(author="dependabot[bot]", utype="Bot"), checkruns=passing, protection=prot1, reviews=[approve("bob")]), "MANUAL_REVIEW"),
]

ok=0
for name, gh, expected in cases:
    got = verdict(gh); mark = "PASS" if got==expected else "FAIL"
    if got==expected: ok+=1
    print(f"[{mark}] {name:42s} got={got}")
print(f"\n{ok}/{len(cases)} passed")
sys.exit(0 if ok==len(cases) else 1)
