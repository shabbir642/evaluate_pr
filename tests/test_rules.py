import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _loader import pb

ME="shabbir"
class FakeGH:
    def __init__(self, pr, reviews=None, files=None):
        self._pr=pr; self._reviews=reviews or []; self._files=files or []
        self._cr={"check_runs":[{"name":"ci","status":"completed","conclusion":"success"}]}
        self._prot={"required_pull_request_reviews":{"required_approving_review_count":1},"required_status_checks":{"contexts":[]}}
    def login(self): return ME
    def get(self,p,params=None):
        if p.endswith("/pulls/1"): return self._pr
        if "/check-runs" in p: return self._cr
        if p.endswith("/status"): return {"statuses":[]}
        if "/protection" in p: return self._prot
        raise pb.ApiError(404,p)
    def get_paginated(self,p,params=None,cap=300):
        return self._reviews if "/reviews" in p else (self._files if "/files" in p else [])

def mkpr(base="develop",title="PR 1",labels=None):
    return {"number":1,"state":"open","merged":False,"title":title,"base":{"ref":base},
            "head":{"sha":"abc"},"user":{"login":"alice","type":"User"},"author_association":"MEMBER",
            "draft":False,"mergeable":True,"mergeable_state":"clean","changed_files":3,
            "additions":5,"deletions":5,"labels":labels or []}
def cfg(rules): c=dict(pb.DEFAULT_CONFIG); c["gate_rules"]=rules; return c
def V(gh,c): return pb.evaluate_pr(gh,c,"o","r",1,ME)["verdict"]

res=[]
def chk(n,cond): res.append(cond); print(f"[{'PASS' if cond else 'FAIL'}] {n}")

rules=[{"name":"hotfix","action":"manual","when":{"title_regex":"(?i)hotfix"}}]
chk("added title rule forces MANUAL", V(FakeGH(mkpr(title="Hotfix: urgent")), cfg(rules))=="MANUAL_REVIEW")
chk("non-matching title not manual", V(FakeGH(mkpr(title="normal change")), cfg(rules))=="NEEDS_YOUR_APPROVAL")

rules=[{"name":"needs-qa","action":"manual","when":{"labels_any":["needs-qa"]}}]
chk("added label rule forces MANUAL", V(FakeGH(mkpr(labels=[{"name":"needs-qa"}])), cfg(rules))=="MANUAL_REVIEW")

rules=[{"name":"wip","action":"skip","when":{"labels_any":["wip"]}}]
chk("skip rule -> DRAFT_OR_HOLD", V(FakeGH(mkpr(labels=[{"name":"WIP"}])), cfg(rules))=="DRAFT_OR_HOLD")

chk("empty rules: master no longer manual", V(FakeGH(mkpr(base="master")), cfg([]))=="NEEDS_YOUR_APPROVAL")

rules=[{"name":"outside allowlist","action":"skip","when":{"repo_not_in":["o/other"]}}]
chk("repo_not_in skips non-allowlisted repo", V(FakeGH(mkpr()), cfg(rules))=="DRAFT_OR_HOLD")

ok=sum(res); print(f"\n{ok}/{len(res)} passed"); sys.exit(0 if ok==len(res) else 1)
