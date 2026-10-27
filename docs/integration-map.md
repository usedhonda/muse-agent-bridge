# Integration map: `bridge/` (Slack) and `bridge_core/`

This branch (`integration/core-pr4-20261025`) is a **union merge** of the
Slack-side PR #4 line (`bridge/`: listener, poller, worker, recovery) and
the platform-common core line (`bridge_core/`: store, queue, Telegram
adapter). Both packages are present in full and all tests from both lines
pass together (155 tests: 116 Slack-side + 102 core-side − 64 shared
base tests, plus 1 lane-fix regression test).

It is **not yet a unification**. No code is shared between the two
packages: `bridge/` still runs its own store and queue, and
`bridge_core/` still runs its own. This document maps where the two
implementations overlap, where their semantics agree or differ, and the
order proposed for actually unifying them.

**Provenance:** locally, this branch is a git merge — the merge commit
has both lines as parents. The GitHub remote branch of the same name is
a content-identical union replica of that local merge (the same file
tree, uploaded file by file) with a different commit structure and
ancestry; it is not the local merge commit itself.

## Overlap 1: state storage

| | `bridge/store.py` — `EventStore` | `bridge_core/store.py` — `JsonStateStore` |
|---|---|---|
| Layout | A state directory: append-only `queue.jsonl` + `seen_ids.txt` (+ `append.lock`) | One JSON document + `<name>.lock` + `<name>.good` snapshot |
| Write model | Append one fsync'd line per accepted event; seen-ID file appended in the same locked section | Whole-document rewrite per transaction: per-process temp file, fsync, atomic rename |
| Locking | `flock` on `append.lock`; lock order documented as queue lock first, then worker SQLite | `flock` on the lock file serializes the entire load → modify → save cycle |
| Corruption posture | Refuses to append past an incomplete queue tail; rebuilds the seen set from the queue on load, closing the crash window between the queue fsync and the seen-ID fsync | Refuses to load at all: corrupt main file, or main file missing while a snapshot exists, raises `StateStoreError`. The `.good` snapshot is forensic evidence for a deliberate operator restore, never an automatic fallback |
| Extra state | The worker keeps a second store (`worker.sqlite3`) for jobs, claims and context receipts | Adapter-specific keys (offset, buffers) live in the same JSON document as the queue |

Both sides fail loud rather than roll state back in time — the
philosophy matches, the mechanics do not. A deployment today runs both
engines side by side (one per platform), with incompatible formats.

## Overlap 2: claim and TTL handling

| | `bridge/worker.py` — `Worker` (Slack) | `bridge_core/queue.py` (core) |
|---|---|---|
| Claim unit | One event per claim, inserted into the SQLite `jobs` table | A batch of up to 5 items from one conversation lane, in the JSON state |
| Lane key | `(scope, audience_id, channel, conversation_ts)` — one active job per conversation; later events wait | `(platform, chat_id)` — one batch in flight per lane. Lane selection skips busy lanes, so a free lane stays claimable even when the oldest pending item sits in a busy lane; a lane is blocked only by its own in-flight batch |
| Claim credential | The claim row itself plus a digest of record + `claimed_at`; context refresh and reply verify the digest is unchanged | One random token per batch; every transition (`mark_sending`, `mark_replied`, `mark_done`, `release`, `mark_send_failed`) requires the matching token |
| TTL | `CLAIM_TTL_SECONDS = 900` is **diagnostic only**. A claim is never reclaimed by age; `status` reports `stale_claimed` and an operator must `resume`, `release` (confirmed-not-started only), or `reconcile` deliberately | `DEFAULT_STALE_TTL = 1800` **does** recycle: an unreplied `claimed` item older than the TTL returns to pending and its old token stops matching, so a stale turn's late calls are rejected |
| Send discipline | `reply` first persists status `sending` (with the reply text), posts outside the locks, then marks `done`. A fresh (60 s) context receipt from an exact-claim refresh is required before posting. If the outcome is uncertain the job stays `sending` until an operator reconciles it (`--sent-ts` / `--confirmed-not-sent`) | The adapter's send fence persists `mark_sending` before the platform call and records the outcome (`mark_replied` / `mark_send_failed` on definitive rejection). A fenced send leaves the item in `sending`; the runtime `--replied` → `--done` step is a handover condition, since send success alone does not close the item. `sending` items are never recycled; closing one without a reply record requires reconciliation `evidence` |
| Duplicate-send guard | `reply` refuses if another job with the same `(channel, ts)` is already `sending` or `done` | An item with `replied_at` set can never be claimed, released, send-failed, or recycled again |

