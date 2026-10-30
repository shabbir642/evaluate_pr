#!/usr/bin/env python3
"""
Check the prwatch setup before running it for real.

A misconfigured Socket Mode app does not error — it connects, authenticates,
logs "prwatch connecting" and then stays silent forever. This checks each link
in the chain separately so a failure names itself.

Run it with the same environment prwatch gets:

    set -a; . ~/.config/prboard/secrets.env; set +a
    .venv/bin/python3 deploy/preflight.py

Stdlib only, and it never prints a token.
"""
import importlib.util
import json
import os
import sys
import urllib.parse

_here = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "prboard", os.path.join(os.path.dirname(_here), "prboard.py"))
pb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pb)

OK, BAD, WARN = "  ok  ", " FAIL ", " warn "
problems = []


def say(mark, text, detail=""):
    print(f"[{mark}] {text}" + (f"\n         {detail}" if detail else ""))
    if mark is BAD:
        problems.append(text)


def slack(method, token, post=False, **params):
    url = f"https://slack.com/api/{method}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return pb._request(url, {"Authorization": f"Bearer {token}",
                             "User-Agent": "prboard-preflight/1.0"},
                       method="POST" if post else "GET",
                       data={} if post else None)


def header(headers, name):
    """Slack sends header names lowercased, and dict() keeps that casing."""
    name = name.lower()
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return ""


