# muse-slack-bridge

A Slack intake and reply bridge for an existing Muse agent runtime. It filters
messages by local audience configuration, persists accepted messages, and exposes
a worker CLI for the runtime to claim turns and post replies.

**This repository is not a turnkey model agent.** You must supply the model/runtime,
its wake hook, and secure credential injection. Starting an intake process alone
does not produce replies. Choose Socket Mode or the VM-specific polling profile;
use the setup steps below and [VM deployment](docs/deployment-vm.md) for
supervision and recovery.

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

Choose one intake profile:

- **Socket Mode:** after injecting both tokens, run
  `.venv/bin/python -m bridge.listener --config .local/config.yaml --state .local/state`.
  This is the profile for configured private agent rooms and membership checks.
- **VM polling:** requires Muse's `dynamic_credentials` helper and surrogate-aware
  egress; pip installation alone is insufficient. Start with one owner and DMs
  only, then run `.venv/bin/python -m bridge.poller --interval 20`.
  The poller excludes configured `agentChannels` entirely because it does not
  implement their membership gate or routing metadata. These rooms require Socket
  Mode; polling is not their fallback during a Socket outage.
  A fresh watermark can ingest existing owner messages, so inspect the intended
  DM before starting. See [VM deployment](docs/deployment-vm.md) for launcher
  dependencies and transport cutover precautions.

Register a wake hook in your existing runtime to invoke `python -m bridge.worker
next` no faster than every five seconds and implement the turn contract below.
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
2. Your runtime calls `python -m bridge.worker next`, starts a model turn only for
   a non-null packet, and retains its `event.event_id`.
3. Your runtime supplies the reply text to `python -m bridge.worker reply EVENT_ID`
   on stdin, or completes a no-reply turn with `skip EVENT_ID`.

For example, with the bot credential injected:

```sh
python -m bridge.worker reply EVENT_ID < .local/reply.txt
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

## Repository hygiene

This public repository contains code, example configuration, and documentation.
Keep real tokens, workspace/channel/user IDs, hosts and conversation content out
of commits. Local configuration and state belong in `.local/` (gitignored);
credentials belong in your approved secure connection or secret injector.

## Further reading

- [Getting started and diagnosing a silent bot](docs/getting-started.md) — detailed VM profile and stage checks
- [Review backlog](docs/review-backlog.md) — completed offline work and remaining acceptance

- [VM deployment](docs/deployment-vm.md) — operator-selected profiles, supervision and upgrades
- [Investigation ledger](docs/muse-ssh-experiment-ledger.md) — reproducible observations, hypotheses and privacy boundaries
- [Runtime lifecycle](docs/runtime-lifecycle.md) — separate short checks, resident intake and model execution
- [Worker integration](docs/worker.md) — wake hook, routing and recovery
- [Design](docs/design.md) — architecture, governance and milestones
- [Slack manifest](slack-app-manifest.yaml) and [example config](config.example.yaml)
- `bridge/` — implementation; `tests/` — offline tests
