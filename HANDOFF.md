# Handoff notes (2026-10-24)

Development continues from this snapshot. State of the work:

## Done

- **Design** (`docs/design.md`): architecture, audience-based access control,
  information governance (scope-tagged context domains, enforcement by
  construction, opening gate with leak tests), safety rules, milestones.
- **Slack app manifest** (`slack-app-manifest.yaml`): Socket Mode,
  v1-minimum bot scopes/events.
- **Recommended Slack-side settings** (`docs/slack-recommended-settings.yaml`):
  what the manifest cannot express, incl. the stealth deployment profiles.
- **M1 listener skeleton** (`bridge/`): config loader (YAML, rejects
  credential-like keys), audience resolution gate, durable fsync'd queue
  with cross-restart event dedupe, Socket Mode transport, listener
  (envelope ack → gate → queue → ack reaction). Tokens are read from the
  environment only.
- **Tests** (`tests/test_bridge.py`): 10 offline tests, all passing
  (`python3 -m unittest discover -s tests`). Cover owner/guest/stranger
  gating, mention rules, per-audience channel allowlists, dedupe, scope
  tagging, config validation.
- systemd user service template (`deploy/muse-slack-bridge.service`).

## Next

1. Create the Slack app from the manifest; generate the app-level token
   (`connections:write`) and install the app (bot token). Register both via
   the owner's secure flow — never in files.
2. Live smoke test of the listener (M1 completion): owner DM lands in the
   queue with a 👀 reaction; a non-audience message leaves no trace.
3. M2: worker side — 5-second queue hook, agent turn, reply posting with
   pinned targets and self-loop guard (see design, "Reply path").
4. Remove the obsolete `config.example.json` (replaced by the YAML example).

## Conventions

- Local/machine-specific state (IDs, tokens, hostnames) never enters this
  repository: real config lives in `.local/` (gitignored); see the design.
- Human-facing configuration in this repo is YAML (Slack accepts YAML
  manifests natively, and settings guidance needs comments).
- The bridge is multi-audience capable by design; deployments decide who
  is served purely through the audience registry in local config.
