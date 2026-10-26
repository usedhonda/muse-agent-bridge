#!/bin/sh
# Run bridge.worker with the bot credential injected as a vault surrogate.
# The egress layer swaps the hsurr:* value for the real bot token on
# authorized requests to slack.com. Real credentials are not exposed to the
# agent/runtime cell; they are held in a separate security domain in the VM.
set -eu
cd "$(dirname "$0")/.."
SURROGATE=$(.venv/bin/python -c "
import sys
sys.path.insert(0, '/opt/muse/skills/custom-connector-setup/bin')
from dynamic_credentials import dynamic_credential_entry
print(dynamic_credential_entry('custom.slack-muse-bot')['surrogate'])
")
SLACK_BOT_TOKEN="$SURROGATE" exec .venv/bin/python -m bridge.worker "$@"
