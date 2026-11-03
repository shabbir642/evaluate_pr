#!/usr/bin/env python3
"""
Install (or remove) the prwatch launchd agent.

The plist template carries /Users/USERNAME placeholders in three places. Edit
one wrongly and launchd restarts the agent every ThrottleInterval seconds
forever, which is both noisy and easy to misread as "it is running". This
fills them in from where the repo actually is.

    python3 deploy/install-launchd.py            # check, install, start
    python3 deploy/install-launchd.py --status
    python3 deploy/install-launchd.py --uninstall
    python3 deploy/install-launchd.py --force    # install despite preflight

Stdlib only.
"""
import argparse
import os
import plistlib
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
TEMPLATE = os.path.join(HERE, "com.vmock.prwatch.plist")
LABEL = "com.vmock.prwatch"
DEST = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
LOG_OUT = os.path.expanduser("~/Library/Logs/prwatch.out")
LOG_ERR = os.path.expanduser("~/Library/Logs/prwatch.err")
SECRETS = os.environ.get("PRBOARD_SECRETS",
                         os.path.expanduser("~/.config/prboard/secrets.env"))


def run(argv, **kw):
    return subprocess.run(argv, capture_output=True, text=True, **kw)


def domain():
    return f"gui/{os.getuid()}"


def read_secrets(path):
    """Parse KEY=value lines the way the wrapper's `. secrets.env` would."""
    env = {}
    if not os.path.isfile(path):
        return env
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            env[key.strip()] = val.strip().strip('"').strip("'")
    return env


def preflight(env):
    script = os.path.join(HERE, "preflight.py")
    python = os.path.join(REPO, ".venv", "bin", "python3")
    if not os.path.exists(python):
        python = sys.executable
    merged = dict(os.environ)
    merged.update(env)
    sys.stdout.flush()   # the child writes straight to the terminal
    proc = subprocess.run([python, script], env=merged, text=True)
    return proc.returncode == 0


def build_plist(channel=None):
    with open(TEMPLATE, "rb") as fh:
        data = plistlib.load(fh)
    data["Label"] = LABEL
    data["ProgramArguments"] = ["/bin/bash", os.path.join(HERE, "run-prwatch.sh")]
    env = dict(data.get("EnvironmentVariables") or {})
    env["PRBOARD_DIR"] = REPO
    # launchd gives a minimal PATH; the wrapper widens it, but be explicit.
    env.setdefault("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
    if channel:
        env["PRBOARD_SLACK_CHANNEL"] = channel
    data["EnvironmentVariables"] = env
    data["StandardOutPath"] = LOG_OUT
    data["StandardErrorPath"] = LOG_ERR
    return data


def status():
    proc = run(["launchctl", "print", f"{domain()}/{LABEL}"])
    if proc.returncode != 0:
        print(f"not loaded ({LABEL})")
        return 1
    for line in proc.stdout.splitlines():
        if any(k in line for k in ("state =", "pid =", "last exit code")):
            print("  " + line.strip())
    print(f"\nlogs: {LOG_OUT}\n      {LOG_ERR}")
    return 0


def uninstall():
    run(["launchctl", "bootout", f"{domain()}/{LABEL}"])
    if os.path.exists(DEST):
        os.remove(DEST)
        print(f"removed {DEST}")
    print("agent stopped and uninstalled")
    return 0


def install(force, channel):
    env = read_secrets(SECRETS)
    if not env:
        print(f"warning: no secrets found at {SECRETS}", file=sys.stderr)
    if not force:
        print(f"running preflight with {SECRETS}…\n", flush=True)
        if not preflight(env):
            print("\npreflight failed — not installing.\n"
                  "launchd would restart a broken agent every 30s. Fix the "
                  "problems above, or pass --force.", file=sys.stderr)
            return 1
    dry = env.get("PRBOARD_DRY_RUN", "")
    if dry and dry.strip().lower() not in ("", "0", "false", "no", "off"):
        print("\nnote: PRBOARD_DRY_RUN is on in secrets.env, so the agent will "
              "log what it WOULD do and change nothing.")

    os.makedirs(os.path.dirname(DEST), exist_ok=True)
    with open(DEST, "wb") as fh:
        plistlib.dump(build_plist(channel), fh)
    print(f"\nwrote {DEST}")

    run(["launchctl", "bootout", f"{domain()}/{LABEL}"])   # ignore if absent
    proc = run(["launchctl", "bootstrap", domain(), DEST])
    if proc.returncode != 0:
        print(f"bootstrap failed: {proc.stderr.strip()}", file=sys.stderr)
        return 1
    run(["launchctl", "kickstart", f"{domain()}/{LABEL}"])
    print("agent loaded and started\n")
    return status()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="install even if the preflight fails")
    ap.add_argument("--channel", help="PRBOARD_SLACK_CHANNEL for the agent "
                                      "(defaults to slack_channel in config)")
    args = ap.parse_args()
    if args.uninstall:
        return uninstall()
    if args.status:
        return status()
    return install(args.force, args.channel)


if __name__ == "__main__":
    sys.exit(main())
