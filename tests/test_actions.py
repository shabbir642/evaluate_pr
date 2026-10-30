import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _loader import pb

ME = "shabbir"
def cfg(**over):
    c = dict(pb.DEFAULT_CONFIG); c["audit_log"] = ""; c.update(over); return c

class FakeGH:
    def __init__(self, pr, reviews=None, checkruns=None, protection=None, files=None):
        self._pr=pr; self._reviews=list(reviews or [])
        self._checkruns=checkruns or {"check_runs":[{"name":"ci","status":"completed","conclusion":"success"}]}
        self._protection=protection; self._files=files or []; self.posts=[]; self.puts=[]
    def login(self): return ME
    def get(self, path, params=None):
        if path.endswith(f"/pulls/{self._pr['number']}"): return self._pr
        if "/check-runs" in path: return self._checkruns
        if path.endswith("/status"): return {"statuses":[]}
        if "/protection" in path:
            if self._protection is None: raise pb.ApiError(403,"no access")
            return self._protection
        raise pb.ApiError(404, path)
    def get_paginated(self, path, params=None, cap=300):
        if "/reviews" in path: return self._reviews
        if "/files" in path: return self._files
        return []
    def post(self, path, body):
        self.posts.append((path, body))
        if body.get("event")=="APPROVE":
            self._reviews.append({"state":"APPROVED","user":{"login":ME}})
        return {}
    def put(self, path, body):
        self.puts.append((path, body)); return {"merged":True}

def mkpr(number=1, base="develop", author="alice"):
    return {"number":number,"state":"open","merged":False,"title":f"PR {number}",
            "base":{"ref":base},"head":{"sha":"deadbeefcafe"},"user":{"login":author,"type":"User"},
            "author_association":"MEMBER","draft":False,"mergeable":True,
            "mergeable_state":"clean","changed_files":3,"additions":10,"deletions":5,"labels":[]}

prot1={"required_pull_request_reviews":{"required_approving_review_count":1},"required_status_checks":{"contexts":[]}}
prot2={"required_pull_request_reviews":{"required_approving_review_count":2},"required_status_checks":{"contexts":[]}}
approve=lambda w:{"state":"APPROVED","user":{"login":w}}

results=[]
def check(name, cond): results.append(cond); print(f"[{'PASS' if cond else 'FAIL'}] {name}")

gh=FakeGH(mkpr(), protection=prot1)
ev=pb.apply_pr(gh,cfg(enable_auto_merge=True),"o","r",1,ME,dry_run=True)
check("dry-run makes no writes", gh.posts==[] and gh.puts==[])
check("dry-run says would approve+merge", "would APPROVE" in ev["action"] and "would MERGE" in ev["action"])

gh=FakeGH(mkpr(), protection=prot1)
ev=pb.apply_pr(gh,cfg(enable_auto_merge=True),"o","r",1,ME,dry_run=False)
check("live approves once", len(gh.posts)==1 and gh.posts[0][1]["event"]=="APPROVE")
check("live approve pins sha", gh.posts[0][1]["commit_id"]=="deadbeefcafe")
check("live merges once", len(gh.puts)==1 and gh.puts[0][1]["sha"]=="deadbeefcafe")
check("merge method squash", gh.puts[0][1]["merge_method"]=="squash")
check("action reports MERGED", "MERGED" in ev["action"])

gh=FakeGH(mkpr(base="master"), protection=prot1)
ev=pb.apply_pr(gh,cfg(enable_auto_merge=True),"o","r",1,ME)
check("master: no writes", gh.posts==[] and gh.puts==[])
check("master: no action", ev["verdict"]=="MANUAL_REVIEW" and "no action" in ev["action"])

gh=FakeGH(mkpr(), protection=prot1)
ev=pb.apply_pr(gh,cfg(enable_auto_merge=False),"o","r",1,ME)
check("auto-merge off: approved", len(gh.posts)==1)
check("auto-merge off: not merged", gh.puts==[] and "auto-merge disabled" in ev["action"])

os.environ["PRBOARD_DISABLE"]="1"
gh=FakeGH(mkpr(), protection=prot1)
ev=pb.apply_pr(gh,cfg(enable_auto_merge=True),"o","r",1,ME)
check("kill switch: no writes", gh.posts==[] and gh.puts==[] and "disabled" in ev["action"])
del os.environ["PRBOARD_DISABLE"]

gh=FakeGH(mkpr(), reviews=[approve("bob")], protection=prot2)
ev=pb.apply_pr(gh,cfg(enable_auto_merge=True),"o","r",1,ME)
check("2-approver: approved then merged", len(gh.posts)==1 and len(gh.puts)==1)

gh=FakeGH(mkpr(), protection=prot1, files=[{"filename":".github/workflows/x.yml"}])
ev=pb.apply_pr(gh,cfg(enable_auto_merge=True),"o","r",1,ME)
check("sensitive path: no writes", gh.posts==[] and gh.puts==[] and ev["verdict"]=="MANUAL_REVIEW")

ok=sum(1 for c in results if c)
print(f"\n{ok}/{len(results)} passed")
sys.exit(0 if ok==len(results) else 1)
