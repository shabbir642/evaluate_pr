#!/bin/bash
# Wrapper that loads secrets and launches prwatch. Used by launchd.
# Keep your tokens in ~/.config/prboard/secrets.env (chmod 600), NOT in here.

set -euo pipefail

# Where prwatch.py lives — EDIT THIS to your install path.
PRBOARD_DIR="${PRBOARD_DIR:-$HOME/tools/prboard}"

# launchd starts us with a minimal PATH that usually has only /usr/bin, so a
# homebrew or pyenv python3 would be invisible. Put the usual places back.
PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
export PATH

# Load secrets (GITHUB_TOKEN, SLACK_APP_TOKEN, SLACK_BOT_TOKEN, PRBOARD_SLACK_USER,
# optional PRBOARD_DRY_RUN, PRBOARD_ALLOWED_SENDERS). See secrets.env.example.
SECRETS="${PRBOARD_SECRETS:-$HOME/.config/prboard/secrets.env}"
if [ -f "$SECRETS" ]; then
  set -a; . "$SECRETS"; set +a
fi

# If GITHUB_TOKEN wasn't set in secrets, try the gh CLI.
if [ -z "${GITHUB_TOKEN:-}" ] && command -v gh >/dev/null 2>&1; then
  export GITHUB_TOKEN="$(gh auth token 2>/dev/null || true)"
fi

# prwatch needs slack_sdk, and `pip install slack_sdk` puts it in whichever
# python3 is on PATH — not necessarily /usr/bin/python3. Hardcoding the system
# python here is how you get a 30-second launchd restart loop. Pick the first
# interpreter that can actually import it.
pick_python() {
  if [ -n "${PRBOARD_PYTHON:-}" ]; then
    echo "$PRBOARD_PYTHON"; return 0
  fi
  if [ -x "$PRBOARD_DIR/.venv/bin/python3" ]; then
    echo "$PRBOARD_DIR/.venv/bin/python3"; return 0
  fi
  local candidate
  for candidate in $(type -aP python3 2>/dev/null) /usr/bin/python3; do
    if "$candidate" -c 'import slack_sdk' >/dev/null 2>&1; then
      echo "$candidate"; return 0
    fi
  done
  return 1
}

if ! PY="$(pick_python)"; then
  echo "prwatch: no python3 on PATH can 'import slack_sdk'." >&2
  echo "  Fix with one of:" >&2
  echo "    python3 -m venv '$PRBOARD_DIR/.venv' && '$PRBOARD_DIR/.venv/bin/pip' install slack_sdk" >&2
  echo "    pip install slack_sdk   (then make sure that python3 is on PATH)" >&2
  echo "    export PRBOARD_PYTHON=/full/path/to/python3" >&2
  exit 1
fi

echo "prwatch: using $PY"
exec "$PY" "$PRBOARD_DIR/prwatch.py"
