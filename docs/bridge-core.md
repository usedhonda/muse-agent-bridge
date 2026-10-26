# Bridge core and the Telegram adapter

`bridge_core/` is the platform-neutral heart of this bridge, shared by
every platform adapter (Telegram today, Slack next). It exists because
the same failure modes — slow serial processing, duplicate replies
after a claim expired, state files corrupted by concurrent writers —
showed up in the standalone per-platform bridges. The core fixes them
once, in one place; adapters only fetch, filter, and shape payloads.

- `bridge_core/store.py` — locked, atomic JSON state storage.
- `bridge_core/queue.py` — intake queue with batch-claim discipline.
- `bridge_core/telegram.py` — the Telegram adapter (Bot API ingest,
  wake payload, operator CLI). It succeeds the standalone `poll.py`
  described under [Migration](#migration-from-the-legacy-pollpy).

Nothing in `store` or `queue` knows a concrete platform's API. Items
are identified by `(platform, external_id)`, so several adapters can
share one state document without ID collisions.

## What the core guarantees

### State store (`JsonStateStore`)

Every mutation runs as one transaction:

- An exclusive `flock` on a `<state file>.lock` side file serializes
  load → modify → save across processes (poller, woken turns marking
  items done, operator commands). Two writers can no longer interleave.
- Saves are a per-process temp file plus an atomic rename, so a reader
  never sees a half-written file.
- The last successfully saved state is also kept as a
  `<state file>.good` snapshot — as forensic evidence for the
  operator, never as an automatic fallback. If the main file exists
  but does not parse, or is missing while a snapshot exists, loads
  raise `StateStoreError` instead of silently rolling back: adopting
  an older snapshot would lose completion and reply records and
  could resurrect already-answered items for a duplicate reply. An
  operator inspects the damage and restores the snapshot
  deliberately. Only a brand-new deployment (neither file present)
  starts empty. A failed snapshot write likewise fails the save
  loudly rather than letting the restore source go silently stale.
- If the transaction body raises, nothing is saved — a half-applied
  update (for example a failed fetch mid-ingest) never persists.

### Queue (`queue`)

- **Dedupe:** `enqueue` drops an item whose `(platform, external_id)`
  is already known, so a platform redelivering an update cannot queue
  or wake it twice.
- **Batch claim, one lane:** one claim takes up to N oldest pending
  items (default 5) of a single conversation lane — one platform and
  one chat, the lane of the oldest pending item for the platform the
  adapter asked for. A batch never mixes platforms or conversations.
- **One batch in flight per lane:** while an item of a conversation
  lane is `claimed` or `sending` and not stale, that lane is not
  claimed again — polls for it stay silent instead of piling up
  claims ahead of the turn that will process them. Other lanes (and
  other platforms) stay claimable, so one conversation's backlog
  never stalls the rest.
- **Claim tokens:** each batch gets one random token. Every
  transition (`mark_sending`, `mark_replied`, `mark_done`, `release`,
  `mark_send_failed`) only applies when the caller's token matches
  the item's current claim; a wrong or missing token is a no-op.
  After a stale claim is recycled and the item re-claimed, the old
  token matches nothing, so a dead turn's late calls are all
  rejected.
- **Sending is an explicit lifecycle:** before calling the platform
  send API, the turn moves the item to `sending` (the Telegram
  adapter's fenced send does this as part of the send itself — see
  below). Entering `sending` is refused once a reply has been
  recorded. From there the send outcome may be unknown, so a
  `sending` item is *never* recycled and never released — it waits
  for an operator to reconcile it. The only ways out are recording
  the reply (`mark_replied`, then `mark_done`) or
  `mark_send_failed`, which is reserved for a definitive platform
  API failure where the send provably did not go through — and is
  refused once a reply has been recorded. Closing a `sending` item
  that has no reply record via `mark_done` requires reconciliation
  *evidence*: a non-empty reference to how the send outcome was
  established (the reply found in platform history, or a definitive
  non-delivery confirmation), stored as `done_evidence`. A reason
  alone never closes such an item — `done_reason` stays optional
  context, kept separate from the evidence.
- **Replied records are never recycled, released or re-claimed:**
  once a reply is recorded (`replied_at`, `reply_message_id`), the
  item can never return to pending or be claimed again, however old
  its claim gets — the reply already went out, so re-waking it could
  only produce a duplicate. `claim_batch` never selects such an
  item even if one somehow sits in `pending`; it is left for
  operator reconciliation.
- **Stale recycle is narrow:** only *unreplied* `claimed` items
  older than the TTL (default 1800s) return to pending, so a wake
  turn that died before sending does not block the queue forever,
  while a slow turn is never raced by a second claim.
- Every item records `received_at`, `claimed_at`, `sending_at` and
  `replied_at`, so intake latency can be measured end to end.

## Telegram adapter

### Runtime shape

The adapter is driven by a polling wake hook, not a daemon:

1. The hook runs the adapter (no arguments) in a context where Muse's
   secure-connection credential surrogate resolves — on the reference
   deployment that is the agent session's hook context, because a
   plain system service cannot fetch the surrogate.
2. Each run fetches new Bot API updates once, ingests the servable
   ones, claims one batch, and prints exactly one JSON wake payload.
   It prints nothing when there is no work — including while the
   previous batch for that conversation is still in flight.
3. The runtime wakes one agent turn with the payload. For each item
   it answers, the turn sends the reply through `--send-fenced`,
   which marks the item `sending` and performs the send under the
   fence (see CLI below), then records the reply with `--replied`
   and closes the item with `--done`. If the send outcome is ambiguous
   (including any nonzero sender exit, or the turn dying mid-send),
   the item stays in `sending` for an operator
   to reconcile — it is never resent automatically. Items closed
   without a reply only need `--done`; passing a short reason word
   as an extra `--done` argument stores it as the item's
   `done_reason`, so audits can tell silently closed items from
   answered ones. For a `sending` item whose reply was never
   recorded, a reason is not enough: `--done` also requires the
   reconciliation evidence argument (stored as `done_evidence`);
   without it, it prints `{"ok": false}` and changes nothing.
4. State lives **outside the repository** (see
   `TELEGRAM_BRIDGE_STATE_PATH`); the hook keeps the polling cadence
   (20s on the reference deployment).

### CLI

Run from the repository root (or with the repository on `PYTHONPATH`):

```sh
python3 -m bridge_core.telegram                      # poll once; print wake payload or nothing
python3 -m bridge_core.telegram --send-fenced <update id> <claim token> <chat id>   # reply text on stdin
python3 -m bridge_core.telegram --sending <update id> <claim token>
python3 -m bridge_core.telegram --done <update id> <claim token> [reason] [evidence]
python3 -m bridge_core.telegram --release <update id> <claim token>
python3 -m bridge_core.telegram --replied <update id> <reply message id> <claim token>
python3 -m bridge_core.telegram --send-failed <update id> <claim token>
python3 -m bridge_core.telegram --adopt <owner user id>
```

Turn protocol per replied item: `--send-fenced` (marks `sending`
and sends) → `--replied` → `--done`.

- `--send-fenced`: the structural send path for queue replies; it
  makes the send discipline a property of the bridge instead of the
  prompt. The reply text is read from stdin. In one transaction the
  fence verifies the item is claimed, the token matches and no
  reply is recorded, and moves it to `sending` — persisted *before*
  any send happens. If a check fails it prints
  `{"ok": false, "stage": "fence"}` and nothing is sent. Only then
  does it run the deployment's send command
  (`TELEGRAM_BRIDGE_SEND_CMD`) exactly once:
  - exit 0 with a message id → `{"ok": true, "message_id": ...}`;
    the item stays in `sending` for `--replied` / `--done`.
  - nonzero exit (including signals), timeout, unstartable command,
    undecodable output, or exit 0 without a message id
    (ambiguous) → the item stays in `sending` (send-unknown) and it
    prints `{"ok": false, "stage": "unknown"}`; it is never resent
    automatically.
    Remote acceptance can precede a lost response, exception or signal;
    neither an exit code nor error text proves non-delivery. The sender
    contract has no automatic retryable failure result. Only independently
    verified non-delivery permits the separate, token-checked operator
    `--send-failed` transition.
  - send command not configured → `{"ok": false, "stage":
    "config"}` and the item stays claimed (the configuration check
    happens before the fence marks anything).
- `--sending`: mark the item as mid-send *before* a manual Bot API
  call, so a crash leaves it in `sending` (never auto-resent)
  instead of recyclable. This manual step remains for operator use;
  normal turns use `--send-fenced`. Refused once a reply has been
  recorded for the item.
- `--done` / `--release` / `--replied` / `--sending` /
  `--send-failed`: the claim token is required in practice —
  without the batch's token the command prints `{"ok": false}` and
  changes nothing.
- `--done`: the optional reason records why an item was closed
  (stored as `done_reason`). Closing a `sending` item with no
  reply record additionally requires the evidence argument — a
  reconciliation reference, stored as `done_evidence`; a reason
  alone is refused there.
- `--replied`: record that a reply went out, after the send and
  *before* `--done`.
- `--release`: return an unreplied claimed item to pending. Refused
  for replied or `sending` items.
- `--send-failed`: return a `sending` item to pending — only when
  the Bot API definitively rejected the send. Refused once a reply
  has been recorded for the item.
- `--adopt`: one-time owner bootstrap (see Setup).

### Environment variables

| Variable | Meaning | Default |
| --- | --- | --- |
| `TELEGRAM_BRIDGE_STATE_PATH` | State file location. Deployments always set this so state stays outside the repo. | `state.json` beside `bridge_core/telegram.py` |
| `TELEGRAM_BRIDGE_BATCH_SIZE` | Max items claimed per wake. | `5` |
| `TELEGRAM_BRIDGE_STALE_TTL` | Seconds before an unreplied claim may be recycled. | `1800` |
| `TELEGRAM_BRIDGE_SEND_CMD` | Path to the deployment's send executable, used by `--send-fenced`. Invoked with argv `[chat id]` and the reply text on stdin; on success it must print the platform message id as the last stdout line. Unset disables the fenced send (stage `config`). | unset |

### Wake payload

```json
{
  "claim_token": "<hex token for this batch>",
  "count": 2,
  "items": [
    {"update_id": 101, "chat_id": "<chat id>", "chat_type": "private",
     "sender_name": "@owner", "is_bot": false, "text": "…"},
    {"update_id": 102, "chat_id": "<chat id>", "chat_type": "group",
     "sender_name": "@agent", "is_bot": true, "text": "…",
     "context": [{"sender_name": "@owner", "is_bot": false, "text": "…", "date": 1700000102}]}
  ],
  "update_id": 101, "chat_id": "<chat id>", "…": "first item's fields, repeated at top level for compatibility"
}
```

Group/supergroup items carry `context` (the tail of a rolling buffer
of recent group messages); private items do not. The first item's
fields are mirrored at the top level so consumers written for the old
single-item payload keep working. All items in one payload belong to
a single conversation (one chat) — claims are lane-scoped, so a turn
never has to mix conversations.

Only the owner's private DMs are served. In groups, only the one
designated group (see Setup) is served; its messages — the owner's and
other bots' alike — are queued with the rolling context buffer.

## Setup from scratch (Telegram deployment)

1. **Create the bot** in BotFather and keep the token for step 2 only.
   - To receive other bots' posts in a group, enable **Bot-to-Bot
     Communication Mode** for the bot in BotFather.
   - Add the bot to the group and promote it to admin (or disable
     privacy mode), so it receives all group messages.
2. **Register the credential** in Muse's secure connection as
   provider `custom.telegram-muse-bot` (API key placed in the URL path
   segment `/bot{}` on `api.telegram.org`). The adapter never sees the
   raw token: it builds API URLs with a short-lived surrogate that the
   egress layer swaps for the real token. Never put the token in the
   repository, a state file, or a chat.
3. **Wire the wake hook** to run `python3 -m bridge_core.telegram`
   with `TELEGRAM_BRIDGE_STATE_PATH` pointing at a state file outside
   the repository, in a context where the surrogate resolves.
4. **Adopt the owner:** have the owner send the bot any DM. The sender
   is recorded as a candidate; then run, once, from the operator side:
   `python3 -m bridge_core.telegram --adopt <owner user id>`.
   From then on, only that owner's DMs are served.
5. **Adopt the group:** the first group where the owner sends a text
   message becomes the designated group automatically. Other groups
   the bot joins are only recorded as candidates.

## Tests

```sh
python3 -m unittest discover -s tests
```

Runs the full offline suite (no network, no credentials). What the
bridge-core tests prove:

- `tests/test_bridge_core.py` — store transactions persist and
  snapshot; a corrupt state file, or a missing main file with a
  snapshot present, raises instead of silently rolling back, a
  fresh deployment still starts empty, and a snapshot write failure
  surfaces; a failed transaction saves nothing; queue dedupe by
  `(platform, external_id)`; batch claim takes the N oldest first
  of one conversation lane for the requested platform; a second
  claim on the same lane is blocked while a batch is in flight,
  even after partial completion, while other lanes and platforms
  stay claimable; wrong-token transitions are no-ops; the sending
  lifecycle (a `sending` item is never recycled and blocks new
  claims on its lane, `--send-failed` is the only way back and is
  refused for replied items, `mark_sending` is likewise refused for
  replied items, a recycled batch rejects the stale turn's token,
  and closing a send-unknown item requires reconciliation evidence
  — a reason alone is refused, and evidence is stored separately
  from the reason); release refused for replied and
  sending items; `mark_replied` requires the claim token; stale
  recycle returns only unreplied claims (a replied claim is never
  recycled and keeps blocking until closed); and a concurrency test
  with 6 parallel processes over 30 items finishes with an intact
  state file and no item claimed twice.
- `tests/test_telegram_adapter.py` — the adapter driven through its
  real CLI with a stubbed fetch: wake payload shape and legacy
  top-level fields, one lane per wake; group context attached, DM
  context absent; group adoption via the owner's first group
  message; an in-flight batch blocks the next wake; the
  token-checked send lifecycle (`--sending` → `--replied` →
  `--done`, `--send-failed`, release of a replied item refused,
  tokenless `--replied` rejected); a redelivered update is deduped
  across polls; closing a send-unknown item through the CLI
  requires the evidence argument; and the send fence driven with a
  fake send executable (fence refusals never invoke it; the happy
  path marks `sending`, sends exactly once and returns the message
  id; nonzero exits, acceptance followed by crash, response loss,
  signal, missing/undecodable output, unstartable command and timeout
  retain the `sending` reservation; unknown sends remain unrecycled
  beyond TTL, block their lane and refuse release, cancellation without
  evidence and repeated sends; verified non-delivery can still be
  returned to pending through token-checked `--send-failed`; a missing send
  command leaves the item claimed).

## Migration from the legacy poll.py

The legacy standalone `poll.py` kept the same top-level state keys
(`offset`, `owner_id`, `group_id`, `group_buffer`, `candidates`,
`candidate_groups`), but its queue items were keyed by `update_id` and
had no claim-token or reply fields. To move a deployment onto the
core:

1. Stop the wake hook.
2. **Back up the legacy state file and keep the backup** — it is the
   rollback source (step "Rollback" below).
3. Convert the state in place: copy the top-level keys unchanged; map
   each queue item to the core shape — `platform: "telegram"`,
   `external_id: str(update_id)`, carry over chat/sender/text/date and
   status fields, claim and sending fields empty, reply fields null,
   and a
   `received_at` (legacy items only have the platform message date).
   Carrying only still-active items is fine; done history need not
   migrate.
4. Point the hook at `bridge_core.telegram` with
   `TELEGRAM_BRIDGE_STATE_PATH` set to the same state file.
5. Resume the hook and watch the first polls: updates continue from
   the preserved `offset`, so nothing is re-fetched or lost.

### Rollback

1. Stop the wake hook.
2. Restore the pre-migration backup over the state file. Do **not**
   point the legacy `poll.py` at a core-format state file — its queue
   items lack the `update_id` key it expects.
3. Point the hook back at the legacy `poll.py` and resume it.