The shared philosophy: an uncertain send outcome is an operator
decision, never an automatic retry. The sharpest real difference is
TTL semantics — Slack never auto-recycles a claim, the core does
recycle. The core's recycle is guarded by token invalidation: every
state transition in `bridge_core` checks the claim token, and the send
fence persists `sending` before the platform call, so a stale turn's
late transition calls are rejected. That guarantee is narrow in
scope — it covers only the token-checked state transitions and the
send fence inside `bridge_core`. It does not by itself stop an old
model executor from running, or prevent double execution of external
operations outside those transitions. Treat the TTL difference as a
per-platform policy to be made explicit, not as a bug on either side.

## Overlap 3: intake dedupe

| | `bridge/` (Slack) | `bridge_core/` (core) |
|---|---|---|
| Where | `bridge/intake.py` (`normalize_event`) admits/normalizes first — rejected events are never written — then `EventStore.append` dedupes | The adapter (`bridge_core/telegram.py`) filters during ingest, then `queue.enqueue` dedupes |
| Primary key | `event_id`, persisted in `seen_ids.txt` and rebuilt from the queue after a crash | `(platform, external_id)` within the single state document; `enqueue` returns `None` on a duplicate |
| Second layer | The same message arriving via Socket Mode and via the history poller carries different event IDs, so `(channel, ts)` is also deduped when both records name a transport (handover duplicate) | None: the Telegram adapter has a single update stream, so `(platform, external_id)` is sufficient today |

The Slack cross-transport message key is the one dedupe concept the
core cannot yet express; any unified identity model has to carry it.

## What this branch does not unify

- Storage engines, state formats, and lock files (see Overlap 1).
- Claim/turn semantics and TTL policy (see Overlap 2).
- Configuration: `bridge/` loads YAML config with an audience model
  (`bridge/config.py`, `bridge/audience.py`); core adapters take
  environment variables and keep bookkeeping in their state document.
  There is no shared config model.
- Recovery tooling: `bridge/recovery.py` (metadata-only rescue
  planning) works on the Slack stores only; the core's recovery story
  is the `.good` snapshot plus operator reconciliation in the queue.
- The Slack listener/poller/worker do not call the core, and the
  Telegram adapter does not call `bridge/`. The only thing the two
  packages currently share is this repository and its test command.

## Proposed unification order

1. **Identity and dedupe contract first.** Extend the core item
   identity with an optional platform message key (the Slack
   `(channel, ts)` analogue) and align what `normalize_event` emits
   with what `enqueue` stores. This is pure semantics — no live state
   changes — and both suites can pin the behavior. It goes first
   because identity underpins both stores and both queues; changing
   it later would force a state migration on top of everything else.
2. **Storage second.** Converge on one transactional state engine per
   deployment, preserving both non-negotiables: the Slack side's
   torn-tail refusal and crash-window rebuild, and the core's
   fail-loud, never-auto-rollback snapshot policy. This goes before
   claim unification because both claim implementations need a single
   transactional home, and the core claim functions already assume
   one (`load → modify → save` under one lock).
3. **Claim and turn semantics last.** Express the Slack worker's
   discipline in core terms: batch size 1, lane = conversation key,
   stale-recycle as an explicit per-platform policy (off for Slack,
   on for Telegram), and the context-receipt gate as an adapter-level
   precondition next to the send fence (both already persist
   "sending" before posting — the patterns should converge there).
   This is last because it changes live turn behavior, depends on
   steps 1–2, and is where the Slack-side review (PR #4) has the
   most to teach the core.

Until those steps land, keep both implementations intact: neither
side's tests are a subset of the other's, and each encodes recovery
behavior the other has not needed yet.
