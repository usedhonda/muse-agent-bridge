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
channel and root thread. The first release serves only the audience whose
ID and scope are both `owner`; guest entries stay unprocessed pending M4.

After composing the response, send UTF-8 text through stdin:

```sh
python3 -m bridge.worker reply EVENT_ID < .local/reply.txt
```

The command requires `SLACK_BOT_TOKEN` injected through the secure launcher.
The model supplies only the text. The worker pins the destination to the
claimed event's channel and root thread, rechecks current config, and never
broadcasts a reply. Text must be nonempty and no longer than 4,000 characters.
A completed job returns its original Slack timestamp without posting again.
The listener ignores the bot's own events.

The queue is append-only. Claims, replies and delivery states are stored in
`.local/state/worker.sqlite3`. Claims are atomic across worker processes;
later messages in the same thread wait for the earlier turn to complete.
Different threads can advance independently. A failed Muse turn leaves a
claim in place instead of silently starting another turn on the next poll.

## Recovery

Inspect unfinished job metadata with `python3 -m bridge.worker pending`.
`python3 -m bridge.worker resume EVENT_ID` returns an existing claimed packet
for deliberate continuation, not an automatic new turn.

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
Use only the owner's ID in local config and allowlist only an owner-only
private channel. Apps can remain visible in a shared workspace's directory;
undistributed does not mean hidden from its members.

## Acceptance

Offline tests cover intake to pinned reply, bot-loop prevention, duplicate
claims and restart behavior, scope/thread history isolation, revocation,
uncertain delivery, and queue recovery. Live acceptance still requires the
owner DM/private-channel message to receive a threaded Muse reply and a
non-owner message to leave no queue record, reaction or reply. Do not mark
M1/M2 live-complete before those checks and wake-hook installation succeed.
