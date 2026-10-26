"""Platform-neutral intake queue with batch-claim discipline.

The queue lives in the ``"queue"`` list of the shared state document
(see ``store.JsonStateStore``); every function here mutates that state
in place and is meant to be called inside ``store.transaction()`` so
the whole load -> modify -> save cycle is serialized.

Claim discipline (the rules that keep wake turns from double-answering):

- At most ONE batch is in flight PER conversation lane. While an
  item of a lane is ``claimed`` or ``sending`` and not stale,
  ``claim_batch`` will not claim that lane again — but other lanes
  (and other platforms) stay claimable, so one conversation's
  backlog never stalls the rest.
- Each claim issues one token shared by the batch; every transition
  (``mark_sending``, ``mark_replied``, ``mark_done``, ``release``,
  ``mark_send_failed``) only applies when the caller's token matches
  the item's current claim.
- Sending is an explicit lifecycle, not a side effect: a turn moves an
  item to ``sending`` (``mark_sending``) BEFORE calling the platform
  send API, then records the outcome — ``mark_replied`` on success,
  ``mark_send_failed`` only when the platform definitively rejected
  the send. ``sending`` items are NEVER recycled: the send may or may
  not have been delivered, so they wait for an operator to reconcile
  them rather than risking an automatic duplicate.
- An item with a recorded reply (``replied_at`` set) is NEVER recycled
  and can never be released or send-failed back to pending, however
  old its claim gets: the reply already went out, so re-waking it
  could only produce a duplicate. ``claim_batch`` never selects such
  an item either, even if it somehow sits in ``pending`` (legacy or
  hand-edited state); it is left for operator reconciliation.
- Closing a ``sending`` item that has no recorded reply requires
  reconciliation ``evidence`` in ``mark_done`` — a reference to how
  the send outcome was established (the reply found in platform
  history, or a definitive non-delivery confirmation). A ``reason``
  alone never closes such an item: the outcome is unknown, so the
  close must cite evidence, stored as ``done_evidence`` (kept
  separate from the optional ``done_reason`` context).
- Stale recycle applies only to unreplied ``claimed`` items older than
  the TTL (default 1800s): a wake turn that died before sending must
  not block the queue forever, but a slow turn is never raced by a
  second claim. After a recycle the old batch token no longer matches,
  so the stale turn's late transition calls are all rejected.
- A batch is a single conversation lane: ``claim_batch`` takes items
  of one platform and one chat only (the lane of the oldest pending
  item for the requested platform), so a turn never mixes platforms
  or conversations.

Every item records ``received_at``, ``claimed_at``, ``sending_at``
and ``replied_at`` so intake latency can be measured end to end.
"""
from __future__ import annotations

import secrets
import time

DEFAULT_BATCH_SIZE = 5
DEFAULT_STALE_TTL = 1800.0

PENDING = "pending"
CLAIMED = "claimed"
SENDING = "sending"
DONE = "done"

#: Statuses that keep a batch in flight (block a further claim on
#: the same conversation lane).
IN_FLIGHT = (CLAIMED, SENDING)


def ensure_state(state: dict) -> dict:
    state.setdefault("queue", [])
    return state


def _now(now: float | None) -> float:
    return time.time() if now is None else now


def _key(platform: str, external_id) -> tuple[str, str]:
    return (platform, str(external_id))


def find_item(state: dict, platform: str, external_id) -> dict | None:
    key = _key(platform, external_id)
    for item in state.get("queue", []):
        if _key(item["platform"], item["external_id"]) == key:
            return item
    return None


def enqueue(
    state: dict,
    *,
    platform: str,
    external_id,
    chat_id=None,
    chat_type: str | None = None,
    sender_id=None,
    sender_name: str | None = None,
    is_bot: bool = False,
    text: str = "",
    date=None,
    now: float | None = None,
) -> dict | None:
    """Append an item unless (platform, external_id) is already known.

    Returns the new item, or ``None`` when it was a duplicate delivery.
    """
    ensure_state(state)
    if find_item(state, platform, external_id) is not None:
        return None
    item = {
        "platform": platform,
        "external_id": str(external_id),
        "chat_id": chat_id,
        "chat_type": chat_type,
        "sender_id": sender_id,
        "sender_name": sender_name,
        "is_bot": bool(is_bot),
        "text": text,
        "date": date,
        "received_at": _now(now),
        "status": PENDING,
        "claimed_at": None,
        "claim_token": None,
        "sending_at": None,
        "replied_at": None,
        "reply_message_id": None,
        "done_at": None,
    }
    state["queue"].append(item)
    return item


