"""
Discovery-window tests: the date gate that keeps a run from evaluating every
stale open PR. Each candidate costs ~6 GitHub calls, so what gets discovered
is what determines how long a run takes.
"""
import datetime
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _loader import pb

UTC = datetime.timezone.utc
res = []
def chk(name, cond):
    res.append(bool(cond)); print(f"[{'PASS' if cond else 'FAIL'}] {name}")


# --- window_start -----------------------------------------------------------
now = datetime.datetime.now(UTC)
w = pb.window_start({})
chk("defaults to 24h", w is not None and 23.9 < (now - w).total_seconds() / 3600 < 24.1)
chk("config value is honoured",
    1.9 < (now - pb.window_start({"discovery_window_hours": 2})).total_seconds() / 3600 < 2.1)
chk("explicit hours override config",
    0.9 < (now - pb.window_start({"discovery_window_hours": 99}, 1)).total_seconds() / 3600 < 1.1)
chk("0 disables the window", pb.window_start({}, 0) is None)
chk("negative disables the window", pb.window_start({}, -5) is None)
chk("garbage disables rather than crashes", pb.window_start({}, "soon") is None)
chk("window is timezone-aware", pb.window_start({}).tzinfo is not None)

chk("_parse_gh_time reads GitHub's format",
    pb._parse_gh_time("2026-10-30T09:15:00Z") ==
    datetime.datetime(2026, 10, 30, 9, 15, tzinfo=UTC))
chk("_parse_gh_time tolerates junk", pb._parse_gh_time("nonsense") is None)
chk("_parse_gh_time tolerates None", pb._parse_gh_time(None) is None)


# --- the search queries carry the window ------------------------------------
class QueryGH:
    def __init__(self): self.queries = []
    def get_paginated(self, path, params=None, cap=300):
        self.queries.append(params["q"])
        return [{"html_url": "https://github.com/o/r/pull/7"}]


since = datetime.datetime(2026, 10, 29, 12, 0, 0, tzinfo=UTC)
gh = QueryGH()
pb.discover_from_github(gh, "shabbir", since=since)
chk("search carries updated:>=",
    all("updated:>=2026-10-29T12:00:00Z" in q for q in gh.queries))
chk("search still filters to open PRs", all("is:open is:pr" in q for q in gh.queries))

gh = QueryGH()
pb.discover_from_github(gh, "shabbir", since=None)
chk("no window -> no updated: qualifier", not any("updated:" in q for q in gh.queries))

gh = QueryGH()
pb.discover_from_github(gh, "shabbir", include_mentions=False, since=since)
chk("window and include_mentions compose",
    len(gh.queries) == 1 and "updated:>=" in gh.queries[0]
    and "mentions:" not in gh.queries[0])

# the window must never widen what we act on
gh = QueryGH()
pb.discover_from_github(gh, "shabbir", include_mentions=False, since=None)
chk("no window still excludes mentions", not any("mentions:" in q for q in gh.queries))


# --- Slack asks the server to trim ------------------------------------------
captured = {}
def fake_request(url, headers, method="GET", data=None, timeout=None):
    captured["url"] = url
    return {"ok": True, "messages": [
        {"text": "please review https://github.com/o/r/pull/9"}]}, {}


real_request, pb._request = pb._request, fake_request
os.environ["SLACK_TOKEN"] = "xoxp-test"
try:
    found = pb.discover_from_slack({"slack_channel": "C1"}, since=since)
    import urllib.parse as _up
    q = dict(_up.parse_qsl(_up.urlparse(captured["url"]).query))
    chk("slack sets oldest to the window start",
        q.get("oldest") == f"{since.timestamp():.6f}")
    chk("slack keeps the channel and limit",
        q.get("channel") == "C1" and q.get("limit") == "200")
    chk("slack still parses PR links", found == {("o", "r", 9)})
    captured.clear()
    pb.discover_from_slack({"slack_channel": "C1"}, since=None)
    chk("no window -> no oldest param", "oldest=" not in captured["url"])
finally:
    pb._request = real_request
    os.environ.pop("SLACK_TOKEN", None)

# --- naming a PR directly: --pr takes the forms people actually paste ------
FULL = "https://github.com/vmockinc/career-service-ui/pull/833"
WANT = ("vmockinc", "career-service-ui", 833)
for text in (FULL,
             FULL + "/files",
             FULL + "#discussion_r1",
             FULL + "?w=1",
             "http://github.com/vmockinc/career-service-ui/pull/833",
             "github.com/vmockinc/career-service-ui/pull/833",
             "www.github.com/vmockinc/career-service-ui/pull/833",
             "ghe.corp.example/vmockinc/career-service-ui/pull/833",
             "vmockinc/career-service-ui/pull/833",
             "vmockinc/career-service-ui#833",
             "vmockinc/career-service-ui/833",
             "   vmockinc/career-service-ui#833   "):
    chk(f"parses {text.strip()[:46]}", pb.parse_pr_ref(text) == WANT)

for bad in ("833", "vmockinc/career-service-ui", "not a pr", "", None,
            "https://github.com/vmockinc/career-service-ui",
            "vmockinc/career-service-ui#abc"):
    chk(f"rejects {bad!r}", pb.parse_pr_ref(bad) is None)

# the strict scanner must stay strict: it reads arbitrary Slack text, where
# "owner/repo#123" style noise would produce false positives.
chk("free-text scanner still ignores shorthand",
    pb.GITHUB_PR_URL_RE.search("see vmockinc/career-service-ui#833") is None)
chk("free-text scanner still finds real links",
    pb.GITHUB_PR_URL_RE.search(f"please review {FULL} today") is not None)

ok = sum(res)
print(f"\n{ok}/{len(res)} passed")
sys.exit(0 if ok == len(res) else 1)
