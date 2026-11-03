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
    SLACK_BOT_TOKEN   xoxb-...   (bot token; bot must be invited to the channel.
                       Scopes: chat:write plus groups:history for a private
                       channel or channels:history for a public one; subscribe
                       to message.groups / message.channels to match.)
    GITHUB_TOKEN      (gh auth token, or a PAT with repo + pull_requests:write)
    PRBOARD_SLACK_USER   your Slack user id (e.g. U02LS4K29KQ) — the mention it reacts to
Env optional:
    PRBOARD_DRY_RUN=1    log what it WOULD do, change nothing (recommended first)
    GITHUB_API_URL       default https://api.github.com
    PRBOARD_ALLOWED_SENDERS   comma-separated Slack user ids allowed to trigger it.
                              Unset = any human in the channel (apps are always
                              ignored). Set it to lock the trigger down to your team.
    PRBOARD_HTTP_TIMEOUT      seconds per GitHub request (default 30)
    PRBOARD_NOTIFY=0          turn macOS notifications off (default on)
    PRBOARD_NOTIFIER          auto (default) | terminal-notifier | osascript | off.
                              terminal-notifier makes the banner clickable
                              (opens the PR); osascript is built in but posts
                              as "Script Editor" and cannot be clicked.

Start it:  ./prwatch.py       (or run under launchd — see com.vmock.prwatch.plist)
"""

import os
import shutil
import subprocess
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


def env_flag(name, default=False):
    """
    Read a boolean env var the way people expect.

    bool(os.environ.get(...)) makes "0", "false" and "off" all true, so
    PRBOARD_DRY_RUN=0 — the obvious way to turn dry-run off — would leave it
    on and prwatch would silently never act. Unset still means `default`.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("", "0", "false", "no", "off")


CFG = pb.load_config(os.environ.get("PRBOARD_CONFIG"))
MY_SLACK_ID = os.environ.get("PRBOARD_SLACK_USER", "")
CHANNEL = CFG.get("slack_channel", "")
DRY_RUN = env_flag("PRBOARD_DRY_RUN")
ALLOWED_SENDERS = {u.strip() for u in
                   os.environ.get("PRBOARD_ALLOWED_SENDERS", "").split(",")
                   if u.strip()}

_inflight = set()
_lock = threading.Lock()


def log(msg):
    import datetime
    print(f"{datetime.datetime.now().isoformat(timespec='seconds')} {msg}", flush=True)


NOTIFY_TIMEOUT = 10
NOTIFY_MAX_CHARS = 160


def _summary(ev, action):
    """
    One line for the banner: what it did, or — when it did nothing — why.

    "no action (MANUAL_REVIEW)" tells you a decision was made but not which,
    which is the least useful thing a notification can say.
    """
    if action and not action.startswith("no action"):
        return action
    reasons = [r for r in (ev.get("reasons") or []) if r]
    body = " · ".join(reasons[:2])
    if len(body) > NOTIFY_MAX_CHARS:
        body = body[:NOTIFY_MAX_CHARS - 1].rstrip() + "…"
    return body or action or "no action"


def _notifier():
    """Which backend to post macOS notifications with, or None for off."""
    choice = os.environ.get("PRBOARD_NOTIFIER", "auto").strip().lower()
    if not env_flag("PRBOARD_NOTIFY", default=True) or choice in ("off", "none"):
        return None
    if choice == "osascript":
        return "osascript" if shutil.which("osascript") else None
    if choice in ("terminal-notifier", "terminal_notifier"):
        return "terminal-notifier" if shutil.which("terminal-notifier") else None
    # auto: prefer terminal-notifier, which can open the PR on click.
    if shutil.which("terminal-notifier"):
        return "terminal-notifier"
    return "osascript" if shutil.which("osascript") else None


