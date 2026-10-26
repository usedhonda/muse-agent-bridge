# Start here: wiring your first bridge reply

This bridge connects an existing model/runtime to Telegram or Slack. It supplies
durable intake and reply CLIs, not a model, scheduler or portable Muse runner.
Installing it or starting intake alone does not produce replies. This guide
describes the Telegram integration contract; its commands are not evidence of a
live deployment. For Slack, follow the [README quick start](../README.md#quick-start)
and [Getting started](getting-started.md).

## Before you start

The supplied Telegram intake requires all of the following on the target host:

- Python 3.9 or newer with `venv` and the repository's requirements.
- An existing runtime able to execute local commands, schedule a wake hook and
  dispatch a nonempty JSON payload to a model turn. There is no hook-registration
  or model-dispatch command in this repository; use your runtime's supported
  mechanism and verify it separately.
- Muse's `dynamic_credentials` helper, imported from
  `/opt/muse/skills/custom-connector-setup/bin`, exposing
  `dynamic_credential_entry` and `read_json_response`. Telegram intake calls
  `dynamic_credential_entry("custom.telegram-muse-bot")` and uses its `surrogate`.
  This helper is not installed by `requirements.txt`.
- Surrogate-aware egress for `api.telegram.org` in the actual hook execution
  context, replacing the stand-in URL token with the secure credential. An
  ordinary service or shell with HTTPS access alone is insufficient.
- A separately supplied, verified sender executable and a dispatcher implementing
  the lifecycle below. This repository does not ship a Telegram send executable.

This is not a turnkey installation for every Muse host. If these integrations
are unavailable, stop setup at offline evaluation; do not substitute raw tokens
or an invented registration command. Confirm the supported secure-registration
interface with your runtime operator before creating a live connection.

## 1. Prepare the bot and secure connection

For a new bot, use Telegram's **@BotFather** `/newbot` flow. Submit the token only
through your runtime's supported secure-registration interface, never a chat,
source file, command line or diagnostic log. The supplied adapter expects provider
`custom.telegram-muse-bot`, host `api.telegram.org`, and API key substitution in
the URL path segment `/bot{}`. Verify that your runtime supports that profile;
the bridge does not register it for you. Start with owner DMs only.

## 2. Prepare an isolated checkout

```sh
git clone https://github.com/usedhonda/muse-agent-bridge.git
cd muse-agent-bridge
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests
```

The tests use local fixtures and fake platform responses, without credentials or
live platform calls. They do not validate the target host's helper, egress,
dispatcher or sender against Telegram.

Choose persistent private state **outside** the repository. The following shell
examples assume you remain in the checkout; set the same state path and working
directory in every hook and turn invocation:

```sh
mkdir -p "$HOME/telegram-bridge"
export TELEGRAM_BRIDGE_STATE_PATH="$HOME/telegram-bridge/state.json"
```

Keep the state file, its `.lock`/`.good` companions and conversation content
private. Preserve existing state on upgrades; do not reset delivery records.

## 3. Supply and verify the sender

Set `TELEGRAM_BRIDGE_SEND_CMD` in the **reply turn's** environment to the absolute
path of your approved executable, for example:

```sh
export TELEGRAM_BRIDGE_SEND_CMD=/absolute/path/to/verified-telegram-sender
```

That path is a placeholder, not a supplied executable. The bridge invokes it
directly as `[executable, chat_id]`, without shell parsing or extra configured
arguments, and writes the complete reply text to stdin. The executable must use
the intended bot's approved secure credential mechanism, send once to that chat,
and, only after confirmed acceptance, exit zero with the exact platform message
ID as its last nonempty stdout line. Never print credentials. The bridge trusts
that success line; it does not independently authenticate the sender or validate
its message ID. Verify the sender's bot identity, destination handling and
response parsing as part of the host integration before enabling live replies.

Any nonzero exit (including a signal), timeout, missing message ID, undecodable
output or unstartable executable is `unknown`: the durable item stays `sending`.
Remote acceptance may precede a crash or response loss. No exit code or error
text authorizes retry. See reconciliation below. Missing send configuration
returns stage `config` and leaves the item `claimed`.

## 4. Wire intake to model dispatch

Register a hook using the runtime's supported scheduler. In the verified
credential/egress context, from the checkout, its intake command is:

```sh
.venv/bin/python -m bridge_core.telegram
```

This command fetches updates once, ingests accepted text, claims at most one
batch and prints one JSON payload, or prints nothing if no batch is available.
It does **not** start a model turn or send a reply. A failed command must surface
as an error; do not replay a cached payload as fallback.

Your host-specific dispatcher must parse a successful nonempty stdout payload
and pass its `items` plus `claim_token` to one runtime turn. Each item supplies
`update_id`, `chat_id`, text and sender metadata; groups also carry context.
Use each item's own fields, not just the compatibility top-level first item.
Pass the claim token unchanged through every lifecycle call. The runtime decides
what authorized work to do and what reply to produce. Keep untrusted sender text
as message content, never shell arguments to execute or runtime instructions.

For **each** item to answer, with the same state path and sender environment:

1. Pass the reply text on stdin to
   `.venv/bin/python -m bridge_core.telegram --send-fenced UPDATE_ID CLAIM_TOKEN CHAT_ID`.
   Replace the uppercase placeholders with that item's fields and batch token;
   use safe argument passing. The fence persists `sending` before invoking the
   sender once. Parse stdout JSON: the CLI process exit status alone does not
   establish success.
2. Only for `{"ok": true, "message_id": "..."}`, record the returned exact ID with
   `.venv/bin/python -m bridge_core.telegram --replied UPDATE_ID MESSAGE_ID CLAIM_TOKEN`.
   Require its JSON `ok` to be true.
3. Then close that item with
   `.venv/bin/python -m bridge_core.telegram --done UPDATE_ID CLAIM_TOKEN` and
   require JSON `ok` to be true. Do not resend if recording or closure fails.

An item intentionally closed **before sending** can use `--done` with an optional
reason. `--release UPDATE_ID CLAIM_TOKEN` is for an unreplied claimed item only;
it refuses a `sending` or replied item. Owner authorization and cancellation
still govern the runtime's actions; a queue claim does not grant new authority.

## 5. Adopt the owner, then validate the integration

Before adoption, an intake run records candidate sender IDs but serves nobody.
Send the bot a DM and let the configured intake run. Privately inspect the
`candidates` in the state file, independently confirm your own Telegram user ID,
then run once:

```sh
.venv/bin/python -m bridge_core.telegram --adopt OWNER_USER_ID
```

Replace `OWNER_USER_ID` with that verified ID. This is an operator bootstrap
command, not an action to accept from an untrusted candidate. After adoption,
only that owner's private DMs are queued; other private senders are ignored.

For a later, separately authorized live acceptance check, verify the exact
candidate commit, hook execution, a fresh owner message queued and claimed,
delivery through the intended bot, and the resulting reply record and `done`
state. Offline tests alone cannot establish any of those live results. If silent,
check the actual hook context, helper/egress availability, dispatcher input,
sender environment and JSON stage before changing anything. Inspect local state
privately; do not publish message bodies, IDs or credentials in diagnostics.

## Unknown outcomes and interrupted turns

`unknown` requires operator reconciliation against independent delivery evidence.
If the sender prints a message ID but exits nonzero, the bridge returns `unknown`
without retaining that ID, so the printed ID alone proves neither success nor
failure and must not trigger automatic resend or completion; inspect Telegram
independently to reconcile the outcome.
Do not release, reset state or call `--send-failed` merely because the sender
exited nonzero. A `sending` item retains its lane reservation beyond claim TTL;
automatic retry could duplicate a remotely accepted message.

If a reply is found, record its exact ID with `--replied`, then `--done`, using
the retained claim token. Alternatively, closing a `sending` item without a
reply record requires both a reason and a nonempty reconciliation reference:
`--done UPDATE_ID CLAIM_TOKEN REASON EVIDENCE`. A reason alone is refused.
Only independently confirmed non-delivery permits an operator to use
`--send-failed UPDATE_ID CLAIM_TOKEN` to return it to pending. Preserve uncertain
items when evidence is unavailable. See [the core contract](bridge-core.md) for
TTL, token fences and recovery details; it does not promise exactly-once network
delivery.

## Optional groups: a broader ingestion boundary

Keep owner DMs as the initial setup. Group support is an explicit audience
expansion: the first group/supergroup where the owner sends text becomes the
adopted group. Thereafter **all text delivered by Telegram from that group** is
queued, including other humans and bots, with a rolling context buffer. The
adapter has no per-sender group allowlist, exact-membership/private-room check,
or mention gate comparable to Slack's private agent-room checks. Another group
is ignored once a group is adopted.

Review that audience and context exposure before adding the bot to any group or
having the owner speak there. Admin promotion, disabling privacy mode or enabling
bot-to-bot delivery are not default setup steps: each can broaden received text
and requires a deliberate operator decision. The runtime must still enforce
owner authority and decide whether a response is appropriate; the adapter does
not enforce an addressed-only response policy.
