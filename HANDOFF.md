# Handoff notes (2026-10-24)

## VM deployment checkpoint (2026-10-24, Muse)

- A second deployment of this bridge was prepared on Muse's agent VM:
  owner-only, DM-first profile in `.local/config.yaml` (gitignored),
  virtualenv in `.venv`, all state under `.local/state/`.
- New intake: `bridge/poller.py` polls the owner's DM with the bot
  credential and appends to the same durable queue as the Socket Mode
  listener, in the same record format, so `bridge.worker` claims events
  identically. It exists for custody: on this VM the bot token is held in
  Muse's secure connection and local processes use short-lived
  surrogates, so no raw token is stored on the VM. Replies run through
  `deploy/run-worker.sh` (surrogate injected per call). Supervision:
  `deploy/muse-slack-bridge-poller.service` plus a wake hook that claims
  queued events for a Muse turn. See `docs/deployment-vm.md`.
- Offline suite is now 56 tests (3 new poller tests). This VM deployment
  is prepared and supervised but on standby: its bot credential
  registration is still outstanding, and a pre-existing agent deployment
  on the owner's Mac runtime is the live path that currently serves the
  owner's Slack DMs. Do not treat the VM deployment as live until its own
  round trip is verified.
- `config.example.json` remains a legacy artifact flagged for removal
  (snapshot pushes cannot delete files; remove it with a full Git client).

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
- Offline regression suite: 53 tests passed, including injected membership
  and storage failures, duplicate delivery, ACK/reaction failures, and nested
  list credential-key checks. These changes have not been deployed or verified
  against live Slack/Muse.
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


## Checkpoint 2026-10-24 (evening): resident Socket Mode listener

- The bridge is live on the VM via wake-hook intake (history poller).
  This deployment posts replies at channel top level
  (`replyInThreads: false` locally; the code default remains threaded).
- Resident listener: `bridge/listener.py` + `bridge/transport.py`
  implement Socket Mode; the transport now honors `HTTPS_PROXY`
  (runtime egress) for the WSS connection. `deploy/run-listener.sh`
  injects both vault surrogates (bot: custom.slack-muse-bot; app:
  custom.slack-muse-app) and exits 3 until the app credential exists.
- The wake hook doubles as the listener keeper: it (re)launches the
  listener in-session when the app credential is registered, and runs
  the poller only while the listener is down. EventStore dedupes
  (channel, ts) across transports (records name their transport:
  socket/poll), so handover never double-queues.
- State: code complete, 61 offline tests pass. The listener goes live
  when the owner registers the app-level token (xapp) as the
  custom.slack-muse-app secure connection.
