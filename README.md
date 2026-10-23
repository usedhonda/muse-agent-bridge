# muse-slack-bridge

Slack Socket Mode bridge that lets Muse (Meta personal agent) join Slack as a bot agent.

Status: **M0 — design only.** Nothing is running yet.

## What this is

A dedicated Slack app (bot identity) plus a small bridge on Muse's agent VM:

- A Socket Mode listener receives DMs and mentions and writes them to a
  durable local queue.
- A wake hook (5-second poll) starts a Muse turn; replies are posted as the
  bot, in the originating thread. An ack reaction is posted on receipt, so
  responses feel immediate (seconds, not minutes).

Modeled on OpenClaw's Slack channel (`openclaw/openclaw`,
`extensions/slack/`, `docs/channels/slack/`), adapted to how Muse runs:
the design contracts — manifest, scopes, token model, mention gating,
thread sessions — carry over; the runtime does not.

## Policy

- **Owner-only in v1.** The bridge answers exactly one person: its owner.
  Everyone else is dropped silently at the listener — no reply, no reaction.
  See the visibility boundary in [docs/design.md](docs/design.md).
- **Information governance is built in from the start**, so the bridge can be
  opened to guests later without redesigning: everything the agent knows is
  scope-tagged (`owner` by default), guest conversations are served only
  from their own context plus explicitly shared items, and owner-private
  facts are neither confirmed nor denied to guests. Opening a guest audience
  is gated on leak tests (milestone M4).

## Repository hygiene

This repository is public and contains only code, the app manifest, an
example config, and docs. **No real tokens, workspace/channel IDs, owner
IDs, hostnames, or conversation content belong here.** Local state lives in
`.local/` (gitignored); tokens live only in Muse's secure connection.

## Layout

- [`docs/design.md`](docs/design.md) — architecture, owner-only access,
  information governance, safety rules, milestones, open questions
- [`slack-app-manifest.yaml`](slack-app-manifest.yaml) — Slack app manifest
  (Socket Mode, v1-minimum scopes and events)
- [`config.example.json`](config.example.json) — example local config;
  copy to `.local/config.json` and fill in locally
