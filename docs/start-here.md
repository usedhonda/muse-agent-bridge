# Start here: your first bridge reply

This guide takes you from zero to a Muse that answers you on Telegram, then
points you at Slack. It assumes no prior knowledge of this repository.

## What you are building

A small bridge program sits between a chat platform and your Muse:

- The bridge watches a Telegram bot (or a Slack app) for messages addressed
  to you-as-owner, stores them in a durable local queue, and wakes Muse up.
- Muse reads the queued message, decides what to say, and replies through
  the bridge as the bot — never as you.
- The bridge is not a model and not a Muse runtime. Your Muse does the
  thinking; the bridge is the mailbox and the doorbell.

## Before you start

You need:

- A Muse environment that can run local commands and register a recurring
  wake hook. If you are reading this as a Muse user, your own Muse can do
  both — you will ask it to, in the steps below.
- Python 3 with `venv` on the machine where the bridge will run.
- A Telegram account (for this guide). The Slack path is linked at the end.

Everything below runs on one machine: the one your Muse runs on.

## Path A: Telegram (recommended first)

### 1. Create the bot

1. In Telegram, open a chat with **@BotFather** and send `/newbot`.
2. Pick a name and a username ending in `bot`. BotFather replies with a
   token. Keep it for step 2 only — never paste it into a chat, a file, or
   this repository.
3. Optional, only if you later want a group with other bots: in BotFather,
   open your bot's settings and enable **Bot-to-Bot Communication Mode**.

### 2. Register the token with Muse

Ask your Muse, in your own words, to do this:

> Register a custom connector for the Telegram Bot API. Provider name
> `custom.telegram-muse-bot`, API host `api.telegram.org`, API key placed
> in the URL path segment `/bot{}`. Show me the secure registration page.

Your Muse will show you a secure page. Paste the BotFather token there.
The token is stored in Muse's secure connection; the bridge only ever
sees a short-lived stand-in that Muse swaps for the real token on the way
out.

### 3. Install the bridge

```sh
git clone https://github.com/usedhonda/muse-agent-bridge.git
cd muse-agent-bridge
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests
```

All tests should pass. They need no credentials and contact no platform.

Pick a state file location **outside** the repository, for example
`~/telegram-bridge/state.json`, and create its directory. The state file
holds the queue; it is yours alone and is never committed.

### 4. Wire the wake hook

Ask your Muse to register a recurring hook (every 20 seconds is a good
start) that runs, from the checkout directory:

```sh
TELEGRAM_BRIDGE_STATE_PATH=<your state file path> \
PYTHONPATH=<checkout directory> \
python3 -m bridge_core.telegram
```

What this does each cycle: poll Telegram for new messages, queue the ones
from senders the bridge serves, and — when there is something to answer —
wake a Muse turn that claims the batch and replies through the bridge
CLI. Replies use a guarded send path so a message is never answered twice.

### 5. Adopt yourself as the owner

1. Send your new bot any direct message, for example `hello`.
2. Your message is recorded as an owner candidate (nobody is served yet).
3. From the checkout, run once, with your Telegram user ID — it is shown
   in the candidate record in your state file:

```sh
TELEGRAM_BRIDGE_STATE_PATH=<your state file path> \
PYTHONPATH=<checkout directory> \
python3 -m bridge_core.telegram --adopt <your user id>
```

From now on, only your DMs are served. Everyone else is ignored silently.

### 6. Send the first real message

Send the bot a question you know the answer to. Within a hook cycle or
two, Muse should reply as the bot. You have a working bridge.

If nothing comes back, check in this order:

1. Is the hook actually running? Ask your Muse to show its recent runs.
2. Did your message reach the queue? Inspect the state file's newest
   items — yours should be there, marked claimed or replied.
3. Is the credential resolving where the hook runs? The hook must run in
   the same context your Muse uses for secure connections. A hook that
   runs in a bare service context cannot swap the stand-in token.
4. Still stuck? The deeper diagnostics live in
   [the bridge core doc](bridge-core.md) and
   [runtime lifecycle](runtime-lifecycle.md).

### 7. Optional: a group room

Add the bot to a group and promote it to admin (or turn off its privacy
mode in BotFather), so it receives all group messages. The first group
where **you** send a text message becomes the bridge's group. Other bots'
messages are served only when Bot-to-Bot Communication Mode is on
(step 1). Muse answers when addressed or handed a turn; it does not
reply to everything.

## Path B: Slack

Slack has more moving parts — an app to create from the supplied
manifest, two tokens (bot and app-level), and an intake choice between
Socket Mode and polling. Follow the [README quick start](../README.md#quick-start)
and then [Getting started and diagnosing a silent bot](getting-started.md).
The concepts from Path A carry over: credentials live in Muse's secure
connection, configuration and state live in `.local/`, and the bridge
posts as its own bot identity, never as you.

## Safety defaults worth keeping

- Serve only yourself at first. The configuration is owner-only by
  design; opening audiences is a later, deliberate step — see the
  information-governance rules in [the design doc](design.md).
- Keep real tokens, IDs, and conversation content out of the repository.
  Configuration and state are local files, excluded by `.gitignore`.
- The bot is a separate identity. If you ever see the bridge posting as
  your user account, stop and treat it as a misconfiguration.
