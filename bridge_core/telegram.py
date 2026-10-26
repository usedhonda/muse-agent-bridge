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
                           --done), {"ok": false, "stage": "send",
                           "returned_to_pending": true} on a definitive
                           failure (item returned to pending),
                           {"ok": false, "stage": "unknown"} when the
                           outcome is ambiguous (item stays "sending",
                           never resent), {"ok": false, "stage":
                           "fence"} when the fence refuses (nothing
                           is sent), and {"ok": false, "stage":
                           "config"} when the send command is not
                           configured (item stays claimed)
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
  --adopt ID   set owner_id (bootstrap; main agent only)

Turn protocol per replied item: --send-fenced (marks the item
"sending" and performs the send under the fence), then --replied,
then --done. The manual --sending step remains available for
operator use. Items closed without a reply only need --done.

State lives in one JSON file managed by ``bridge_core.store``: the core
queue plus this adapter's keys (``offset``, ``owner_id``, ``group_id``,
``group_buffer``, ``candidates``, ``candidate_groups``). The state path
is ``$TELEGRAM_BRIDGE_STATE_PATH`` or ``state.json`` beside this file.
Batch size and stale TTL can be tuned with ``$TELEGRAM_BRIDGE_BATCH_SIZE``
and ``$TELEGRAM_BRIDGE_STALE_TTL`` (seconds).

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

HERE = Path(__file__).resolve().parent


def state_path() -> Path:
    override = os.environ.get("TELEGRAM_BRIDGE_STATE_PATH")
    return Path(override) if override else HERE / "state.json"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


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
    with store.transaction() as state:
        _ensure_adapter_state(state)
        ingest(state, result)
        batch = queue_core.claim_batch(
            state, platform=PLATFORM, batch_size=batch_size, stale_ttl=stale_ttl
        )
        payload = build_payload(state, batch) if batch else None
    return payload


def _run_send_command(send_cmd: str, chat_id, text: str):
    """Invoke the deployment's send executable once.

    Returns ``(outcome, message_id)``:

    - ``("sent", id)`` — exit 0 and a message id as the last
      non-empty stdout line.
    - ``("failed", None)`` — nonzero exit: the send definitively
      did not go through.
    - ``("unknown", None)`` — the runner timed out or could not be
      started, or exited 0 without a parseable message id: the send
      may or may not have been delivered, so the item must stay in
      "sending" for reconciliation and never be resent blindly.
    """
    try:
        proc = subprocess.run(
            [send_cmd, str(chat_id)],
            input=text,
            capture_output=True,
            text=True,
            timeout=SEND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown", None
    if proc.returncode != 0:
        return "failed", None
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if not lines:
        return "unknown", None
    return "sent", lines[-1]


def send_fenced(store: JsonStateStore, target: int, token, chat_id, text: str) -> dict:
    """Send one queue reply under the send fence.

    Order is the whole point: configuration is checked first (an
    unconfigured deployment leaves the item claimed), then the fence
    — verify the item is claimed, the token matches and no reply is
    recorded, and move it to "sending" — is persisted in one
    transaction. Only after the fence is durable does the send
    command run, exactly once. Its outcome decides the result:

    - sent → the item stays in "sending"; the caller records the
      reply with --replied and closes with --done, as usual.
    - definitively failed → the item returns to pending (exactly
      mark_send_failed) so it can be claimed and answered later.
    - unknown → the item stays in "sending" (send-unknown) for
      operator reconciliation; it is never resent automatically.
    """
    send_cmd = os.environ.get(SEND_CMD_ENV)
    if not send_cmd:
        return {"ok": False, "stage": "config"}
    with store.transaction() as state:
        _ensure_adapter_state(state)
        fenced = queue_core.mark_sending(state, PLATFORM, target, token)
    if not fenced:
        return {"ok": False, "stage": "fence"}
    outcome, message_id = _run_send_command(send_cmd, chat_id, text)
    if outcome == "sent":
        return {"ok": True, "message_id": message_id}
    if outcome == "failed":
        with store.transaction() as state:
            _ensure_adapter_state(state)
            returned = queue_core.mark_send_failed(state, PLATFORM, target, token)
        return {"ok": False, "stage": "send", "returned_to_pending": returned}
    return {"ok": False, "stage": "unknown"}


def main(argv=None, *, fetch=fetch_updates, path: Path | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    store = JsonStateStore(path or state_path())

    if args[:1] == ["--adopt"] and len(args) == 2:
        with store.transaction() as state:
            _ensure_adapter_state(state)
            state["owner_id"] = int(args[1])
            state["candidates"] = []
        print(json.dumps({"owner_id": int(args[1])}))
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
            store, int(args[1]), args[2], args[3], sys.stdin.read()
        )
        print(json.dumps(result))
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
