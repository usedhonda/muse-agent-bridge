# Muse worker integration

The Muse agent is already a running runtime. This bridge does not start a
replacement model or assume an undocumented Muse API. Install this checkout
on the agent VM, where the existing Muse runtime can invoke local commands.
The runtime's wake-hook registration and secure credential injection are
separate deployment steps and must be verified live.

## Wake hook

Configure the existing runtime to poll no faster than every five seconds:

```sh
python3 -m bridge.worker next
```

The result is JSON `null` when there is no new eligible event, otherwise an
object with `event` and `history`. Start one Muse turn only for a non-null
result. `event.event_id` is the durable job key; pass it through the turn.
`history` includes only completed exchanges from the same scope, audience,
channel and conversation. A private agent room uses separate room-level history for top-level messages and separate history for each existing thread. The first release serves only the audience whose
ID and scope are both `owner`; guest entries stay unprocessed pending M4.

After composing the response, send UTF-8 text through stdin:

```sh
python3 -m bridge.worker reply EVENT_ID < .local/reply.txt
```

The command requires `SLACK_BOT_TOKEN` injected through the secure launcher.
The model supplies only the text. The worker pins the destination to the
claimed event's channel and reply location, rechecks current config, and never
broadcasts a reply. Text must be nonempty and no longer than 4,000 characters.
A completed job returns its original Slack timestamp without posting again.
The listener ignores the bot's own events.

## Refresh context before replying

A packet contains the claimed event and completed same-conversation history.
It is not a live view of the queue. `queue_context` adds a metadata-only count
of newer, unfinished, currently eligible messages and the `context` action.
Messages may arrive after that count was taken, even if it was zero. All `next`
and `resume` packets include `queue_context.required_before_reply: true`, the
exact `refresh_event_id` and `refresh_argv: ["context", EVENT_ID]`. These are
mandatory runtime instructions before composing any new reply, including when
the initial count is zero. Use the same approved launcher, config and state as
the claim. The worker CLI does not enforce runtime adoption with a new database
state or send gate. Retrieve the snapshot explicitly:

```sh
# With the bot credential securely injected, as for reply:
python3 -m bridge.worker context EVENT_ID
```

On the supported credential-adapter profile, `sh deploy/run-worker.sh context
EVENT_ID` injects the credential. The result is private conversation content;
keep it in the authorized runtime context, not public logs or issue reports.
The command requires an existing `claimed` job and rechecks current policy.
For agent rooms it authenticates the same bot identity as the original event
and verifies exact current private/unshared membership before returning text.
Lookup failure or changed membership fails closed without changing jobs.

The snapshot labels `read_only: true` and includes at most the **10 latest**
eligible unfinished events in that same conversation, newer by exact Slack
message timestamp. It keeps their chronological order, exact event IDs, authors
(`user`), message/thread timestamps, intake time and source transport, with
`provenance: local_durable_queue` and current job state. Room-level messages,
individual threads, channels, scopes and audiences stay separate. Missing
legacy provenance fields remain null, rather than being inferred. Each text is
limited to 2,000 characters with `text_truncated`; `omitted_older_count` reports
excluded older context when more than 10 events qualify.

These records are context, **not additional claimed work or new authorization**.
Use corrections to avoid a stale response while preserving unresolved original
work. Do not automatically skip, finish, claim or execute those later jobs.
They remain eligible for their own normal handling. Avoid duplicate acknowledgements,
but keep substantive progress updates; this is not a one-reply quota. The original
reply location and existing `sending`/reconciliation protection remain unchanged.
A `sending` or `done` job cannot be refreshed with this action.

This is a local snapshot, not Slack history backfill, and it can become stale
again if another event arrives. It neither drains the FIFO queue nor increases
runtime throughput, starts a missing listener, or recovers Socket outage messages.
The external runtime must call this supported refresh action; an old integration
that ignores it will still compose replies from its original packet.

### Runtime integration checklist

