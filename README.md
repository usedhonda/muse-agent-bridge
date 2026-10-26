# muse-slack-bridge

A Slack intake and reply bridge for an existing Muse agent runtime. It filters
messages by local audience configuration, persists accepted messages, and exposes
a worker CLI for the runtime to claim turns and post replies.

**This repository is not a turnkey model agent.** You must supply the model/runtime,
its wake hook, and secure credential injection. Starting an intake process alone
does not produce replies. Choose Socket Mode or the VM-specific polling profile;
use the setup steps below and [VM deployment](docs/deployment-vm.md) for
supervision and recovery.

## What this is

A dedicated Slack app (bot identity) plus a small bridge on Muse's agent VM:

- A Socket Mode listener receives DMs and mentions and writes them to a
  durable local queue.
- A wake hook (5-second poll) starts a Muse turn; replies are posted as the
  bot, in the originating thread, or directly in an explicitly configured private agent room. Receipt reactions are optional; the matching private secretary
  deployment profile leaves them disabled.

Modeled on OpenClaw's Slack channel (`openclaw/openclaw`,
`extensions/slack/`, `docs/channels/slack/`), adapted to how Muse runs:
the design contracts — manifest, scopes, token model, mention gating,
thread sessions — carry over; the runtime does not.

Status: **M1 listener and M2 worker interface are implemented and offline-tested.** An owner/private-room round trip was verified on an earlier deployment. New agent-room routing is offline-tested and has not been deployed. See [HANDOFF.md](HANDOFF.md).

## Quick start

