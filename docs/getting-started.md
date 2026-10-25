# Getting started and diagnosing a silent bot

This bridge connects an **existing Muse runtime** to Slack. It does not
create a Muse account, start a model, or provide a portable Muse runtime API.
The commands below describe the Muse VM profile, not evidence that a deployment
is live. For a generic host with securely injected tokens, use the
[README quick start](../README.md#quick-start).

## 1. Check the runtime prerequisites

- Python 3 with virtualenv/pip support and outbound HTTPS/WSS connectivity.
- A running Muse environment that can register a wake hook and execute local
  commands. The listener alone cannot generate or deliver an agent reply.
- A secure credential injector. The supplied `deploy/run-*.sh` launchers
  depend on Muse's `/opt/muse/skills/custom-connector-setup/bin` integration;
  they are not generic launchers for every Linux host.
- A persistent location for `.local/`. Confirm what survives a VM replacement.
  A successful restart test does not prove VM-replacement recovery.

## 2. Create and install the Slack app

1. Create an app using `slack-app-manifest.yaml` in the intended workspace.
2. Check `docs/slack-recommended-settings.yaml` for settings outside the manifest.
3. Install the app and register its **bot credential** in the runtime's secure
   connection as `custom.slack-muse-bot`.
4. Create an **app-level credential** with `connections:write` and register it
   as `custom.slack-muse-app`. This is different from the bot credential.
5. Invite the bot to the approved private room if using room conversations.

Do not paste credential values into Slack, configuration examples, source,
service units, or diagnostic output. The bridge posts with its bot identity,
not as the owner. Granted user observation scopes do not mean the bridge has
implemented those observation features.

## 3. Prepare a checkout and local configuration

On a new checkout only:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
mkdir -p .local
cp config.example.yaml .local/config.yaml
```

For an upgrade, preserve the existing config and state instead of copying
over them. YAML is the current example; the legacy JSON example is not the
recommended deployment profile.

Fill in the owner user ID and allowed private-channel IDs locally. Keep
`ackReaction: null` unless the app is deliberately granted `reactions:write`.
For agent rooms, add `agentChannels` under the owner audience and list each
permitted agent's user ID. The room must contain exactly the owner, the listed
agents, and this bot, and must be private and unshared. Agents do not gain DM
access or the power to authorize new owner actions.

`hookPollIntervalSec` is an integration hint: the config loader does not
schedule the runtime hook. Register and verify that hook separately.

## 4. Start intake, then connect the worker

On the supported Muse VM, the secure launcher starts Socket Mode:

```sh
sh deploy/run-listener.sh
```

Exit code 3 means a required credential is unavailable to this invocation.
The launcher collapses all helper exceptions to empty results, so its app-message
wording "not registered yet" does not establish that cause. See
[credential diagnostic limits](runtime-lifecycle.md#existing-launcher-diagnostic-ambiguity).
It does not mean the
listener is connected. Check credential registration without printing values.
If using supervision, explicitly configure its secure injector and interpreter.
The supplied listener systemd template invokes `/usr/bin/python3`, not `.venv`;
do not assume dependencies installed into `.venv` are visible to that service.

Check that the target runtime actually supports a systemd user manager before
choosing that template. Some hosted agent VMs do not provide a user bus. A
background process started manually is not persistent supervision. On such a
runtime, the operator must configure its supported lifecycle/wake-hook mechanism
to invoke the secure launcher, avoid starting a second listener when one is
already alive, and verify recovery after a real runtime restart. This repository
does not supply a portable keeper for that lifecycle; document the target-specific
registration and rollback before claiming unattended recovery. If supervision is
unavailable, describe the deployment as manual-start rather than auto-recovering.
See the [runtime lifecycle contract](runtime-lifecycle.md) to separate intake,
short checks and model execution without assuming a parent-process escape.
The launch command above also serves as the manual recovery entry after checking
for an existing listener; preserve `.local/` and reconcile unfinished work first.

Register a runtime hook to call the worker no faster than every five seconds:

```sh
sh deploy/run-worker.sh next
```

Only a non-null packet should start a Muse turn. Preserve its `event_id`.
Pass the composed response through stdin to `run-worker.sh reply EVENT_ID`,
or finish a no-reply turn with `run-worker.sh skip EVENT_ID`. See
`worker.md` for the full contract and delivery recovery.

Before composing any new reply, follow the packet's mandatory exact-event
`queue_context.refresh_argv` to refresh newer same-conversation context with
`sh deploy/run-worker.sh context EVENT_ID` on this adapter profile. This bounded
read-only snapshot rechecks room membership and leaves all jobs untouched;
see [context refresh](worker.md#refresh-context-before-replying).
It improves context freshness, not queue throughput or missing intake.

The room runtime should use its own conversation context, not the owner's
private DM context. Separate context and separate execution capacity are
different properties: verify both. Merely creating a new session does not
prove that it avoids a global runtime execution queue.

## 5. Diagnose by stage

Opt into metadata-only intake diagnostics while investigating silent drops:

```sh
sh deploy/run-listener.sh --diagnostics
```

For generic secure token injection, add `--diagnostics` to the listener CLI.
Stderr emits JSON stage/reason and UTC time, plus event ID/message timestamp
only when their formats validate. Bodies, sender/channel IDs, tokens and raw
exception details are excluded. `dropped` reasons include `no-mention`,
`no-audience`, `unapproved-bot` and `private-room-mismatch`; `queued` and
`duplicate` follow durable intake. No pre-drop body is saved. Keep diagnostic
logs local and protected. This instrumentation cannot explain past drops that
occurred before it was enabled, and it does not measure runtime execution.

Startup authentication makes at most five attempts for transient network/API
failures. HTTP 408/429/500/502/503/504 may retry; other HTTP errors, including
401/403, stop immediately. Valid `Retry-After` values (seconds or HTTP date)
set a minimum delay and are not truncated by the backoff cap. Missing or invalid
values use bounded exponential backoff. Invalid bot identity/credentials fail closed. WebSocket
connection exceptions and empty receives trigger reconnect. These are recovery
inside the listener process, not process supervision after a runtime restart.

Use event identifiers and timestamps, not message bodies or credentials.

| Stage | Evidence to check | What it does not establish |
| --- | --- | --- |
| App setup | Correct workspace/app, scopes and credential registrations | Running listener |
| Intake | Socket connection and an authorized durable queue record | Worker started |
| Eligibility | Owner/agent IDs and current exact room membership | All ignored events are failures |
| Dispatch | Worker claim and runtime turn receipt | Turn finished |
| Execution | Runtime start/end and queue wait | Slack accepted a post |
| Delivery | Confirmed Slack message timestamp and matching event | Correct speaker/location without checking them |

No queue record: check connection, event subscriptions, room invitation,
mention policy and membership. A bot event being available in history does
not prove it woke the runtime.

Queue record but no turn: use `.venv/bin/python -m bridge.worker status` for
metadata-only counts and `.venv/bin/python -m bridge.worker pending` for
unfinished event IDs. These do not require Slack credentials or make Slack
requests; they may initialize local worker state. Then inspect the runtime hook,
and older unfinished jobs in the same conversation. A delayed status snapshot
may describe a turn that has already replied; correlate event ID and delivery.

Turn running but slow: measure receive, enqueue, claim, runtime start/end,
and post times separately. Test room responsiveness while an owner-DM turn
is busy. A protocol ACK or receipt reaction is not task completion. Do not
introduce an automatic text acknowledgement for every bot post: no-reply
turns must remain possible without creating acknowledgement loops.

Delivery uncertain: check actual Slack delivery before using `reconcile`.
Never erase the worker database or blindly replay the turn. Claim age is diagnostic only; the worker never automatically replays an aged
claim. Verify the old runtime stopped and inspect its progress before deliberately
using `resume`. Repeating its prior external actions may be unsafe.

## 6. Acceptance before calling setup complete

- Owner DM receives one reply with the intended bot identity.
- Approved agents wake a turn naturally only in the configured private room.
- In an agent room, test owner plain text, name-only calls and explicit mentions
  separately. In the reviewed code a configured agent room bypasses mention
  gating for the owner too; a mention-only result needs a deployed-config,
  source-version or upstream-hook explanation rather than being assumed normal.
- A top-level room message receives a top-level reply; an existing-thread
  message stays in that thread. Test the actual deployed settings.
- No-reply/own-bot events do not create an empty reply loop.
- A long owner-DM turn does not silently block the intended room workflow.
- Restart preserves unfinished work and uncertain delivery without duplicates.
- Credential absence, network loss and listener/fallback handover are visible
  and recoverable; offline tests alone do not establish live behavior.

Record the source commit, local modifications (filenames only), configuration
revision, launch method and tested stages in ignored local deployment notes.
Keep public documentation free of real deployment IDs, hosts and conversations.
