"""Telegram adapter on the bridge core (successor to the standalone poll.py).

Keeps the operational interface the wake hook drives:

  (no args)   fetch updates once, queue servable messages, claim one
              batch and print ONE wake payload JSON. Prints nothing
              when there is no work.
  --sending ID TOKEN       move a claimed item to "sending" — run this
                           BEFORE the Bot API send for the item, so a
                           crash mid-send leaves the item in "sending"
                           (never auto-resent) instead of recyclable.
                           Refused once a reply has been recorded.
                           (Operator/manual path; the fenced send
                           below is the normal turn path.)
  --send-fenced ID TOKEN CHAT_ID
                           fenced send of one queue reply: the reply
                           text is read from stdin. In one transaction
                           the item is verified (claimed, token matches,
                           no reply recorded) and moved to "sending";
                           only then is the deployment's send command
                           ($TELEGRAM_BRIDGE_SEND_CMD, invoked with
                           argv [chat id] and the text on stdin, and
                           expected to print the platform message id
                           as its last stdout line) executed. Prints
                           {"ok": true, "message_id": ...} on success
                           (item left in "sending" for --replied and
                           --done),
                           {"ok": false, "stage": "unknown"} when the
                           outcome is ambiguous (item stays "sending",
                           never resent), {"ok": false, "stage":
                           "fence"} when the fence refuses (nothing
                           is sent), and {"ok": false, "stage":
                           "config"} when the send command is not
                           configured (item stays claimed), and
                           {"ok": false, "stage": "input"} for empty or
                           oversized reply text, and {"ok": false,
                           "stage": "stale"} when the item is older
                           than the maximum reply age — the fence then
                           closes the item as expired without sending,
                           because the conversation has moved on. A
                           destination other than the chat recorded
                           at intake is refused without sending.
  --done ID [TOKEN] [REASON] [EVIDENCE]
                           mark a claimed or sending item done; the
                           optional REASON (one short word, e.g.
                           "superseded" or "ack-only") records why an
                           item was closed without a reply. Closing a
                           "sending" item with no reply record REQUIRES
                           the EVIDENCE argument (a reconciliation
                           reference); a reason alone is refused
  --release ID [TOKEN]     return a claimed item to pending (refused
                           for replied or sending items)
  --replied ID MESSAGE_ID [TOKEN]
                           record the reply that went out for an item
                           (after the send, before --done; a replied
                           item is never recycled or released, so it
                           can never be answered twice). The token is
                           required in practice: without the batch's
                           token the record is rejected
  --send-failed ID TOKEN   return a "sending" item to pending — ONLY
                           when the Bot API definitively rejected the
                           send (the reply provably did not go out)
  --purge-expired [--older-than SECONDS] [--dry-run]
                           operator purge: run the pending-expiry
                           sweep on demand (the same sweep claiming
                           performs) and print JSON counts plus the
                           expired items' ids. --older-than overrides
                           the group TTL for this run; private (owner
                           DM) items keep their configured TTL —
                           never, by default — so a purge cannot
                           silently drop the owner's own asks.
                           --dry-run reports what would expire and
                           changes nothing
  --adopt ID   bind a positive owner ID once (main agent/operator only).
               The same ID is idempotent; changing an established owner is
               refused and requires deliberate, offline state migration.

Turn protocol per replied item: --send-fenced (marks the item
"sending" and performs the send under the fence), then --replied,
then --done. The manual --sending step remains available for
operator use. Items closed without a reply only need --done.

State lives in one JSON file managed by ``bridge_core.store``: the core
queue plus this adapter's keys (``offset``, ``owner_id``, ``group_id``,
``group_buffer``, ``candidates``, ``candidate_groups``). The state path
is ``$TELEGRAM_BRIDGE_STATE_PATH`` or ``state.json`` beside this file.
Batch size and stale TTL can be tuned with ``$TELEGRAM_BRIDGE_BATCH_SIZE``
and ``$TELEGRAM_BRIDGE_STALE_TTL`` (seconds). The pending-expiry sweep
and the fence's stale guard are tuned with
``$TELEGRAM_BRIDGE_GROUP_PENDING_TTL`` (default 600),
``$TELEGRAM_BRIDGE_PRIVATE_PENDING_TTL`` (unset: owner DMs never
expire) and ``$TELEGRAM_BRIDGE_MAX_REPLY_AGE`` (default 1800);
a negative value disables the limit it tunes.

The wake payload carries every claimed item under ``"items"`` plus the
batch ``claim_token``; for compatibility the first item's fields are
also present at the top level, exactly as the single-item payload did.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from . import queue as queue_core
from .store import JsonStateStore

PLATFORM = "telegram"
CREDENTIAL = "custom.telegram-muse-bot"
BUFFER_MAX = 20
CONTEXT_TAIL = 8

#: Environment variable naming the deployment's send executable for
#: the fenced send (--send-fenced). The executable is invoked with
#: argv [chat_id] and the reply text on stdin, and must print the
#: platform message id as the last stdout line on success.
SEND_CMD_ENV = "TELEGRAM_BRIDGE_SEND_CMD"

#: Seconds the fenced send waits for the send command before the
#: outcome is classified as unknown (the send may have gone out).
SEND_TIMEOUT_SECONDS = 30.0

#: Environment variables tuning the pending-expiry sweep (claim-time
#: and --purge-expired) and the send fence's stale guard, in seconds.
#: The group TTL defaults to queue_core.DEFAULT_GROUP_PENDING_TTL;
#: the private TTL is unset (owner DMs never expire) by default; the
#: maximum reply age defaults to DEFAULT_MAX_REPLY_AGE. A negative
#: value disables the limit it tunes.
GROUP_PENDING_TTL_ENV = "TELEGRAM_BRIDGE_GROUP_PENDING_TTL"
PRIVATE_PENDING_TTL_ENV = "TELEGRAM_BRIDGE_PRIVATE_PENDING_TTL"
MAX_REPLY_AGE_ENV = "TELEGRAM_BRIDGE_MAX_REPLY_AGE"

#: Default maximum age (seconds since intake) at which the send
#: fence still sends a queue reply. Past it, the fence refuses with
#: stage "stale" and closes the item as expired: the conversation
#: has moved on, so the reply would land off-context. Operator posts
#: that do not come from the queue never pass through the fence and
#: are unaffected.
DEFAULT_MAX_REPLY_AGE = 1800.0

HERE = Path(__file__).resolve().parent


def state_path() -> Path:
    override = os.environ.get("TELEGRAM_BRIDGE_STATE_PATH")
    return Path(override) if override else HERE / "state.json"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_seconds(name: str, default):
    """Float seconds from the environment; ``default`` when unset or
    invalid. A negative value resolves to None — the limit it tunes
    is disabled."""
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return None if value < 0 else value


def _expiry_ttls() -> tuple:
    """(group_ttl, private_ttl) for the pending-expiry sweep."""
    return (
        _env_seconds(
            GROUP_PENDING_TTL_ENV, queue_core.DEFAULT_GROUP_PENDING_TTL
        ),
        _env_seconds(PRIVATE_PENDING_TTL_ENV, None),
    )


def _max_reply_age():
    """The send fence's maximum reply age, from the environment."""
    return _env_seconds(MAX_REPLY_AGE_ENV, DEFAULT_MAX_REPLY_AGE)