def _notify_command(backend, title, subtitle, text, url=""):
    """
    Build the argv for one notification.

    Nothing from GitHub is ever interpolated into a script body. PR titles
    contain quotes, backslashes and apostrophes; pasting them into AppleScript
    source would both break and hand a PR author a way to run AppleScript on
    this machine. osascript takes them through `argv` instead, so they stay
    data.
    """
    if backend == "terminal-notifier":
        argv = ["terminal-notifier", "-title", title, "-subtitle", subtitle,
                "-message", text, "-group", f"prboard-{title}"]
        if url:
            argv += ["-open", url]
        return argv
    return [
        "osascript",
        "-e", "on run argv",
        "-e", "display notification (item 1 of argv) "
              "with title (item 2 of argv) subtitle (item 3 of argv)",
        "-e", "end run",
        "--", text, title, subtitle,
    ]


def _notify_mac(title, subtitle, text, url=""):
    """
    Best-effort macOS banner. Never raises, never blocks for long.

    This runs after an approve or merge has already happened, so a notification
    problem must not surface as a failure of the action it is reporting.
    """
    backend = _notifier()
    if backend is None:
        return
    try:
        subprocess.run(_notify_command(backend, title, subtitle, text, url),
                       timeout=NOTIFY_TIMEOUT, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        log(f"mac notification failed ({backend}): {e}")


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
        if ev["verdict"] in pb.actionable_verdicts(CFG):
            ev = pb.apply_pr(gh, CFG, owner, repo, number, me, dry_run=DRY_RUN)
        action = ev.get("action", "no action")
        label = pb.VERDICT_LABEL.get(ev["verdict"], ev["verdict"])
        log(f"{owner}/{repo}#{number} [{label}] -> {action}")
        # Only ping you when something meaningful happened or needs you.
        # "timeout" matters: a --wait that gives up after wait_timeout_seconds
        # drops the PR, and without this you would never learn it had.
        if any(w in action for w in ("APPROVED", "MERGED", "failed", "would",
                                     "timeout")) \
                or ev["verdict"] in ("MANUAL_REVIEW", "CONFLICT", "CHECKS_FAILED",
                                     "CHANGES_REQUESTED", "CHECKS_ADVISORY",
                                     "ERROR"):
            mode = " [DRY-RUN]" if DRY_RUN else ""
            summary = _summary(ev, action)
            _notify(web, f"{owner}/{repo}#{number} [{label}]{mode}\n"
                         f"{summary}\n{ev['url']}")
            # Same condition as the Slack DM, so there is one definition of
            # "worth telling you" rather than two that drift apart.
            _notify_mac(title=f"{owner}/{repo}#{number}",
                        subtitle=f"{label}{mode}",
                        text=summary,
                        url=ev.get("url", ""))
    except Exception as e:
        log(f"{owner}/{repo}#{number} ERROR: {e}")
        _notify_mac(title=f"{owner}/{repo}#{number}", subtitle="ERROR",
                    text=str(e)[:NOTIFY_MAX_CHARS],
                    url=f"https://github.com/{owner}/{repo}/pull/{number}")
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
    # This channel exists for review and merge; discussion happens elsewhere.
    # So a PR link YOU post here is itself the request, and needs no mention —
    # otherwise you could only queue a PR from your laptop, never your phone.
    # Everyone else still has to mention you.
    self_posted = sender == MY_SLACK_ID
    if ALLOWED_SENDERS and not self_posted and sender not in ALLOWED_SENDERS:
        log(f"ignoring message from {sender} (not in PRBOARD_ALLOWED_SENDERS)")
        return
    text = event.get("text", "") or ""
    if not self_posted and MY_SLACK_ID and f"<@{MY_SLACK_ID}>" not in text:
        return
    prs = set()
    for m in pb.GITHUB_PR_URL_RE.finditer(text):
        prs.add((m.group(1), m.group(2), int(m.group(3))))
    for owner, repo, number in prs:
        log(f"trigger: {owner}/{repo}#{number} from Slack message by {sender}"
            + (" (self-posted)" if self_posted else ""))
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
    if DRY_RUN:
        mode = "DRY-RUN"
    else:
        mode = "LIVE, auto-merge " + ("ON" if CFG.get("enable_auto_merge") else "OFF")
        if CFG.get("act_on_advisory_checks"):
            mode += ", acting on PRs with non-required checks RED"
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
