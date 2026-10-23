# Design

Status: M0 (design only, nothing is running yet)

## Goal

Give Muse (Meta's personal agent) its own presence in Slack, modeled on how
OpenClaw's Slack channel works: a dedicated Slack app (bot identity), reached
over Socket Mode, that can receive DMs and mentions and reply in thread.

**Owner-only by policy.** This bridge serves exactly one human: the workspace
owner who installed it. It must not answer anyone else, and it should be as
invisible to other people as the deployment shape allows (see
"Owner-only access" below).

Non-goals for v1: group conversations with third parties, file/media handling,
slash commands, Slack Agent View / assistant surfaces, posting as the user.

## What we take from OpenClaw (and what we do not)

Reference: `openclaw/openclaw`, `extensions/slack/` and `docs/channels/slack/`.

Reused as design input (contracts and checklists, not code):

- Slack app manifest shape for Socket Mode, bot scope list, bot event list
  (`docs/channels/slack/manifest-and-scopes.md`), trimmed here to a v1 minimum.
- Token model: a bot token (`xoxb-…`) for Web API calls and an app-level token
  (`xapp-…`, `connections:write`) for the Socket Mode connection.
- Access-control ideas: DM policy, channel allowlists, mention gating, and
  ack reactions (`docs/channels/slack/access-control.md`, `messaging.md`).
  Our owner-only rule below is a stricter, fixed version of these.
- Thread/session behavior: one conversation thread maps to one agent context;
  replies stay in the originating thread.

Not reused:

- OpenClaw's runtime, plugin SDK, and session store. OpenClaw's agent process
  is itself driven by the socket; Muse's agent turns are started by the Muse
  runtime, so this bridge ends at a durable local queue plus a fast wake hook.
- Agent View / `assistant:write` surfaces, which depend on OpenClaw's own
  session model. They are out of scope for v1.

## Architecture

```text
Slack workspace
   │  Socket Mode (outbound WSS from the agent VM)
   ▼
listener (service on the Muse VM)
   │  1. verifies the event is from the owner (see below); drops everything else
   │  2. posts an immediate ack reaction (owner's conversations only)
   │  3. appends the event to a durable local queue
   ▼
.local/state/queue            (local only, never committed)
   │  wake hook polls the queue every 5 s (minimum interval)
   ▼
Muse worker turn
   │  reads context, composes a reply
   ▼
bot Web API client            (credentials via Muse's secure connection)
   │  chat.postMessage to the same channel/thread, as the bot
   ▼
Slack workspace
```

Latency budget (targets, not guarantees): socket-to-queue under a second,
queue detection within one 5-second hook poll, a typical end-to-end reply in
seconds to tens of seconds including model time. The ack reaction is posted by
the listener itself, so "seen" feedback is effectively immediate.

## Owner-only access

This is a hard requirement, enforced at the earliest point (the listener),
not a prompt-level instruction:

1. **Sender allowlist with exactly one entry**: the owner's Slack user ID,
   stored only in `.local/config.json`. Events from any other sender are
   dropped silently before queueing — no reply, no ack reaction, no error
   message to the sender. Message bodies of dropped events are not retained.
2. **DMs**: only the owner's DM with the bot is accepted. There is no pairing
   flow for anyone else.
3. **Channels**: the bot never auto-joins. It only acts in channels listed in
   the local channel allowlist, and only for the owner's messages. Other
   members' messages in those channels are context at most, never triggers,
   and v1 defaults to mentions-only even there.
4. **Visibility limits (honest boundary)**: an app installed in a shared
   workspace still appears in that workspace's app directory; Slack offers no
   per-user hiding for installed apps. Full invisibility is only achieved by
   installing the app into a workspace (or org) where the owner is the only
   member. Both shapes are supported by this design; the sender gate applies
   identically either way. The deployment choice is recorded in
   `.local/config.json`, never in this repository.

## Information governance (built in from v1, required before opening up)

The bridge is owner-only in v1, but it is designed to be opened to other
people later. That only becomes safe if information-flow rules are
architectural from the start — retrofitting them onto a shared memory is
where leaks happen. The rules below therefore apply already in v1, while
there is only one audience: the machinery runs, with a single domain active.

1. **One principal.** The owner is the only principal. Everyone else is a
   guest: a guest's requests are input to consider, never instructions that
   can expand what the agent may disclose or do.
2. **Context domains.** Every piece of stored state (thread state, notes,
   queue entries, future memory items) carries a scope tag:
   `owner` (the default for anything untagged), `shared:<audience>`, or
   `public`. Bridge state files include the tag from the first version.
3. **One-way flow.** Information flows toward the owner freely. It flows
   from the owner domain to a guest audience **only** when the specific item
   is tagged `shared:<audience>` or `public`, or was created inside that
   guest conversation for that audience. Content from one guest conversation
   never flows into another guest conversation.
4. **Enforcement by construction.** A worker turn that serves a guest
   conversation is assembled only from that conversation's own history plus
   `shared:<audience>` / `public` items. Owner-private context is never
   loaded into such a turn, so it cannot leak what it never saw. Prompt-level
   "please don't tell" is a backstop, never the mechanism.
5. **Neither confirm nor deny.** To a guest, the agent does not confirm or
   deny owner-private facts — including whether such information exists.
   Refusals must not leak through their wording.
6. **Owner-only re-tagging.** Only the owner, speaking in an owner
   conversation, can tag an item as shared or authorize a one-off disclosure.
   Authorization is per item and per audience; it does not generalize, and a
   guest can never grant it.
7. **Surface separation.** Knowledge from the owner's other surfaces (this
   chat, email, calendar, other workspaces, other projects) lives in the
   owner domain by default, exactly like rule 3 — being reachable by the
   agent does not make it shareable.
