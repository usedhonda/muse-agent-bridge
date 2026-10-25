# VM deployment profiles

This is an operator guide, not an inventory of live deployments. Choose the
intake, credential provider, paths and supervisor for the target host, then
verify its own round trip. The repository does not establish that any host is
live, standby, or ready to take over.

Start with the [README quick start](../README.md#quick-start). The VM needs an existing agent
runtime and a registered wake hook; supervising intake alone does not run a
model or produce responses.

## Choose a profile

| Profile | Credentials and dependencies | Intended intake |
| --- | --- | --- |
| Socket Mode | Secure injection of `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN`; app token has `connections:write` | Listener audience gate, DMs and configured private rooms |
| Polling | Muse VM `dynamic_credentials` helper and surrogate-aware egress, with the bot credential registered | First configured owner's DM; start with a DM-only allowlist |

For Socket Mode, launch `.venv/bin/python -m bridge.listener --config
.local/config.yaml --state .local/state` with approved credential injection.
`deploy/run-listener.sh` demonstrates the VM-specific vault helper. Polling uses
`.venv/bin/python -m bridge.poller --config .local/config.yaml --state .local/state
--interval 20`; it cannot run on a generic VM with only the pip dependencies.
The poller excludes configured `agentChannels` from history reads and queue
writes. Their exact-membership check and pinned routing metadata are implemented
only by Socket Mode. Polling cannot provide an agent-room outage fallback.

Keep `ackReaction: null` with the supplied manifest. Inject the bot credential
into worker reply commands too; `deploy/run-worker.sh` is the corresponding
VM-specific example. Never put credentials in config, units, logs or commits.
Runtime-specific connection names and helper paths in the launchers must be
provisioned or adapted locally; these files do not register credentials for you.
Custom Connector support does not by itself establish credential eligibility
for an independent service; consult the [verified Muse boundaries and unknowns](runtime-lifecycle.md#muse-public-architecture-and-integration-unknowns).

## Supervision and paths

Keep the checkout, virtual environment, `.local/config.yaml`, and `.local/state/`
consistent across intake and worker invocations. Use the virtualenv interpreter
that has `requirements.txt` installed.

The units under `deploy/` are templates to inspect and adapt, not ready-made
portable installations. The Socket Mode file is a systemd user unit; the polling
file uses a system service layout and fixed example paths. Set the target paths,
interpreter, credential launcher and service ownership locally before enabling
the selected unit. The Socket Mode template invokes Python directly, so adapt
it to use the approved launcher or supply its secure environment explicitly.
Confirm service logs without printing credentials or conversation bodies.

The external runtime must schedule `bridge.worker next` no faster than every
five seconds, carry the event ID through its turn, and call `reply` or `skip`.
Use [the worker contract](worker.md) for packet handling and runtime recovery.

Keep short checks and long-lived intake/model execution under separate supported
lifecycle owners. The shell launchers use `exec` and do not detach from a check
or install a keeper. If the user bus is unavailable, establish the host's approved
lifecycle mechanism before claiming unattended recovery; see the
[runtime lifecycle contract](runtime-lifecycle.md) for existing support, manual
recovery limits and required deployment evidence.

## Upgrade and transport cutover

1. Pause intake and new wake-hook claims. Inspect `bridge.worker pending` and
   finish or deliberately recover in-flight turns before changing the runtime.
2. Preserve local edits and back up `.local/`, including config, queue, worker
   SQLite database and associated SQLite sidecar files, dedupe data, and
   `poller.json` watermarks. Use a consistent snapshot with processes stopped.
   Protect backups as private conversation state; keep them outside commits.
3. Update the checkout and dependencies, reconcile local patches, and keep the
   same local state paths. Check changed configuration and launcher requirements.
4. Restart the chosen intake and wake hook, then verify a small authorized live
   round trip and the unauthorized-sender rejection check.

Run one intake profile per conversation. During a handover, tagged socket/poll
records for the same `(channel, ts)` are deduplicated under an append lock when
both processes run this version on the same host/local state directory. Stop
older unpatched writers before upgrading; separate hosts or state directories
are not coordinated.
Before switching, establish the last accepted message and inspect the target
transport's backlog/watermarks. A fresh polling watermark can ingest history.
Plan the cutover deliberately; do not delete state to force a clean start.

## Preservation and recovery

An empty `next` result is not proof of a healthy runtime. Check intake and hook
supervision, credentials, local config and pending jobs independently. A receipt
ACK/reaction is intake evidence; a reply is delivery evidence; external task
completion requires the runtime's own result verification.

Use `.venv/bin/python -m bridge.worker status` for local metadata counts,
incomplete queue-tail detection and recovery hints, without message bodies or
Slack requests. It may initialize the local worker database if one is missing. `stale_claimed` is an age diagnostic, not permission to replay a
turn; claim age does not automatically release it. Inspect unfinished job metadata
with `.venv/bin/python -m bridge.worker pending`. Verify the old runtime has stopped
before deliberately continuing a known claim with
`.venv/bin/python -m bridge.worker resume EVENT_ID`; do not automatically repeat
model turns or external actions. A job stuck in `sending` requires checking Slack for
the attempted post before using `reconcile --sent-ts MESSAGE_TS` or
`reconcile --confirmed-not-sent`. The latter permits a deliberate reply retry;
it does not send by itself. See [recovery details](worker.md#recovery).

Never reset the worker database, truncate queue evidence, remove dedupe data, or
clear polling watermarks to fix a stuck job. Preserve incomplete queue tails for
operator recovery. Restoring an older snapshot can replay intake or lose evidence
of a delivered reply; compare actual Slack delivery and runtime actions before
resuming claims. Resolve uncertain delivery before restarting normal processing.
