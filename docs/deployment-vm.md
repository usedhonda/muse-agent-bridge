# Deployment on Muse's agent VM (owner-DM profile)

This is a second, standby deployment prepared on Muse's agent VM so the
bridge does not depend on the owner's Mac being awake. The pre-existing
Mac-runtime deployment is currently the live path for the owner's Slack
DMs; this VM deployment takes over once its bot credential registration
(see "Credential custody") is complete and its own round trip is verified.

## Shape

- Checkout at `~/muse-slack-bridge` (this repository, `main`), virtualenv
  in `.venv`. Local state in `.local/` (gitignored): `config.yaml`,
  `state/` (queue, worker SQLite, poller watermark).
- Config profile: one `owner` audience, DM only, `ackReaction: null` —
  matching the existing Slack app (bot events `message.im` /
  `message.groups`, DM-oriented scopes, no distribution).

## Credential custody

No raw Slack token exists on this VM. The bot token is registered once in
Muse's secure connection as `custom.slack-muse-bot` (API key, bearer
header, host `slack.com`). Local processes ask authd for an `hsurr:*`
surrogate and put it in the `Authorization` header; the egress layer swaps
it for the real token on requests to slack.com.

- Intake (`bridge/poller.py`) applies the surrogate per request.
- Replies run through `deploy/run-worker.sh`, which fetches a fresh
  surrogate and exports it as `SLACK_BOT_TOKEN` for `bridge.worker`.

## Intake choice

`bridge/poller.py` polls the owner's DM channel every 20 seconds and
appends new owner messages to the same durable queue the Socket Mode
listener writes, in the same record format, so `bridge.worker` claims
them identically. The Socket Mode listener remains the lower-latency
upgrade path for a deployment where the app token can be held directly;
on this VM custody wins. Polling a single DM channel stays far inside
Slack's rate tiers.

## Supervision

- `deploy/muse-slack-bridge-poller.service` (systemd user unit,
  `Restart=always`) runs the poller.
- A runtime wake hook polls `bridge.worker next` and starts a Muse turn
  for each claimed event; replies go out through `deploy/run-worker.sh`.

## Upgrade rule

Preserve `.local/` across upgrades (config, queue, worker DB, poller
watermark). Never reset the worker database to "fix" a stuck job; use
`bridge.worker pending` / `resume` / `reconcile` as documented in
`docs/worker.md`.
