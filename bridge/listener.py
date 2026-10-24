"""Socket Mode listener: gate, dedupe, queue, ack reaction.

Flow per envelope (design: docs/design.md):
  1. Resolve the event to an audience; ACK and drop silently if none.
  2. Verify configured private-room membership before retaining any body.
  3. Append to the durable queue (deduplicated by event_id), then ACK.
  4. Only then post the ack reaction, so "seen" implies "queued".

A validation or storage exception leaves the envelope unacknowledged; this
avoids confirming receipt of an event we cannot retain safely. Slow membership
checks can exceed Slack's ACK deadline; this is not a delivery guarantee.

Tokens come from the environment (SLACK_APP_TOKEN / SLACK_BOT_TOKEN),
injected by the service wrapper from the secure connection. They are
never read from files in this repository.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import os
import time
from datetime import datetime, timezone

from .audience import resolve_event
from .config import BridgeConfig, load_config
from .store import EventStore


class Reconnect(Exception):
    pass


class Listener:
    def __init__(self, cfg: BridgeConfig, store: EventStore, web, bot_user_id: str, diagnostic=None):
        self.cfg = cfg
        self.store = store
        self.web = web
        self.bot_user_id = bot_user_id
        self.diagnostic = diagnostic

    def _diagnose(self, stage, reason, payload, event):
        if self.diagnostic is None:
            return
        # Only fixed classifications and validated correlation metadata. Never
        # retain the body, sender/channel IDs, credentials or exception text.
        report = dict(stage=stage, reason=reason, at=datetime.now(timezone.utc).isoformat())
        event_id, ts = payload.get("event_id"), event.get("ts")
        if isinstance(event_id, str) and re.fullmatch(r"Ev[A-Za-z0-9]{1,64}", event_id):
            report["event_id"] = event_id
        if isinstance(ts, str) and re.fullmatch(r"[0-9]{1,20}\.[0-9]{1,10}", ts):
            report["message_ts"] = ts
        self.diagnostic(report)

    def handle_envelope(self, envelope: dict, send_ack) -> None:
        envelope_type = envelope.get("type")
        if envelope_type == "events_api":
            payload = envelope["payload"]
            event = payload.get("event", {})
            decision = resolve_event(self.cfg, event, self.bot_user_id)
            if not decision.accepted:
                send_ack(envelope["envelope_id"])
                self._diagnose("dropped", decision.reason, payload, event)
                return  # silent drop: no queue entry, no reaction, no body kept
            agents = decision.audience.agent_channels.get(event.get("channel"))
            if agents and (self.bot_user_id in agents or not self.web.private_room_matches(
                event["channel"], decision.audience.user_ids | agents | {self.bot_user_id}
            )):
                send_ack(envelope["envelope_id"])
                self._diagnose("dropped", "private-room-mismatch", payload, event)
                return
            record = {
                "event_id": payload.get("event_id"),
                "transport": "socket",
                "audience_id": decision.audience.id,
                "scope": decision.audience.scope,
                "channel": event.get("channel"),
                "user": event.get("user"),
                "text": event.get("text", ""),
                "ts": event.get("ts"),
                "thread_ts": event.get("thread_ts") or event.get("ts"),
                "received_at": datetime.now(timezone.utc).isoformat(),
            }
            if agents:
                # Keep an existing thread; ordinary room messages stay in the room.
                thread = event.get("thread_ts")
                if not thread and event.get("reply_count", 0):
                    thread = event.get("ts")
                record.update(reply_thread_ts=thread, conversation_ts=thread or "",
                              bot_user_id=self.bot_user_id)
            appended = self.store.append(record)
            # Duplicate IDs are already durable; ACK them too. Exceptions above
            # must not ACK an event whose verification or persistence failed.
            send_ack(envelope["envelope_id"])
            self._diagnose("queued" if appended else "duplicate",
                           "durable-intake" if appended else "already-durable", payload, event)
            if appended and self.cfg.ack_reaction:
                self.web.reactions_add(
                    record["channel"], record["ts"], self.cfg.ack_reaction
                )
        elif envelope_type == "disconnect":
            raise Reconnect()
        # "hello" and other control envelopes need no action.

    def run(self, transport) -> None:
        while True:
            envelope = transport.receive()
            self.handle_envelope(envelope, transport.send_ack)


def authenticate(web, sleep=time.sleep, attempts=5, diagnostic=None):
    """Bounded startup retry for transport failures; invalid identities fail closed."""
    for attempt in range(attempts):
        try:
            return web.auth_test()
        except (OSError, ConnectionError):
            if diagnostic is not None:
                diagnostic(dict(stage="auth-retry", attempt=attempt + 1))
            if attempt + 1 < attempts:
                sleep(min(2 ** attempt, 30))
    raise RuntimeError("bot authentication unavailable after startup retries") from None


def serve(listener, app_token, transport_factory, sleep=time.sleep, diagnostic=None):
    backoff = 1
    while True:
        transport = transport_factory(app_token)
        try:
            transport.connect()
            backoff = 1
            listener.run(transport)
        except (Reconnect, ConnectionError, OSError):
            if diagnostic is not None:
                diagnostic(dict(stage="reconnect", retry_seconds=backoff))
        finally:
            try:
                transport.close()
            except (ConnectionError, OSError):
                pass
        sleep(backoff)
        backoff = min(backoff * 2, 60)


def main() -> None:  # pragma: no cover - live entry point
    parser = argparse.ArgumentParser(description="muse-slack-bridge listener")
    parser.add_argument("--config", default=".local/config.yaml")
    parser.add_argument("--state", default=".local/state")
    parser.add_argument("--diagnostics", action="store_true",
                        help="emit metadata-only intake decisions to stderr")
    args = parser.parse_args()

    app_token = os.environ["SLACK_APP_TOKEN"]
    bot_token = os.environ["SLACK_BOT_TOKEN"]

    from .transport import SocketModeTransport
    from .webapi import SlackWebClient

    cfg = load_config(args.config)
    store = EventStore(args.state)
    web = SlackWebClient(bot_token)
    diagnostic = (lambda report: print(json.dumps(report), file=sys.stderr, flush=True)) if args.diagnostics else None
    bot_user_id = authenticate(web, diagnostic=diagnostic)
    listener = Listener(cfg, store, web, bot_user_id, diagnostic=diagnostic)

    serve(listener, app_token, SocketModeTransport, diagnostic=diagnostic)


if __name__ == "__main__":  # pragma: no cover
    main()

