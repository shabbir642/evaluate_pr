#!/usr/bin/env python3
"""
prwatch — event-driven trigger for prboard.

Listens to a Slack channel via Socket Mode (no public URL needed). When a
message mentions YOU and contains a GitHub PR link, it starts watching that
PR, waits for its checks to go green, then hands it to prboard's action engine
(approve + merge, but ONLY if every safety gate passes — never master/prod,
never a red flag).

Runtime deps:  pip install slack_sdk
Env required:
    SLACK_APP_TOKEN   xapp-...   (Socket Mode app-level token, scope connections:write)
    SLACK_BOT_TOKEN   xoxb-...   (bot token; bot must be in the channel)
    GITHUB_TOKEN      (gh auth token, or a PAT with repo + pull_requests:write)
    PRBOARD_SLACK_USER   your Slack user id (e.g. U02LS4K29KQ) — the mention it reacts to
Env optional:
    PRBOARD_DRY_RUN=1    log what it WOULD do, change nothing (recommended first)
    GITHUB_API_URL       default https://api.github.com
    PRBOARD_ALLOWED_SENDERS   comma-separated Slack user ids allowed to trigger it.
                              Unset = any human in the channel (apps are always
                              ignored). Set it to lock the trigger down to your team.
    PRBOARD_HTTP_TIMEOUT      seconds per GitHub request (default 30)

Start it:  ./prwatch.py       (or run under launchd — see com.vmock.prwatch.plist)
"""

import os
import sys
import threading
import importlib.util

# --- load prboard (same directory) as the engine ---------------------------
_here = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("prboard", os.path.join(_here, "prboard.py"))
pb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pb)

try:
    from slack_sdk.socket_mode import SocketModeClient
    from slack_sdk.socket_mode.request import SocketModeRequest
    from slack_sdk.socket_mode.response import SocketModeResponse
    from slack_sdk.web import WebClient
except ImportError:
    sys.exit("prwatch needs slack_sdk:  pip install slack_sdk")


CFG = pb.load_config(os.environ.get("PRBOARD_CONFIG"))
MY_SLACK_ID = os.environ.get("PRBOARD_SLACK_USER", "")
CHANNEL = CFG.get("slack_channel", "")
DRY_RUN = bool(os.environ.get("PRBOARD_DRY_RUN"))
ALLOWED_SENDERS = {u.strip() for u in
                   os.environ.get("PRBOARD_ALLOWED_SENDERS", "").split(",")
                   if u.strip()}

_inflight = set()
_lock = threading.Lock()


def log(msg):
    import datetime
    print(f"{datetime.datetime.now().isoformat(timespec='seconds')} {msg}", flush=True)


def _notify(web, text):
    """Best-effort DM to yourself so you see what happened."""
    if not MY_SLACK_ID:
        return
    try:
        web.chat_postMessage(channel=MY_SLACK_ID, text=text)
    except Exception as e:
        log(f"notify failed: {e}")


def handle_pr(owner, repo, number, web):
    key = (owner, repo, number)
    with _lock:
        if key in _inflight:
            log(f"{owner}/{repo}#{number} already in flight; skipping")
            return
        _inflight.add(key)
    try:
        gh = pb.GitHub(os.environ.get("GITHUB_TOKEN"),
                       os.environ.get("GITHUB_API_URL", "https://api.github.com"))
        me = gh.login()
        log(f"{owner}/{repo}#{number} watching (waiting for checks)…")
        ev = pb.wait_until_actionable(gh, CFG, owner, repo, number, me)
        if ev["verdict"] in ("NEEDS_YOUR_APPROVAL", "READY_TO_MERGE", "APPROVED_WAITING"):
            ev = pb.apply_pr(gh, CFG, owner, repo, number, me, dry_run=DRY_RUN)
        action = ev.get("action", "no action")
        label = pb.VERDICT_LABEL.get(ev["verdict"], ev["verdict"])
        log(f"{owner}/{repo}#{number} [{label}] -> {action}")
        # Only ping you when something meaningful happened or needs you.
        if any(w in action for w in ("APPROVED", "MERGED", "failed", "would")) \
                or ev["verdict"] in ("MANUAL_REVIEW", "CONFLICT", "CHECKS_FAILED",
                                     "CHANGES_REQUESTED"):
            mode = " [DRY-RUN]" if DRY_RUN else ""
            _notify(web, f"{owner}/{repo}#{number} [{label}]{mode}\n"
                         f"{action}\n{ev['url']}")
    except Exception as e:
        log(f"{owner}/{repo}#{number} ERROR: {e}")
    finally:
        with _lock:
            _inflight.discard(key)


def on_event(client, req):
    if req.type != "events_api":
        return
    client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
    event = (req.payload or {}).get("event", {})
    if event.get("type") != "message":
        return
    if event.get("subtype"):          # ignore edits, joins, bot_message, etc.
        return
    if event.get("channel") != CHANNEL:
        return
    # subtype alone does not reliably exclude apps: a Slack app posting with a
    # bot token produces a plain message event carrying bot_id/app_id. Without
    # this, a GitHub or Jira integration posting a PR link that mentions you
    # would approve and merge it with no human having asked.
    if event.get("bot_id") or event.get("app_id"):
        log(f"ignoring app-posted message (bot_id={event.get('bot_id')})")
        return
    sender = event.get("user")
    if not sender:
        return
    if sender == MY_SLACK_ID:         # your own messages aren't a request to you
        return
    if ALLOWED_SENDERS and sender not in ALLOWED_SENDERS:
        log(f"ignoring message from {sender} (not in PRBOARD_ALLOWED_SENDERS)")
        return
    text = event.get("text", "") or ""
    # React only when YOU are mentioned (your rule: "sends PR and mentions me").
    if MY_SLACK_ID and f"<@{MY_SLACK_ID}>" not in text:
        return
    prs = set()
    for m in pb.GITHUB_PR_URL_RE.finditer(text):
        prs.add((m.group(1), m.group(2), int(m.group(3))))
    for owner, repo, number in prs:
        log(f"trigger: {owner}/{repo}#{number} from Slack message by {sender}")
        threading.Thread(target=handle_pr, args=(owner, repo, number, client.web_client),
                         daemon=True).start()


def main():
    missing = [v for v in ("SLACK_APP_TOKEN", "SLACK_BOT_TOKEN", "GITHUB_TOKEN")
               if not os.environ.get(v)]
    if missing:
        sys.exit("missing env: " + ", ".join(missing))
    if not MY_SLACK_ID:
        sys.exit("set PRBOARD_SLACK_USER to your Slack user id (e.g. U02LS4K29KQ)")
    if not CHANNEL:
        sys.exit("no slack_channel in config")
    for problem in pb.validate_gate_rules(pb.gate_rules_for(CFG)):
        log(f"WARNING: gate_rules — {problem}")

    client = SocketModeClient(
        app_token=os.environ["SLACK_APP_TOKEN"],
        web_client=WebClient(token=os.environ["SLACK_BOT_TOKEN"]),
    )
    client.socket_mode_request_listeners.append(on_event)
    mode = "DRY-RUN" if DRY_RUN else ("LIVE, auto-merge "
            + ("ON" if CFG.get("enable_auto_merge") else "OFF"))
    senders = ",".join(sorted(ALLOWED_SENDERS)) if ALLOWED_SENDERS else "any human"
    log(f"prwatch connecting — channel {CHANNEL}, reacting to <@{MY_SLACK_ID}> "
        f"from {senders} [{mode}]")
    client.connect()
    threading.Event().wait()   # run forever


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
