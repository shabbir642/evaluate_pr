"""
macOS notification tests.

Two things matter here. Nothing from GitHub may reach a script body — PR
titles are attacker-influenceable text and AppleScript is a scripting
language. And a notification failure must never surface as a failure of the
approve or merge it is reporting, because that has already happened.
"""
import os, sys, types
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

res = []
def chk(name, cond):
    res.append(bool(cond)); print(f"[{'PASS' if cond else 'FAIL'}] {name}")


def load_prwatch(**env):
    for mod in ("slack_sdk", "slack_sdk.socket_mode", "slack_sdk.socket_mode.request",
                "slack_sdk.socket_mode.response", "slack_sdk.web"):
        sys.modules[mod] = types.ModuleType(mod)
    sys.modules["slack_sdk.socket_mode"].SocketModeClient = object
    sys.modules["slack_sdk.socket_mode.request"].SocketModeRequest = object
    sys.modules["slack_sdk.socket_mode.response"].SocketModeResponse = object
    sys.modules["slack_sdk.web"].WebClient = object
    for k in ("PRBOARD_NOTIFY", "PRBOARD_NOTIFIER"):
        os.environ.pop(k, None)
    os.environ.update(env)
    os.environ.setdefault("PRBOARD_SLACK_USER", "U02LS4K29KQ")
    import importlib.util
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location("pw_n", os.path.join(root, "prwatch.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def with_which(pw, *available):
    pw.shutil.which = lambda name: f"/usr/bin/{name}" if name in available else None


# --- backend selection ------------------------------------------------------
pw = load_prwatch()
with_which(pw, "terminal-notifier", "osascript")
chk("auto prefers terminal-notifier", pw._notifier() == "terminal-notifier")
with_which(pw, "osascript")
chk("auto falls back to osascript", pw._notifier() == "osascript")
with_which(pw)
chk("auto gives up when neither exists", pw._notifier() is None)

pw = load_prwatch(PRBOARD_NOTIFIER="osascript")
with_which(pw, "terminal-notifier", "osascript")
chk("explicit osascript wins over terminal-notifier", pw._notifier() == "osascript")

pw = load_prwatch(PRBOARD_NOTIFIER="terminal-notifier")
with_which(pw, "osascript")
chk("explicit terminal-notifier, absent -> None", pw._notifier() is None)

pw = load_prwatch(PRBOARD_NOTIFIER="off")
with_which(pw, "osascript")
chk("PRBOARD_NOTIFIER=off disables", pw._notifier() is None)

pw = load_prwatch(PRBOARD_NOTIFY="0")
with_which(pw, "osascript")
chk("PRBOARD_NOTIFY=0 disables", pw._notifier() is None)

pw = load_prwatch(PRBOARD_NOTIFY="1")
with_which(pw, "osascript")
chk("PRBOARD_NOTIFY=1 enables", pw._notifier() == "osascript")


# --- the injection concern --------------------------------------------------
pw = load_prwatch()
NASTY = '" & do shell script "touch /tmp/pwned" & "'
WEIRD = 'fix: don\'t "quote" me\\n\\\\backslash\\\\ — ünïcode 😀'

for label, hostile in (("applescript injection", NASTY), ("quotes/backslashes", WEIRD)):
    argv = pw._notify_command("osascript", title=hostile, subtitle="x", text=hostile)
    scripts = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
    chk(f"osascript: {label} never enters a script body",
        not any(hostile in s for s in scripts))
    chk(f"osascript: {label} survives verbatim in argv", argv.count(hostile) == 2)
    chk(f"osascript: {label} sits after the -- separator",
        "--" in argv and argv.index("--") < argv.index(hostile))

argv = pw._notify_command("osascript", "t", "s", "msg")
chk("osascript script body references argv, not literals",
    all(("item 1 of argv" in s or "run" in s) for s in
        [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]))
chk("osascript passes exactly text, title, subtitle after --",
    argv[argv.index("--") + 1:] == ["msg", "t", "s"])


# --- terminal-notifier argv -------------------------------------------------
argv = pw._notify_command("terminal-notifier", "o/r#1", "READY", "MERGED",
                          "https://github.com/o/r/pull/1")
chk("terminal-notifier passes fields as separate args",
    argv[argv.index("-title") + 1] == "o/r#1"
    and argv[argv.index("-message") + 1] == "MERGED")
chk("terminal-notifier makes it clickable",
    argv[argv.index("-open") + 1] == "https://github.com/o/r/pull/1")
chk("terminal-notifier groups per PR", "-group" in argv)

argv = pw._notify_command("terminal-notifier", "o/r#1", "READY", "MERGED", "")
chk("no url -> no -open flag", "-open" not in argv)

argv = pw._notify_command("terminal-notifier", NASTY, "s", NASTY, "")
chk("terminal-notifier keeps hostile text as data", argv.count(NASTY) == 2)


# --- failure must never propagate -------------------------------------------
pw = load_prwatch()
with_which(pw, "osascript")
logged = []
pw.log = lambda m: logged.append(m)

for label, boom in (
        ("binary missing", FileNotFoundError("no such file")),
        ("timed out", __import__("subprocess").TimeoutExpired("osascript", 10)),
        ("permission denied", PermissionError("denied")),
        ("anything else", RuntimeError("kaboom"))):
    logged.clear()
    pw.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(boom)
    try:
        pw._notify_mac("t", "s", "m")
        raised = False
    except Exception:
        raised = True
    chk(f"{label}: does not raise", not raised)
    chk(f"{label}: is logged", any("notification failed" in m for m in logged))

# disabled means no subprocess at all, not a silent failure
pw = load_prwatch(PRBOARD_NOTIFY="0")
with_which(pw, "osascript")
called = []
pw.subprocess.run = lambda *a, **k: called.append(a)
pw._notify_mac("t", "s", "m")
chk("disabled -> subprocess never invoked", called == [])

# enabled means it really is invoked, with a timeout
pw = load_prwatch()
with_which(pw, "osascript")
seen = {}
pw.subprocess.run = lambda *a, **k: seen.update(argv=a[0], kw=k)
pw._notify_mac("o/r#1", "READY", "MERGED", "https://x")
chk("enabled -> subprocess invoked", seen.get("argv", [None])[0] == "osascript")
chk("invoked with a timeout", seen.get("kw", {}).get("timeout") == pw.NOTIFY_TIMEOUT)
chk("invoked without check=True", seen.get("kw", {}).get("check") is False)

ok = sum(res)
print(f"\n{ok}/{len(res)} passed")
sys.exit(0 if ok == len(res) else 1)