def _recycle_stale(state: dict, stale_ttl: float, now: float) -> None:
    # Only CLAIMED items are candidates. SENDING items are excluded by
    # construction: their send outcome is unknown, so they must never
    # be recycled into a possible duplicate send. Replied items are
    # excluded too — the reply already went out.
    for item in state["queue"]:
        if (
            item["status"] == CLAIMED
            and item["replied_at"] is None
            and item["claimed_at"] is not None
            and now - item["claimed_at"] > stale_ttl
        ):
            item["status"] = PENDING
            item["claimed_at"] = None
            item["claim_token"] = None


def claim_batch(
    state: dict,
    *,
    platform: str | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    stale_ttl: float = DEFAULT_STALE_TTL,
    now: float | None = None,
) -> dict | None:
    """Claim up to ``batch_size`` pending items as one batch.

    The batch is a single conversation lane: when ``platform`` is
    given, only that platform's items are eligible. Eligible items
    are considered in received order, and the batch takes the lane
    of the FIRST eligible item whose (platform, chat_id) lane has
    nothing in flight; up to ``batch_size`` items of that lane are
    claimed, in received order.

    The in-flight guard is scoped per lane: a lane is blocked only
    by its OWN in-flight batch (an item ``claimed`` or ``sending``
    in the same lane, not recycled as stale). Busy lanes are
    skipped during selection instead of blocking the whole claim,
    so a newer item in a free lane is claimed even when the oldest
    pending item sits in a busy lane.

    Items with a recorded reply (``replied_at`` set) are never
    eligible, even if one somehow sits in ``pending``: its reply
    already went out, so claiming it could only duplicate the
    answer. It is left for operator reconciliation.

    Returns ``{"claim_token": str, "items": [item, ...]}`` or
    ``None`` when there is nothing eligible to claim or every
    eligible item's lane still has a batch in flight.
    """
    ensure_state(state)
    now = _now(now)
    _recycle_stale(state, stale_ttl, now)
    pending = [
        item
        for item in state["queue"]
        if item["status"] == PENDING
        and item["replied_at"] is None
        and (platform is None or item["platform"] == platform)
    ]
    pending.sort(key=lambda item: item["received_at"])
    if not pending:
        return None
    busy_lanes = {
        (item["platform"], item["chat_id"])
        for item in state["queue"]
        if item["status"] in IN_FLIGHT
    }
    lane = None
    for item in pending:
        candidate = (item["platform"], item["chat_id"])
        if candidate not in busy_lanes:
            lane = candidate
            break
    if lane is None:
        return None
    batch = [
        item
        for item in pending
        if (item["platform"], item["chat_id"]) == lane
    ][:batch_size]
    token = secrets.token_hex(8)
    for item in batch:
        item["status"] = CLAIMED
        item["claimed_at"] = now
        item["claim_token"] = token
    return {"claim_token": token, "items": batch}


def _owned_item(
    state: dict, platform: str, external_id, token, statuses
) -> dict | None:
    """The item, iff it exists, is in one of ``statuses``, and the
    caller's token matches the item's current claim token.

    The token is always required: a missing or stale token (e.g. from
    a batch that has since been recycled) matches nothing, so a stale
    turn can never move an item it no longer owns.
    """
    item = find_item(state, platform, external_id)
    if item is None or item["status"] not in statuses:
        return None
    if token is None or item["claim_token"] != token:
        return None
    return item