8. **Opening gate.** No non-owner audience is enabled until these rules are
   implemented and pass leak tests: scripted guest conversations that probe
   for owner-private facts, for other guests' content, and for the existence
   of information, all answered without disclosure. Results are recorded
   before the audience is enabled (milestone M4).

## Repository vs local state

This repository is public. It contains code, the app manifest, an example
config, and documentation — nothing environment-specific.

Kept only in `.local/` (gitignored) or in Muse's secure connection:

- bot token and app-level token (secure connection only; never on disk in
  this repo's working tree, never in logs, never in chat)
- workspace ID, owner user ID, channel allowlist with real IDs
- queue, dedupe state, per-thread conversation state, logs

No real hostnames, personal paths, workspace names, or conversation content
in committed files, examples, fixtures, or docs.

## Safety rules

- The bridge always posts **as the bot**. It never uses the separate
  user-identity Slack connector, so its messages cannot be mistaken for the
  owner typing.
- Reply targets are pinned to the originating channel/thread of the accepted
  event. The agent cannot redirect a reply to a different channel.
- Dedupe by Slack event ID; Socket Mode envelopes are acked immediately and
  processed idempotently, so redelivery never produces a duplicate reply.
- Kill switch: disabling the wake hook and stopping the listener stops all
  behavior; revoking the app installation or either token disables the
  bridge entirely without affecting anything else on the VM.
- The listener holds no authority beyond receiving events and posting the ack
  reaction. Composing and sending replies happens only inside a Muse turn.

## Setup sequence (operator steps, later milestones)

1. Create the Slack app from `slack-app-manifest.yaml` and install it in the
   chosen workspace.
2. Generate an app-level token (`connections:write`) in the app settings.
3. Register the bot token and app token through Muse's secure connection
   flow (never by pasting them into chat or files).
4. Copy `config.example.json` to `.local/config.json` and fill in the local
   values (owner user ID, channel allowlist, deployment shape).
5. Install and start the listener service; enable the wake hook.
6. Smoke test in a private test channel: owner mention → ack reaction →
   threaded reply; a non-owner message → complete silence.

## Milestones

- **M0** — design, manifest, example config, repo hygiene (this state).
- **M1** — listener + queue + wake hook; ack reaction only, no agent replies.
- **M2** — agent replies, owner-only, in one private test channel/DM.
- **M3** — review logs and gating behavior; only then consider widening the
  channel allowlist (the sender allowlist never widens beyond the owner
  unless the owner explicitly changes this policy).
- **M4** — opening to a first guest audience, gated on the information
  governance rules above being implemented and passing the leak tests.
  Widening access is an owner decision made at that time, not a default.

## Open questions

- OQ-1: credential delivery to the long-running listener. The agent-side Web
  API client can use Muse's secure connection per request; the listener's
  Socket Mode connection needs the app-level token at process start. The
  mechanism (secure-connection-backed launcher vs. another approved path)
  must be settled in M1 without ever writing the raw token into this repo,
  a unit file, or logs.
- OQ-2: per-thread memory. How much thread history the worker turn should
  load by default, and where the boundary sits between bridge state
  (`.local/`) and the agent's own memory.
