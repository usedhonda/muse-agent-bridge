# Shared room polling: implementation and remaining coverage

The poller and Socket listener use `bridge.intake.normalize_event` for the same
audience/sender gate, bot-self rejection and private/unshared exact-membership
check. They produce the same room routing and bot identity fields, with distinct
transport provenance. Missing-metadata owner records remain ineligible; there is
no owner bypass in the worker. Polling does not require the Socket process to
receive supported history messages, but neither transport starts a model executor.

The source candidate reviewed was [605f8d7](https://github.com/usedhonda/muse-slack-bridge/commit/605f8d7103ab26228e1d2e1f12463ae1051959c9),
based on `f1065b9`. Its room polling idea was integrated through the existing
shared gate rather than taking its weaker owner-record exception or membership-only
check. No unrelated launcher credential fallback is included. Offline evidence
is separate from a deployed runtime or a peer's reported test count.

## Admission and credential adapter

`PollWebClient` reuses the existing `SlackWebClient.auth_test` and
`private_room_matches` implementations. The runtime-specific surrogate adapter
remains the only polling credential route. GET and POST both lazily import the
existing helper; importing the module, offline tests and `--help` do not load
actual credentials. No token file fallback or new credential registration is added.

Bot identity is authenticated each pass. Each eligible room record rechecks
private/unshared properties and the exact owner + configured agents + bot
membership before saving its body. API failure stops the channel before its
checkpoint advances. A definite room mismatch drops the body, without expanding
membership. Normal channels follow the same mention policy as the listener;
configured rooms accept natural authorized messages without a mention. Sender,
bot/subtype and timestamps come from Slack history, not agent assertions.

A root that names itself as `thread_ts` remains top-level unless it has replies;
a true existing-thread message keeps its exact thread destination. This shared
normalization is tested for both transports. **History roots alone do not supply
all thread replies.** Separate `conversations.replies` intake/backfill is not
implemented or live verified here. Do not promise complete thread recovery.

## Pagination and checkpoints

History requests include the stored exact timestamp boundary. The last completed
boundary is skipped, not reinterpreted as a legacy rescue. Later messages are
ordered with Decimal, without float rounding or lexical timestamp comparison.
All pages for a channel must complete before any of that channel's records are
queued or its watermark advances. Repeated page boundaries use stable
`slack:CHANNEL:TS` IDs; overlap with Socket uses existing `(channel, ts)` dedupe
and preserves the first durable ID. All cooperating writers must share one host
and state directory. Different state copies/hosts are not coordinated.

A cursor loop, missing cursor with `has_more`, invalid timestamp, API failure or
100-page bound stops that channel with its old checkpoint intact. The bounded
range stays in memory; rejected bodies are not written to state/logs. A repeated
100-page stop needs an operator-reviewed bounded backfill plan, not a watermark
jump or deletion. A failed append/checkpoint can leave durable queued records;
retrying from the old watermark deduplicates them rather than creating new work.

A dedicated poller checkpoint lock serializes passes and reloads saved state
inside that lock. It is separate from the queue append lock, so polling HTTP does
not block Socket appends. Checkpoints use a flushed same-directory temporary
file and atomic replacement. Stop older writers before adopting; they do not
honor this protocol. Preserve the checkpoint file and its lock with queue state;
do not delete either lock while processes are running.

## Acceptance and cutover limits

Offline cases cover poll-first/Socket-first × owner/agent × root/thread, exact
routing and one accepted identity/claim/send, room API/membership failures with
no body retention, public/shared/archived/group-DM rejection, bot/author gates,
member changes within a pass, complete/failed pages, cursor faults, exact numeric
boundaries, interrupted append/checkpoint and concurrent poller instances.
The reply path still requires a fresh exact-claim context receipt and preserves
uncertain sends. GET/POST tests use substituted helpers and HTTP mocks only.

No live transport was switched and no old payload was repaired/replayed. Preserve
existing watermarks for new incoming processing. An absent/zero watermark can
ingest history; establish a reviewed cutover boundary before starting rather
than resetting state. Existing legacy rows still shadow redelivery of the same
message identity; use the separate metadata-only rescue planner and latest
conversation/delivery readback to design any deliberate recovery. A new intake
fix is not proof those older requests were answered or completed.

Before any installation, independently match the current native conversation
and intended target runtime; prior SSH success or matching HEAD is insufficient.
Lifecycle, credential eligibility, API coverage and actual executor adoption
remain live acceptance work. This patch performs no service, credential, key,
permission, network, queue-apply or deployment operation.