def mark_sending(
    state: dict, platform: str, external_id, token, *, now: float | None = None
) -> bool:
    """Move a claimed item to ``sending`` before the platform send.

    From here the item is never recycled: if the turn dies, the send
    outcome is unknown and an operator reconciles the item instead of
    the queue risking an automatic duplicate.

    Refused when a reply is recorded (``replied_at`` set): a claimed
    item stays ``claimed`` until ``mark_done`` even after its reply
    is recorded, and moving it into the send lifecycle would expose
    an already-answered item to a second send. Same guard as
    ``mark_send_failed``.
    """
    item = _owned_item(state, platform, external_id, token, (CLAIMED,))
    if item is None:
        return False
    if item["replied_at"] is not None:
        return False
    item["status"] = SENDING
    item["sending_at"] = _now(now)
    return True


def mark_send_failed(
    state: dict, platform: str, external_id, token, *, now: float | None = None
) -> bool:
    """Return a ``sending`` item to pending after a DEFINITIVE send
    failure (the platform API said the send did not go through).

    This is the only way back from ``sending`` besides ``mark_done``;
    it must never be used when the outcome is merely unknown.

    Refused when a reply is recorded (``replied_at`` set): the item
    sits in ``sending`` until ``mark_done`` even after a successful
    send, and bouncing it back to pending would let a later claim
    answer it a second time.
    """
    item = _owned_item(state, platform, external_id, token, (SENDING,))
    if item is None:
        return False
    if item["replied_at"] is not None:
        return False
    item["status"] = PENDING
    item["claimed_at"] = None
    item["claim_token"] = None
    item["sending_at"] = None
    return True


def mark_done(
    state: dict,
    platform: str,
    external_id,
    token,
    *,
    now: float | None = None,
    reason: str | None = None,
    evidence: str | None = None,
) -> bool:
    """Close a claimed or sending item. A wrong token is a no-op.

    ``reason`` records WHY the item was closed (e.g. "superseded",
    "ack-only"); audit trails use it to tell answered items from
    silently closed ones. It is optional context everywhere.

    Closing a ``sending`` item with no reply record is stricter:
    that item's send outcome is unknown, so the close must cite
    reconciliation ``evidence`` — a non-empty reference to how the
    outcome was established (e.g. the platform message id found in
    history, or a definitive non-delivery confirmation). A reason
    alone does NOT suffice there; without evidence the call returns
    False and changes nothing. Given evidence is stored as
    ``done_evidence``, separate from ``done_reason``. ``claimed``
    items (never sent) and items with a recorded reply need no
    evidence, as before.
    """
    item = _owned_item(state, platform, external_id, token, IN_FLIGHT)
    if item is None:
        return False
    if (
        item["status"] == SENDING
        and item["replied_at"] is None
        and not evidence
    ):
        return False
    item["status"] = DONE
    item["done_at"] = _now(now)
    item["claim_token"] = None
    if reason:
        item["done_reason"] = reason
    if evidence:
        item["done_evidence"] = evidence
    return True


def release(
    state: dict, platform: str, external_id, token
) -> bool:
    """Return a claimed item to pending (the turn could not handle it).

    Refused for items that were already replied to or are in
    ``sending``: their reply went out (or may have), so putting them
    back in the queue would resurrect them for a duplicate answer.
    """
    item = _owned_item(state, platform, external_id, token, (CLAIMED,))
    if item is None:
        return False
    if item["replied_at"] is not None:
        return False
    item["status"] = PENDING
    item["claimed_at"] = None
    item["claim_token"] = None
    return True


def mark_replied(
    state: dict,
    platform: str,
    external_id,
    message_id,
    token,
    *,
    now: float | None = None,
) -> bool:
    """Record that a reply went out for a claimed or sending item.

    The claim token is required and must match, exactly like
    ``mark_done``: a stale worker that no longer owns the claim cannot
    record a reply. Once recorded, the item is never recycled and
    never released (see module docstring); the turn is expected to
    follow up with ``mark_done``.
    """
    item = _owned_item(state, platform, external_id, token, IN_FLIGHT)
    if item is None:
        return False
    item["replied_at"] = _now(now)
    item["reply_message_id"] = str(message_id)
    return True


def counts(state: dict) -> dict:
    result = {PENDING: 0, CLAIMED: 0, SENDING: 0, DONE: 0}
    for item in state.get("queue", []):
        result[item["status"]] = result.get(item["status"], 0) + 1
    return result