1. Retain the exact claimed packet/event ID and original unresolved work in the
   supported authorized runtime context; a claim is not evidence work completed.
2. Follow that packet's required `refresh_argv` before composing a new reply,
   even when the earlier metadata count was zero. On refresh failure, keep the
   job intact and surface the failure; do not automatically post, skip or replay.
3. Treat returned messages as read-only context, not completed work or extra
   execution authority. Apply relevant corrections while preserving pending work;
   keep substantive updates and suppress only duplicate acknowledgements.
4. Submit through the original event's pinned `reply` path. Keep existing
   sending/reconcile handling; a refresh cannot authorize an uncertain resend.
5. Verify runtime adoption and lifecycle separately with synthetic evidence.
   This repo does not register the adapter or prove main-chat/check/executor
   lifetime, listener availability, credential eligibility or queue throughput.

## Private agent rooms

An owner audience can opt specific rooms into `agentChannels`, mapping each
allowlisted private channel to the exact agent user IDs allowed there. These
IDs do not gain DM access or access to other rooms. The owner, named agents
and Muse must be the complete membership. Intake and send both verify that
the room is private, unshared and has exactly that membership. API failures
stop processing; they do not relax the checks. `groups:read` and
`groups:history` are required on the bot; no user token is used.

In these rooms, natural messages are eligible without a formal mention.
Top-level messages receive top-level replies. Existing `thread_ts` is
preserved; before a top-level reply, the worker checks whether a thread has
since appeared on the original message and uses it if present. The model
cannot choose another destination. DM/channel replies use `replyInThreads` (default `true`); set it to `false`
for top-level replies. Agent-room routing follows the rules above independently.

The packet includes `conversation_policy`. Follow it: agent conversation
must not become new owner authorization or import private DM memory. Skip
empty acknowledgement exchanges while continuing substantive authorized work;
there is no blanket agent-turn quota. Finish a turn needing no reply with:

```sh
python3 -m bridge.worker skip EVENT_ID
```

This releases the conversation without posting. Unknown bots, Muse's own
messages, edits, other users and group DMs remain rejected. Replies always
use the bot credential and identity; user tokens are not accepted for posts.
Slack's Agent experience switch is a presentation setting, not a switch for
bot processing or permission to post as a human.

Before enabling `agentChannels` on an existing runtime, inspect pending jobs.
Legacy queued room records lack membership/routing metadata and are left
unclaimed, not automatically migrated or replayed. Reconcile them deliberately
against actual Slack delivery. Preserve existing runtime edits before any upgrade; reconcile local patches
rather than overwriting a dirty checkout.

The queue is append-only. Claims, replies and delivery states are stored in
`.local/state/worker.sqlite3`. Claims are atomic across worker processes;
later messages in the same thread wait for the earlier turn to complete.
Different threads can advance independently. A failed Muse turn leaves a
claim in place instead of silently starting another turn on the next poll.

## Diagnose the stage

Run without Slack credentials or a network request:

```sh
python3 -m bridge.worker status
python3 -m bridge.worker pending
```

`status` reports counts only, with no message text or sender IDs:

- `queued`: eligible records not yet claimed and not behind an active turn.
- `blocked`: unclaimed records waiting for an unfinished turn in the same conversation.
- `ineligible`: queued records rejected by the current worker policy/config.
- `claimed`: handed to the runtime; this does not prove the runtime is still running.
- `stale_claimed`: a subset of claimed jobs older than 15 minutes (or without a claim timestamp).
- `sending`: delivery outcome is uncertain; check Slack before recovery.
- `done`: worker finished posting or skipping, or delivery was reconciled. This does not prove an external task succeeded.

An empty queue does not prove listener connectivity or Slack delivery. A Socket
Mode envelope ACK confirms durable intake (or deliberate rejection), and an
optional receipt reaction means reception, not execution or completion. A
non-null `next` result is the runtime handoff; the runtime must separately
report execution progress and the task result. `status` does not poll Slack,
start a turn or test the runtime wake hook. It initializes local worker state
if missing, so always use the same `--state` directory as intake.