def api(method: str, params: dict, timeout: float = 20.0) -> dict:
    """Call the Telegram Bot API through Muse's credential surrogate.

    Same mechanism the live poll.py uses: the surrogate path segment is
    swapped for the real token by the egress layer, so the token itself
    never exists on this machine and is never printed.
    """
    sys.path.insert(0, "/opt/muse/skills/custom-connector-setup/bin")
    from dynamic_credentials import dynamic_credential_entry, read_json_response

    entry = dynamic_credential_entry(CREDENTIAL)
    surrogate = str(entry["surrogate"]).strip()
    url = f"https://api.telegram.org/bot{surrogate}/{method}"
    request = urllib.request.Request(
        url,
        data=json.dumps(params).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return read_json_response(response)


def fetch_updates(offset: int) -> dict:
    return api(
        "getUpdates",
        {
            "offset": offset,
            "timeout": 0,
            "allowed_updates": ["message", "my_chat_member"],
        },
    )


def sender_label(user: dict) -> str:
    if user.get("username"):
        return "@" + user["username"]
    name = " ".join(p for p in [user.get("first_name"), user.get("last_name")] if p)
    return name or str(user.get("id"))


def _ensure_adapter_state(state: dict) -> dict:
    queue_core.ensure_state(state)
    state.setdefault("offset", 0)
    state.setdefault("owner_id", None)
    state.setdefault("group_id", None)
    state.setdefault("candidates", [])
    state.setdefault("group_buffer", [])
    state.setdefault("candidate_groups", [])
    return state


def ingest(state: dict, result: dict) -> None:
    """Fold one getUpdates response into the state (queue + buffers)."""
    if not result.get("ok"):
        raise RuntimeError(f"getUpdates failed: {result.get('description')}")
    _ensure_adapter_state(state)
    for update in result.get("result", []):
        state["offset"] = max(state["offset"], update["update_id"] + 1)

        member = update.get("my_chat_member")
        if member:
            chat = member.get("chat") or {}
            if chat.get("type") in ("group", "supergroup"):
                if chat["id"] not in state["candidate_groups"]:
                    state["candidate_groups"].append(chat["id"])
            continue

        message = update.get("message") or {}
        sender = message.get("from") or {}
        sender_id = sender.get("id")
        chat = message.get("chat") or {}
        chat_type = chat.get("type")
        text = message.get("text")
        if sender_id is None or not text:
            continue

        if state["owner_id"] is None:
            if sender_id not in state["candidates"]:
                state["candidates"].append(sender_id)
            continue

        if chat_type == "private":
            if sender_id == state["owner_id"]:
                queue_core.enqueue(
                    state,
                    platform=PLATFORM,
                    external_id=update["update_id"],
                    chat_id=chat["id"],
                    chat_type="private",
                    sender_id=sender_id,
                    sender_name=sender_label(sender),
                    is_bot=bool(sender.get("is_bot")),
                    text=text,
                    date=message.get("date"),
                )
            continue

        if chat_type in ("group", "supergroup"):
            # Adopt the first group where the owner speaks.
            if state["group_id"] is None and sender_id == state["owner_id"]:
                state["group_id"] = chat["id"]
            if chat["id"] != state["group_id"]:
                continue
            state["group_buffer"].append(
                {
                    "sender_name": sender_label(sender),
                    "is_bot": bool(sender.get("is_bot")),
                    "text": text,
                    "date": message.get("date"),
                }
            )
            state["group_buffer"] = state["group_buffer"][-BUFFER_MAX:]
            queue_core.enqueue(
                state,
                platform=PLATFORM,
                external_id=update["update_id"],
                chat_id=chat["id"],
                chat_type=chat_type,
                sender_id=sender_id,
                sender_name=sender_label(sender),
                is_bot=bool(sender.get("is_bot")),
                text=text,
                date=message.get("date"),
            )


def _payload_item(state: dict, item: dict) -> dict:
    view = {
        "update_id": int(item["external_id"]),
        "chat_id": item["chat_id"],
        "chat_type": item["chat_type"],
        "sender_name": item["sender_name"],
        "is_bot": item["is_bot"],
        "text": item["text"],
    }
    if item["chat_type"] in ("group", "supergroup"):
        view["context"] = state["group_buffer"][-CONTEXT_TAIL:]
    return view


def build_payload(state: dict, batch: dict) -> dict:
    # The batch must contain only Telegram items of one chat lane
    # (claim_batch is called with platform=PLATFORM). Refuse loudly
    # rather than shaping a foreign item into a Telegram payload —
    # raising inside the transaction leaves every item untouched.
    for item in batch["items"]:
        if item["platform"] != PLATFORM:
            raise ValueError(
                f"refusing to shape a {item['platform']!r} item "
                "into a Telegram payload"
            )
    items = [_payload_item(state, item) for item in batch["items"]]
    payload = {
        "claim_token": batch["claim_token"],
        "count": len(items),
        "items": items,
    }
    # Compatibility: the first item's fields at the top level, as the
    # old single-item payload presented them.
    payload.update(items[0])
    return payload


def run_once(store: JsonStateStore, fetch=fetch_updates) -> dict | None:
    """Fetch, ingest, claim one batch; return the wake payload or None."""
    snapshot = _ensure_adapter_state(store.load())
    result = fetch(snapshot["offset"])
    if not result.get("ok"):
        raise RuntimeError(f"getUpdates failed: {result.get('description')}")
    batch_size = _env_int("TELEGRAM_BRIDGE_BATCH_SIZE", queue_core.DEFAULT_BATCH_SIZE)
    stale_ttl = float(_env_int("TELEGRAM_BRIDGE_STALE_TTL", int(queue_core.DEFAULT_STALE_TTL)))
    group_ttl, private_ttl = _expiry_ttls()
    with store.transaction() as state:
        _ensure_adapter_state(state)
        ingest(state, result)
        batch = queue_core.claim_batch(
            state,
            platform=PLATFORM,
            batch_size=batch_size,
            stale_ttl=stale_ttl,
            group_pending_ttl=group_ttl,
            private_pending_ttl=private_ttl,
        )
        payload = build_payload(state, batch) if batch else None
    return payload


def _run_send_command(send_cmd: str, chat_id, text: str):
    """Invoke the deployment's send executable once.

    Returns ``(outcome, message_id)``:

    - ``("sent", id)`` — exit 0 and a message id as the last
      non-empty stdout line.
    - ``("unknown", None)`` — the runner timed out or could not be
      started, exited nonzero (including a signal), produced undecodable
      output, or exited 0 without a message id: the send
      may or may not have been delivered, so the item must stay in
      "sending" for reconciliation and never be resent blindly.

    The executable contract has no verifiable no-send result. An exit
    code alone cannot prove rejection: remote acceptance may precede a
    lost response or a local crash. Confirmed non-delivery uses the
    separate operator --send-failed path after reconciliation.
    """
    try:
        proc = subprocess.run(
            [send_cmd, str(chat_id)],
            input=text,
            capture_output=True,
            text=True,
            timeout=SEND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return "unknown", None
    if proc.returncode != 0:
        return "unknown", None
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if not lines:
        return "unknown", None
    return "sent", lines[-1]


def send_fenced(
    store: JsonStateStore,
    target: int,
    token,
    chat_id,
    text: str,
    *,
    max_reply_age: float | None = DEFAULT_MAX_REPLY_AGE,
    now: float | None = None,
) -> dict:
    """Send one queue reply under the send fence.

    Order is the whole point: configuration is checked first (an
    unconfigured deployment leaves the item claimed), then the fence
    — verify the item is claimed, the token matches and no reply is
    recorded, and move it to "sending" — is persisted in one
    transaction. Only after the fence is durable does the send
    command run, exactly once. Its outcome decides the result:

    - sent → the item stays in "sending"; the caller records the
      reply with --replied and closes with --done, as usual.
    - unknown → the item stays in "sending" (send-unknown) for
      operator reconciliation; it is never resent automatically.
      Only independently verified non-delivery permits an operator
      to return it to pending with --send-failed.
    - stale → the item is older than ``max_reply_age`` (seconds
      since intake; None disables the guard) and has no recorded
      reply. The fence refuses
      BEFORE marking "sending" or running the send command, and
      closes the item as expired (token-checked, same transaction):
      the conversation has moved on since intake, so the reply
      would land off-context. If the close itself is refused (bad
      token, wrong status), the result is a plain fence refusal
      and nothing changes.
    """
    # Refuse invalid text before reserving the send. Telegram sendMessage
    # accepts 1-4096 characters, and a rejected empty/oversized reply must
    # never strand an otherwise valid claim in an ambiguous "sending" state.
    if not isinstance(text, str) or not text.strip() or len(text) > 4096:
        return {"ok": False, "stage": "input"}
    send_cmd = os.environ.get(SEND_CMD_ENV)
    if not send_cmd:
        return {"ok": False, "stage": "config"}
    with store.transaction() as state:
        _ensure_adapter_state(state)
        item = queue_core.find_item(state, PLATFORM, target)
        # The model/dispatcher controls argv, but not the intended recipient.
        # Bind the requested chat to the durable intake record *inside* the
        # same transaction as the claim fence, before any external send.
        queued_chat_id = item.get("chat_id") if item is not None else None
        if type(queued_chat_id) is not int or str(queued_chat_id) != str(chat_id):
            return {"ok": False, "stage": "fence"}
        if (
            max_reply_age is not None
            and item is not None
            and item["replied_at"] is None
            and (time.time() if now is None else now) - item["received_at"]
            > max_reply_age
        ):
            closed = queue_core.mark_done(
                state,
                PLATFORM,
                target,
                token,
                reason=queue_core.EXPIRED_REASON,
                now=now,
            )
            return {"ok": False, "stage": "stale" if closed else "fence"}
        fenced = queue_core.mark_sending(state, PLATFORM, target, token)
    if not fenced:
        return {"ok": False, "stage": "fence"}
    # Never trust the caller's destination, even after comparison. Only
    # the queue's persisted chat ID may reach the sender subprocess.
    outcome, message_id = _run_send_command(send_cmd, queued_chat_id, text)
    if outcome == "sent":
        return {"ok": True, "message_id": message_id}
    return {"ok": False, "stage": "unknown"}


def main(argv=None, *, fetch=fetch_updates, path: Path | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    store = JsonStateStore(path or state_path())

    if args[:1] == ["--adopt"] and len(args) == 2:
        # Bootstrap is a one-time operator action, not a mutable preference.
        # Silently replacing an established owner can transfer all future DM
        # access to the wrong Telegram account.
        try:
            requested = int(args[1])
        except ValueError:
            requested = 0
        if requested <= 0:
            print(json.dumps({"ok": False, "error": "invalid-owner-id"}))
            return 2
        with store.transaction() as state:
            _ensure_adapter_state(state)
            existing = state.get("owner_id")
            if existing is not None and (type(existing) is not int or existing != requested):
                print(json.dumps({"ok": False, "error": "owner-already-adopted"}))
                return 2
            state["owner_id"] = requested
            state["candidates"] = []
        print(json.dumps({"owner_id": requested}))
        return 0

    if args[:1] in (["--done"], ["--release"]) and len(args) in (2, 3, 4, 5):
        target = int(args[1])
        token = args[2] if len(args) >= 3 else None
        reason = args[3] if len(args) >= 4 and args[0] == "--done" else None
        evidence = args[4] if len(args) == 5 and args[0] == "--done" else None
        with store.transaction() as state:
            _ensure_adapter_state(state)
            if args[0] == "--done":
                ok = queue_core.mark_done(
                    state, PLATFORM, target, token,
                    reason=reason, evidence=evidence,
                )
            else:
                ok = queue_core.release(state, PLATFORM, target, token)
        print(json.dumps({"ok": ok}))
        return 0

    if args[:1] == ["--send-fenced"] and len(args) == 4:
        result = send_fenced(
            store,
            int(args[1]),
            args[2],
            args[3],
            sys.stdin.read(),
            max_reply_age=_max_reply_age(),
        )
        print(json.dumps(result))
        return 0

    if args[:1] == ["--purge-expired"]:
        older_than = None
        dry_run = False
        rest = args[1:]
        index = 0
        valid = True
        while index < len(rest):
            if rest[index] == "--dry-run":
                dry_run = True
                index += 1
            elif rest[index] == "--older-than" and index + 1 < len(rest):
                try:
                    older_than = float(rest[index + 1])
                except ValueError:
                    valid = False
                    break
                index += 2
            else:
                valid = False
                break
        if not valid:
            print(json.dumps({
                "ok": False,
                "error": "usage: --purge-expired "
                         "[--older-than SECONDS] [--dry-run]",
            }))
            return 2
        group_ttl, private_ttl = _expiry_ttls()
        if older_than is not None:
            group_ttl = older_than
        if dry_run:
            # A dry run only reads: no transaction, no save.
            state = _ensure_adapter_state(store.load())
            expired = queue_core.expire_stale_pending(
                state,
                platform=PLATFORM,
                group_ttl=group_ttl,
                private_ttl=private_ttl,
                dry_run=True,
            )
        else:
            with store.transaction() as state:
                _ensure_adapter_state(state)
                expired = queue_core.expire_stale_pending(
                    state,
                    platform=PLATFORM,
                    group_ttl=group_ttl,
                    private_ttl=private_ttl,
                )
        print(json.dumps({
            "ok": True,
            "dry_run": dry_run,
            "expired": len(expired),
            "external_ids": [item["external_id"] for item in expired],
        }))
        return 0

    if args[:1] == ["--replied"] and len(args) in (3, 4):
        target = int(args[1])
        token = args[3] if len(args) == 4 else None
        with store.transaction() as state:
            _ensure_adapter_state(state)
            ok = queue_core.mark_replied(
                state, PLATFORM, target, args[2], token
            )
        print(json.dumps({"ok": ok}))
        return 0

    if args[:1] in (["--sending"], ["--send-failed"]) and len(args) in (2, 3):
        target = int(args[1])
        token = args[2] if len(args) == 3 else None
        with store.transaction() as state:
            _ensure_adapter_state(state)
            if args[0] == "--sending":
                ok = queue_core.mark_sending(state, PLATFORM, target, token)
            else:
                ok = queue_core.mark_send_failed(state, PLATFORM, target, token)
        print(json.dumps({"ok": ok}))
        return 0

    payload = run_once(store, fetch=fetch)
    if payload:
        print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