From a checkout, prepare an isolated Python environment and local configuration:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
mkdir -p .local
cp config.example.yaml .local/config.yaml
python -m unittest discover -s tests
```

Fill in the owner and channel placeholders in `.local/config.yaml`, keeping
`ackReaction: null` for the supplied manifest. Use one audience with ID and scope
`owner`, one human owner in `userIds`, and `dmEnabled: true`. For DMs only, set
`channelAllowlist: []`; otherwise allowlist only approved private channels and
invite the bot. Keep `mentionsOnlyInChannels: true` for ordinary channels. Use
[the worker contract](docs/worker.md) before enabling private agent rooms.

Create a Slack app from [the manifest](slack-app-manifest.yaml), review its scopes
and [Slack-side settings](docs/slack-recommended-settings.yaml), then install it in
your workspace. Reinstall after scope changes. The bridge uses a bot token
(`SLACK_BOT_TOKEN`, normally `xoxb-…`) for API reads/posts. Socket Mode also needs
an app-level token (`SLACK_APP_TOKEN`, normally `xapp-…`) with `connections:write`.
A user token cannot replace either. Have an approved secret injector supply these
environment variables; never place them in config or shell history. The manifest
subscribes to `message.im` and `message.groups`, with bot scopes for DM/private
channel reads and replies. Receipt reactions require adding `reactions:write`
and changing local `ackReaction` together.

## Bridge core and the Telegram adapter

The repository also carries `bridge_core/` — a platform-neutral intake
core (locked atomic state, dedupe, batch-claim discipline) shared by
platform adapters, with a working Telegram adapter on top of it. See
[docs/bridge-core.md](docs/bridge-core.md) for the architecture, the
guarantees the core makes, setup, migration, and rollback.

Choose one intake profile:

- **Socket Mode:** after injecting both tokens, run
  `.venv/bin/python -m bridge.listener --config .local/config.yaml --state .local/state`.
  Configured private agent rooms use the shared room admission checks.
- **VM polling:** requires Muse's `dynamic_credentials` helper and surrogate-aware
  egress; pip installation alone is insufficient. Start with one owner and DMs
  only, then run `.venv/bin/python -m bridge.poller --interval 20`.
  Configured `agentChannels` use the same private/unshared membership, sender
  and routing normalization as Socket intake. History pagination is supported;
  fetching existing thread replies separately is not implemented or verified.
  A fresh watermark can ingest existing owner messages, so inspect the intended
  DM before starting. See [VM deployment](docs/deployment-vm.md) for launcher
  dependencies and transport cutover precautions.

Register a wake hook in your existing runtime to invoke `sh deploy/run-wake.sh`
on the Muse credential-adapter profile, or `python -m bridge.worker next
--with-context` with secure bot injection, no faster than every five seconds.
This executes bounded context refresh before handing the packet to the runtime;
implement the turn contract below.
The `hookPollIntervalSec` config field does not schedule a hook by itself.

The offline tests require no Slack credentials and do not prove a live deployment.
Use `python -m bridge.worker status` for local queue/job counts without claiming
a turn or contacting Slack; it may initialize the worker database. `next` changes
durable state. For live acceptance, send a harmless owner DM and verify one runtime
turn and a delivered reply, then verify an unauthorized sender leaves no queue
record, reaction or reply. Check `pending` for unfinished jobs; recover interrupted
turns deliberately before retrying external actions.

## How a turn works

1. Intake accepts an eligible DM or configured channel message into a durable queue.
2. Your runtime uses the context-refreshing wake adapter, starts a model turn only for
   a non-null packet, and retains its `event.event_id`.
3. Your runtime supplies the reply text to `python -m bridge.worker reply EVENT_ID --context-receipt RECEIPT_ID`
   on stdin, or completes a no-reply turn with `skip EVENT_ID`.

Refresh immediately before composing the reply and use the returned `receipt.id`
as `RECEIPT_ID`. For example, with the bot credential injected:

```sh
python -m bridge.worker context EVENT_ID
python -m bridge.worker reply EVENT_ID --context-receipt RECEIPT_ID < .local/reply.txt
```

Reply text must be nonempty UTF-8 and at most 4,000 characters. Commands default
to `.local/config.yaml` and `.local/state`; custom `--config` and `--state` options
go before the worker subcommand. Keep intake and worker state paths identical.

The worker fixes the Slack destination from the queued event. Optional receipt
reactions indicate intake, not model progress or completion of the user's task.
A successful post confirms message delivery; the runtime must verify and describe
any external work separately. Recovery after uncertain delivery requires operator
reconciliation; see [the worker contract](docs/worker.md).

## Access and privacy

Audience admission is controlled by local configuration. Start with one owner
human principal and only approved private channels. Named agent exceptions belong
in `agentChannels`, not `userIds`. The worker currently serves the audience whose
ID and scope are both `owner`; guest access remains gated on the design's M4 work.
Runtime behavior must also enforce the context and authorization policies described
in [the design](docs/design.md) and the worker's conversation policy.

The manifest includes read-only user scopes for a broader observation profile,
but this bridge uses the bot token and does not implement that observation feature.
Slack app visibility and enabled scopes do not prove access restrictions or runtime
behavior. Review [Slack-side settings](docs/slack-recommended-settings.yaml) too.

## Policy

- **Multi-audience capable, locked down by configuration.** The bridge can
  serve multiple audiences under the governance rules below; who it serves
  is local config, not code. The first deployment enables exactly one
  audience — the owner — with everyone else dropped silently at the
  listener, and uses Slack-side settings to stay invisible to others.
  See [docs/design.md](docs/design.md).
- **Information governance is built in from the start**, so the bridge can be
  opened to guests later without redesigning: everything the agent knows is
  scope-tagged (`owner` by default), guest conversations are served only
  from their own context plus explicitly shared items, and owner-private
  facts are neither confirmed nor denied to guests. Opening a guest audience
  is gated on leak tests (milestone M4).

## Repository hygiene

This public repository contains code, example configuration, and documentation.
Keep real tokens, workspace/channel/user IDs, hosts and conversation content out
of commits. Local configuration and state belong in `.local/` (gitignored);
credentials belong in your approved secure connection or secret injector.

This repository is public and contains only code, the app manifest, an
example config, and docs. **No real tokens, workspace/channel IDs, owner
IDs, hostnames, or conversation content belong here.** Local state lives in
`.local/` (gitignored); tokens live only in Muse's secure connection.

## Further reading

- [Getting started and diagnosing a silent bot](docs/getting-started.md) — detailed VM profile and stage checks
- [Review backlog](docs/review-backlog.md) — completed offline work and remaining acceptance

- [VM deployment](docs/deployment-vm.md) — operator-selected profiles, supervision and upgrades
- [Investigation ledger](docs/muse-ssh-experiment-ledger.md) — reproducible observations, hypotheses and privacy boundaries
- [Relay timeout proposal](docs/relay-timeouts.md) — connect timeout versus established-session idle behavior
- [Runtime lifecycle](docs/runtime-lifecycle.md) — separate short checks, resident intake and model execution
- [Worker integration](docs/worker.md) — Muse wake-hook interface, routing and recovery
- [Design](docs/design.md) — architecture, governance and milestones; owner-only access, information governance, safety rules, open questions
- [Slack manifest](slack-app-manifest.yaml) and [example config](config.example.yaml) — copy the example to `.local/config.yaml` and fill in locally
- [Slack-side settings](docs/slack-recommended-settings.yaml) — recommended Slack-side settings the manifest cannot express (app home, workspace admin, stealth deployment profiles)
- [Bridge core](docs/bridge-core.md) — platform-neutral bridge core and the Telegram adapter: operation, setup, migration, rollback
- `bridge/` — the listener and durable worker interface; `bridge_core/` — the platform-neutral core and adapters; `tests/` — offline tests (`python3 -m unittest discover -s tests`)

New replies require a successful exact-claim context receipt, valid for 60 seconds.
Pass `receipt.id` from `context EVENT_ID`; send refusal preserves the job.
See [the worker contract](docs/worker.md#refresh-context-before-replying) for freshness, audit writes and no-retry boundaries.

See [shared room polling and limits](docs/poll-room-normalization-plan.md) for
page/checkpoint recovery, cutover boundaries and unimplemented thread backfill.