Both listener and poller must share that state directory during a handover.
Append locking and dedupe cover cooperating processes on one host/local
filesystem; they do not coordinate different hosts or separately copied state.
Configured agent rooms are Socket-only; the poller excludes them before history
reads so an ineligible polling record cannot shadow a verified socket event.
Older incomplete room records are not upgraded or replayed automatically: inspect
`ineligible` counts and preserve those records while comparing actual runtime and
Slack delivery before deliberate operator recovery.
Keep the lock file, queue and seen-ID store together. Do not delete the lock
file while intake runs. Every append refreshes persisted IDs under the lock;
the current append-only store reads the full queue and needs an operator
retention plan for large deployments.

## Recovery

Eligible events without claims are known to be unstarted and continue through
`next` automatically. If the dispatcher receives a claim but can establish that
it failed **before starting any runtime turn** (for example, process creation
failed), it may release that claim and let normal polling retry without human
approval:

```sh
python3 -m bridge.worker release EVENT_ID --confirmed-not-started
```

The flag is an assertion by the dispatcher, not an inference made by the bridge.
A runtime exception, timeout or process exit after starting is insufficient:
external actions may already have happened. Resume that work from recorded
progress, and reconcile uncertain posts separately. Do not connect a blanket
exception handler to `release`. `sending` and completed jobs cannot be released.

Inspect unfinished job metadata with `python3 -m bridge.worker pending`.
`python3 -m bridge.worker resume EVENT_ID` returns an existing claimed packet
for deliberate continuation, not an automatic new turn. Age alone never releases
a claim: a long task may still be running and may already have performed external
actions. Before `resume`, verify the previous runtime stopped, inspect its task
progress, and continue from that progress. `resume` refreshes the diagnostic age.
There is no automatic 15-minute replay. A stale sending job is never resumed;
use delivery reconciliation below.

A post is marked `sending` durably before contacting Slack. A crash, timeout,
API rejection, or lost success response leaves it there and prohibits
resending. Slack and the local database do not share a transaction; this
is duplicate prevention by stopping on uncertainty, not an exactly-once
network-delivery guarantee. Check the actual Slack thread first, then use:

```sh
python3 -m bridge.worker reconcile EVENT_ID --sent-ts MESSAGE_TS
# Only after confirming that the attempted reply did not arrive:
python3 -m bridge.worker reconcile EVENT_ID --confirmed-not-sent
```

The second command allows an explicit `reply` retry; it never sends by itself.
Do not blindly reconcile jobs or repeat a Muse turn that may have performed
external actions. An incomplete queue tail is ignored by readers and blocks
new appends until the operator has recovered it, preserving partial evidence.

## Slack deployment profile

The manifest matches the existing private secretary app configuration:
Agent View and Socket Mode enabled, DM and private-channel bot events,
read-only user scopes for observation, bot scopes for replies, no public
or group-DM bot events, and no distribution activation. The bridge does not
use the user token or implement read-only observation yet. Do not treat an
app description or enabled scope as proof of that behavior.

This profile has no `reactions:write` scope, so `ackReaction: null` disables
receipt reactions. If reactions are wanted later, both the scope and local
configuration must change. DM access is individual; group DMs are dropped.
Keep the owner as the only human principal. Allowlist only approved private
channels; named bot exceptions belong in `agentChannels`, never in `userIds`. Apps can remain visible in a shared workspace's directory;
undistributed does not mean hidden from its members.

## Acceptance

Offline tests cover room membership, agent admission, in-place routing, no-post turns,
intake to pinned reply, self-event prevention, duplicate
claims and restart behavior, scope/thread history isolation, revocation,
uncertain delivery, and queue recovery. Live acceptance still requires the
owner DM/private-channel message to receive a threaded Muse reply and a
non-owner message to leave no queue record, reaction or reply. Do not mark
M1/M2 live-complete before those checks and wake-hook installation succeed.
