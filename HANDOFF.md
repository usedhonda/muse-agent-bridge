# Handoff notes (2026-10-24)

## Current state

Muse itself is already running. This repository adds its Slack bridge; do
not confuse pending Slack integration with Muse runtime availability.
The selected deployment is an existing shared workspace with one authorized
owner. App existence may be visible to members; conversations and replies
must remain owner-only. Match the existing private secretary app's settings.
Actual app/workspace/channel/user IDs and deployment receipts live only in
local ignored state, never in this file.

## Implemented and offline-verified

- M1 listener: audience gate, fsync'd queue, restart dedupe, bot-loop guard.
  Group DMs are rejected, including messages from the owner.
- M2 worker interface (`bridge/worker.py`, `docs/worker.md`): `next` claims
  one owner event for the running Muse runtime; `reply` posts text to the
  original channel/root thread. Current allowlists are rechecked at send.
- Durable SQLite claims and delivery states prevent automatic duplicate
  turns/posts. One active turn per thread; completed history stays scoped
  to that thread. `pending`, `resume`, and explicit `reconcile` support
  recovery. Uncertain network delivery stops instead of auto-resending.
- Queue-based dedupe recovery covers a crash before the secondary seen-ID
  file is flushed. Partial queue tails are preserved and block appends.
- Matching Slack profile: Agent View, writable DM tab, Socket Mode, only
  DM/private-channel bot events, read-only user observation scopes,
  bot reply scopes, no distribution activation. No `reactions:write`;
  example config sets `ackReaction: null`. User observation and advanced
  Agent status/title interactions are not implemented by the bridge.
- Offline checks: the original 10 listener/config checks plus 14 worker,
  Web API, queue recovery and access-control checks passed. No live
  Slack/Muse reply has been verified from this checkout.

## Remaining acceptance and deployment

1. Finish the selected Slack app's OAuth installation and dedicated
   `connections:write` app-level token creation. Check local deployment
   state for actual progress; never recreate an existing app blindly.
2. Register bot/app credentials through the approved secure flow. Do not
   store tokens in chat, tracked files, plaintext env files, logs or a unit.
   The service template deliberately requires secure environment injection.
3. Set local config to the single owner and only the approved owner-only
   private channel. Confirm membership before allowing channel replies.
4. Deploy on the running Muse agent VM and register its existing runtime's
   five-second wake hook using `docs/worker.md`. The runtime integration
   path is not yet established; the CLI is not an installed wake hook.
5. Live proof: owner DM and private-channel message -> threaded Muse reply;
   non-owner and group-DM event -> no queue record, reaction or reply.
6. Keep guest audiences closed until M4 governance/leak tests are complete.

## Conventions

- Local/machine-specific state and credentials never enter this public repo.
- Human-facing config is YAML. `config.example.json` is a legacy artifact;
  use the YAML example for the current profile.
- Offline implementation, Slack app registration, installation, running
  services, and end-to-end acceptance are distinct states. Report each
  accurately. Public pushes require the owner's explicit publication GO.
