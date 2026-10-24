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
Keep the lock file, queue and seen-ID store together. Do not delete the lock
file while intake runs. Every append refreshes persisted IDs under the lock;
the current append-only store reads the full queue and needs an operator
retention plan for large deployments.

## Recovery

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
