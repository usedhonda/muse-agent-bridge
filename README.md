# muse-slack-bridge

Slack Socket Mode bridge that lets Muse (Meta personal agent) join Slack as a bot agent.

Status: **M1 listener and M2 worker interface are implemented and offline-tested.** An owner/private-room round trip was verified on an earlier deployment. New agent-room routing is offline-tested and has not been deployed. See [HANDOFF.md](HANDOFF.md).

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

This repository is public and contains only code, the app manifest, an
example config, and docs. **No real tokens, workspace/channel IDs, owner
IDs, hostnames, or conversation content belong here.** Local state lives in
`.local/` (gitignored); tokens live only in Muse's secure connection.

## Layout

- [`docs/design.md`](docs/design.md) — architecture, owner-only access,
  information governance, safety rules, milestones, open questions
- [`slack-app-manifest.yaml`](slack-app-manifest.yaml) — Slack app manifest
  (Socket Mode, v1-minimum scopes and events)
- [`docs/slack-recommended-settings.yaml`](docs/slack-recommended-settings.yaml) —
  recommended Slack-side settings the manifest cannot express (app home,
  workspace admin, stealth deployment profiles)
- [`docs/worker.md`](docs/worker.md) — Muse wake-hook interface and recovery
- `bridge/` — the listener and durable worker interface; `tests/` — offline tests (`python3 -m unittest discover -s tests`)
- [`config.example.yaml`](config.example.yaml) — example local config;
  copy to `.local/config.yaml` and fill in locally
