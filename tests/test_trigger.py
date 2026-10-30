"""
Trigger-scope tests: what is allowed to put a PR in front of the action engine.

Covers the GitHub discovery feeds and prwatch's Slack event filter. prwatch
imports slack_sdk at module scope, so it is stubbed here — these tests need no
network, no tokens and no pip install.
"""
import os, sys, types
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _loader import pb

res = []
def chk(name, cond):
    res.append(bool(cond)); print(f"[{'PASS' if cond else 'FAIL'}] {name}")


# --- GitHub discovery: mentions: is far broader than review-requested: -----
class QueryRecordingGH:
    def __init__(self): self.queries = []
    def get_paginated(self, path, params=None, cap=300):
        self.queries.append(params["q"])
        return [{"html_url": "https://github.com/o/r/pull/7"}]


gh = QueryRecordingGH()
pb.discover_from_github(gh, "shabbir", include_mentions=True)
chk("default discovery asks for review-requested", any("review-requested:" in q for q in gh.queries))
chk("default discovery asks for mentions", any("mentions:" in q for q in gh.queries))

gh = QueryRecordingGH()
found = pb.discover_from_github(gh, "shabbir", include_mentions=False)
chk("include_mentions=False drops the mentions query",
    not any("mentions:" in q for q in gh.queries))
chk("include_mentions=False keeps review-requested",
    any("review-requested:" in q for q in gh.queries))
chk("discovery still parses PR urls", found == {("o", "r", 7)})


# --- prwatch event filter ---------------------------------------------------
def load_prwatch(**env):
    """Import prwatch fresh with slack_sdk stubbed and a given environment."""
    for mod in ("slack_sdk", "slack_sdk.socket_mode", "slack_sdk.socket_mode.request",
                "slack_sdk.socket_mode.response", "slack_sdk.web"):
        m = types.ModuleType(mod); sys.modules[mod] = m
    sys.modules["slack_sdk.socket_mode"].SocketModeClient = object
    sys.modules["slack_sdk.socket_mode.request"].SocketModeRequest = object
    sys.modules["slack_sdk.socket_mode.response"].SocketModeResponse = lambda **kw: None
    sys.modules["slack_sdk.web"].WebClient = object

    saved = {k: os.environ.get(k) for k in
             ("PRBOARD_SLACK_USER", "PRBOARD_ALLOWED_SENDERS", "PRBOARD_CONFIG")}
    os.environ.update({k: v for k, v in env.items()})
    for k in saved:
        if k not in env:
            os.environ.pop(k, None)
    try:
        import importlib.util
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        spec = importlib.util.spec_from_file_location(
            "prwatch_t", os.path.join(root, "prwatch.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k, v in saved.items():
            if v is None: os.environ.pop(k, None)
            else: os.environ[k] = v


ME_SLACK = "U02LS4K29KQ"
LINK = "https://github.com/o/r/pull/7"


class FakeReq:
    type = "events_api"
    envelope_id = "e1"
    def __init__(self, event): self.payload = {"event": event}


class FakeClient:
    web_client = None
    def send_socket_mode_response(self, resp): pass


def triggers(mod, event, channel=None):
    """Run on_event and report whether it started a watcher."""
    event = dict(event)
    event.setdefault("type", "message")
    event.setdefault("channel", channel or mod.CHANNEL)
    fired = []
    mod.handle_pr = lambda *a, **k: fired.append(a)
    orig = mod.threading.Thread
    mod.threading.Thread = lambda target, args, daemon=True: types.SimpleNamespace(
        start=lambda: target(*args))
    try:
        mod.on_event(FakeClient(), FakeReq(event))
    finally:
        mod.threading.Thread = orig
    return bool(fired)


pw = load_prwatch(PRBOARD_SLACK_USER=ME_SLACK)
chk("human mention with a PR link triggers",
    triggers(pw, {"user": "U_TEAMMATE", "text": f"<@{ME_SLACK}> please review {LINK}"}))
chk("app-posted message is ignored (bot_id)",
    not triggers(pw, {"user": "U_BOT", "bot_id": "B123",
                      "text": f"<@{ME_SLACK}> review {LINK}"}))
chk("app-posted message is ignored (app_id)",
    not triggers(pw, {"user": "U_BOT", "app_id": "A123",
                      "text": f"<@{ME_SLACK}> review {LINK}"}))
chk("subtype is still ignored",
    not triggers(pw, {"user": "U_T", "subtype": "message_changed",
                      "text": f"<@{ME_SLACK}> {LINK}"}))
chk("message with no sender is ignored",
    not triggers(pw, {"text": f"<@{ME_SLACK}> {LINK}"}))
chk("your own message does not trigger you",
    not triggers(pw, {"user": ME_SLACK, "text": f"<@{ME_SLACK}> {LINK}"}))
chk("no mention -> no trigger",
    not triggers(pw, {"user": "U_T", "text": f"anyone free for {LINK}?"}))
chk("mention without a PR link -> no trigger",
    not triggers(pw, {"user": "U_T", "text": f"<@{ME_SLACK}> standup?"}))
chk("other channel -> no trigger",
    not triggers(pw, {"user": "U_T", "text": f"<@{ME_SLACK}> {LINK}"}, channel="COTHER"))

pw = load_prwatch(PRBOARD_SLACK_USER=ME_SLACK, PRBOARD_ALLOWED_SENDERS="U_OK, U_ALSO_OK")
chk("allowlisted sender triggers",
    triggers(pw, {"user": "U_OK", "text": f"<@{ME_SLACK}> {LINK}"}))
chk("allowlist tolerates whitespace",
    triggers(pw, {"user": "U_ALSO_OK", "text": f"<@{ME_SLACK}> {LINK}"}))
chk("non-allowlisted sender is ignored",
    not triggers(pw, {"user": "U_STRANGER", "text": f"<@{ME_SLACK}> {LINK}"}))

ok = sum(res)
print(f"\n{ok}/{len(res)} passed")
sys.exit(0 if ok == len(res) else 1)
