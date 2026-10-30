#!/bin/bash
# Wrapper that loads secrets and launches prwatch. Used by launchd.
# Keep your tokens in ~/.config/prboard/secrets.env (chmod 600), NOT in here.

set -euo pipefail

# Where prwatch.py lives — EDIT THIS to your install path.
PRBOARD_DIR="${PRBOARD_DIR:-$HOME/tools/prboard}"

# Load secrets (GITHUB_TOKEN, SLACK_APP_TOKEN, SLACK_BOT_TOKEN, PRBOARD_SLACK_USER,
# optional PRBOARD_DRY_RUN). See secrets.env.example.
SECRETS="${PRBOARD_SECRETS:-$HOME/.config/prboard/secrets.env}"
if [ -f "$SECRETS" ]; then
  set -a; . "$SECRETS"; set +a
fi

# If GITHUB_TOKEN wasn't set in secrets, try the gh CLI.
if [ -z "${GITHUB_TOKEN:-}" ] && command -v gh >/dev/null 2>&1; then
  export GITHUB_TOKEN="$(gh auth token 2>/dev/null || true)"
fi

exec /usr/bin/python3 "$PRBOARD_DIR/prwatch.py"
