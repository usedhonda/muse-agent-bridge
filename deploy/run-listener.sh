#!/bin/sh
# Run bridge.listener (Socket Mode) with both credentials injected as
# vault surrogates. The egress layer swaps each hsurr:* value for the
# real token on requests to slack.com; raw tokens never exist on this VM.
#
# Credentials:
#   SLACK_BOT_TOKEN <- secure connection "custom.slack-muse-bot"
#   SLACK_APP_TOKEN <- secure connection "custom.slack-muse-app"
# Exit code 3 means the app-level credential is not registered yet; the
# keeper treats that as "waiting", not as a crash.
set -eu
cd "$(dirname "$0")/.."
SURROGATES=$(.venv/bin/python - << 'PYEOF'
import sys
sys.path.insert(0, "/opt/muse/skills/custom-connector-setup/bin")
from dynamic_credentials import dynamic_credential_entry

try:
    bot = dynamic_credential_entry("custom.slack-muse-bot")["surrogate"]
except Exception:
    bot = ""
try:
    app = dynamic_credential_entry("custom.slack-muse-app")["surrogate"]
except Exception:
    app = ""
print(bot)
print(app)
PYEOF
)
BOT=$(printf '%s\n' "$SURROGATES" | sed -n '1p')
APP=$(printf '%s\n' "$SURROGATES" | sed -n '2p')
if [ -z "$BOT" ]; then
  echo "run-listener: bot credential (custom.slack-muse-bot) unavailable" >&2
  exit 3
fi
if [ -z "$APP" ]; then
  echo "run-listener: app credential (custom.slack-muse-app) not registered yet" >&2
  exit 3
fi
SLACK_BOT_TOKEN="$BOT" SLACK_APP_TOKEN="$APP" \
  exec .venv/bin/python -m bridge.listener \
  --config .local/config.yaml --state .local/state
