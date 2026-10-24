# Handoff notes (2026-10-24)

## Current state

Muse itself is already running. This repository adds its Slack bridge; do
not confuse pending Slack integration with Muse runtime availability.
The selected deployment is an existing shared workspace with one authorized
owner. App existence may be visible to members; only the owner and explicitly named agents in approved private rooms may converse. Match the existing private secretary app's settings.
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
  Web API, queue recovery and access-control checks passed. An earlier deployed owner/private-room round trip was verified; new routing
  changes remain offline-only.

## Current development checkpoint

- Intake durability: accepted events are protocol-ACKed only after authorized
  queue persistence. Membership API failures remain unacknowledged rather than
  being treated as verified rejection; optional reactions happen afterward.
  This preserves the no-unauthorized-body-retention boundary. It does not prove
  the three-second ACK deadline or guaranteed Slack replay; see `docs/design.md`.
- Configuration credential-key validation now traverses lists as well as
  mappings. Error messages identify the key path, never its value.
- Offline regression suite: 58 tests passed, including injected membership
  and storage failures, duplicate delivery, ACK/reaction failures, and nested
  list credential-key checks. These changes have not been deployed or verified
  against live Slack/Muse.
- The in-memory duplicate set is updated after primary queue fsync, before
  secondary seen-ID persistence. A failed secondary write still raises, but
  same-process redelivery no longer appends the already durable event again.
  Failed primary writes/fsync do not mark an event seen. This is not a guarantee
  of exactly-once delivery or recovery of durability-uncertain primary writes.
- Explicit `agentChannels` room-to-agent allowlists permit natural agent
  conversation without admitting agents into DMs or other rooms. Exact
  private/unshared membership is checked before intake and before replies.
- Replies stay in the room for top-level messages and in existing threads
  for threaded messages. Threads created while a job waits are checked before
  sending. `skip` completes a no-reply turn without starting an acknowledgement loop.
- Room history identifies speakers and stays separate from threads and DMs.
  Agent messages do not create owner authority; substantive authorized work
  is not subject to a blanket agent-turn quota.
- Deployment state belongs in ignored local records. Never enable an app,
  install a hook, or change a runtime solely because this code is available.
  The Slack Agent experience switch controls presentation, not bot transport
  or credential revocation. Preserve local runtime changes during upgrades.
- The bridge uses bot credentials only. It does not provide posting as the
  human owner, unsolicited outbound actions, or arbitrary destinations.

## Remaining acceptance and deployment

1. Reconcile existing runtime modifications before an upgrade; preserve
   pending/uncertain delivery records and secure credential injection.
2. Configure the owner and approved private-room agent IDs locally. Do not
   commit deployment IDs or sample data copied from actual conversations.
3. Install the listener and five-second hook only for an authorized deployment.
4. Verify natural agent conversation, direct room replies, existing-thread
   replies, membership changes, and bot identity on that runtime. Offline
   tests do not establish live acceptance for this checkpoint.
5. Keep guest audiences closed until M4 governance/leak tests are complete.

## Conventions

- Local/machine-specific state and credentials never enter this public repo.
- Human-facing config is YAML. `config.example.json` is a legacy artifact;
  use the YAML example for the current profile.
- Offline implementation, Slack app registration, installation, running
  services, and end-to-end acceptance are distinct states. Report each
  accurately. Public pushes require the owner's explicit publication GO.