def main():
    cfg = pb.load_config(os.environ.get("PRBOARD_CONFIG"))
    channel = cfg.get("slack_channel", "")
    app_token = os.environ.get("SLACK_APP_TOKEN", "")
    bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
    me_slack = os.environ.get("PRBOARD_SLACK_USER", "")

    print("\nprwatch preflight\n" + "-" * 60)

    # --- 1. environment ---------------------------------------------------
    for name, val, prefix in (("SLACK_APP_TOKEN", app_token, "xapp-"),
                              ("SLACK_BOT_TOKEN", bot_token, "xoxb-")):
        if not val:
            say(BAD, f"{name} is not set")
        elif not val.startswith(prefix):
            say(BAD, f"{name} does not start with {prefix!r}",
                "app-level and bot tokens are easy to swap — check which is which")
        else:
            say(OK, f"{name} present ({prefix}…, {len(val)} chars)")

    if not os.environ.get("GITHUB_TOKEN"):
        say(BAD, "GITHUB_TOKEN is not set")
    else:
        say(OK, "GITHUB_TOKEN present")

    if not me_slack.startswith("U"):
        say(BAD, f"PRBOARD_SLACK_USER looks wrong: {me_slack!r}",
            "it should be a user id like U02LS4K29KQ, not a @name")
    else:
        say(OK, f"PRBOARD_SLACK_USER = {me_slack}")

    if not channel:
        say(BAD, "no slack_channel in config")
    else:
        say(OK, f"watching channel {channel}")

    # --- 2. bot token: who am I, and with what scopes ---------------------
    scopes = set()
    bot_user = ""
    if bot_token.startswith("xoxb-"):
        try:
            data, headers = slack("auth.test", bot_token)
            if not data.get("ok"):
                say(BAD, f"auth.test failed: {data.get('error')}")
            else:
                bot_user = data.get("user_id", "")
                say(OK, f"bot authenticates as @{data.get('user')} ({bot_user}) "
                        f"in {data.get('team')}")
                scopes = {s.strip() for s in
                          header(headers, "x-oauth-scopes").split(",") if s.strip()}
                if not scopes:
                    say(WARN, "Slack did not report the granted scopes",
                        "skipping the scope checks below")
        except pb.ApiError as e:
            say(BAD, f"could not reach Slack with the bot token: {e}")

    if scopes:
        for need, why in (("groups:history", "receive messages in a PRIVATE channel"),
                          ("chat:write", "DM you the result")):
            if need in scopes:
                say(OK, f"scope {need}")
            elif need == "groups:history" and "channels:history" in scopes:
                say(BAD, "scope groups:history missing (channels:history is present)",
                    "channels:* is for PUBLIC channels; a private one needs groups:*")
            else:
                say(BAD, f"scope {need} missing — needed to {why}")
        if "im:write" not in scopes:
            say(WARN, "scope im:write missing",
                "DMs may fail; prwatch logs 'notify failed' and carries on")

    # --- 3. can the bot actually see the channel? ------------------------
    if bot_token.startswith("xoxb-") and channel:
        try:
            data, _ = slack("conversations.info", bot_token, channel=channel)
            if not data.get("ok"):
                err = data.get("error")
                if err == "missing_scope":
                    say(WARN, "cannot verify the channel — needs groups:read",
                        "prwatch itself does NOT need groups:read; add it only if "
                        "you want this check to run. Verify by hand instead: the "
                        "bot must be invited to the channel.")
                elif err in ("channel_not_found", "not_in_channel"):
                    say(BAD, f"the bot cannot see {channel}: {err}",
                        "run /invite @evaluate_pr in the channel")
                else:
                    say(BAD, f"conversations.info failed: {err}")
            else:
                ch = data.get("channel", {})
                kind = "private" if ch.get("is_private") else "public"
                say(OK, f"channel #{ch.get('name')} is {kind}")
                if ch.get("is_private") and "groups:history" not in scopes:
                    say(BAD, "private channel but no groups:history",
                        "subscribe to message.groups, not message.channels")
                if ch.get("is_member") is False:
                    say(BAD, "the bot is NOT a member of the channel",
                        f"run /invite @yourapp in #{ch.get('name')}")
                elif ch.get("is_member"):
                    say(OK, "the bot is a member of the channel")
        except pb.ApiError as e:
            say(BAD, f"channel check failed: {e}")

    # --- 4. app token: does Socket Mode work? ----------------------------
    if app_token.startswith("xapp-"):
        try:
            # Documented POST-only; a GET can come back as insecure_request.
            data, _ = slack("apps.connections.open", app_token, post=True)
            if data.get("ok"):
                say(OK, "Socket Mode connects (connections:write is granted)")
            else:
                err = data.get("error", "")
                hint = {
                    "missing_scope": "the app-level token needs connections:write",
                    "invalid_auth": "that is not a valid app-level token — it is the "
                                    "xapp- one from Socket Mode, not the xoxb- bot token",
                    "insecure_request": "this call must be a POST — a preflight bug "
                                        "if you still see it",
                    "not_allowed_token_type": "this needs the xapp- app-level token, "
                                              "not a bot or user token",
                }.get(err, "")
                say(BAD, f"apps.connections.open failed: {err}", hint)
        except pb.ApiError as e:
            say(BAD, f"Socket Mode check failed: {e}")

    # --- 5. GitHub --------------------------------------------------------
    if os.environ.get("GITHUB_TOKEN"):
        try:
            gh = pb.GitHub(os.environ["GITHUB_TOKEN"],
                           os.environ.get("GITHUB_API_URL", "https://api.github.com"))
            say(OK, f"GitHub authenticates as @{gh.login()}")
        except (pb.ApiError, SystemExit) as e:
            say(BAD, f"GitHub auth failed: {e}")

    # --- 6. what it will DO once triggered -------------------------------
    print("-" * 60)
    # Same semantics as prwatch's env_flag: unset means off, and "0"/"false"/
    # "off" mean off too rather than being truthy strings.
    dry = os.environ.get("PRBOARD_DRY_RUN")
    dry_on = bool(dry) and dry.strip().lower() not in ("", "0", "false", "no", "off")
    if dry_on:
        print("MODE: DRY-RUN — it will log what it would do and change nothing.")
    else:
        bits = ["approve PRs that pass every gate"]
        if cfg.get("enable_auto_merge"):
            bits.append(f"MERGE them ({cfg.get('merge_method', 'squash')})")
        if cfg.get("act_on_advisory_checks"):
            bits.append("act on PRs with a non-required check RED")
        print("MODE: LIVE — it will " + ", ".join(bits) + ".")
    senders = os.environ.get("PRBOARD_ALLOWED_SENDERS", "").strip()
    print(f"TRIGGER: a message in {channel} mentioning <@{me_slack}> with a PR link,")
    print(f"         from {senders if senders else 'any human in the channel'}.")

    print("-" * 60)
    if problems:
        print(f"{len(problems)} problem(s) to fix before running prwatch.\n")
        return 1
    print("All checks passed — prwatch is ready to run.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
