"""Socket Mode listener: gate, dedupe, queue, ack reaction.

Flow per envelope (design: docs/design.md):
  1. Acknowledge the envelope immediately (Socket Mode protocol).
  2. Resolve the event to an audience; drop silently if none.
  3. Append to the durable queue (deduplicated by event_id).
  4. Only then post the ack reaction, so "seen" implies "queued".

Tokens come from the environment (SLACK_APP_TOKEN / SLACK_BOT_TOKEN),
injected by the service wrapper from the secure connection. They are
never read from files in this repository.
"""
from __future__ import annotations

import argparse
import os
import time
from datetime import datetime, timezone

from .audience import resolve_event
from .config import BridgeConfig, load_config
from .store import EventStore


class Reconnect(Exception):
    pass


class Listener:
    def __init__(self, cfg: BridgeConfig, store: EventStore, web, bot_user_id: str):
        self.cfg = cfg
        self.store = store
        self.web = web
        self.bot_user_id = bot_user_id

    def handle_envelope(self, envelope: dict, send_ack) -> None:
        envelope_type = envelope.get("type")
        if envelope_type == "events_api":
            # Socket Mode requires a prompt envelope ack regardless of the
            # audience decision; it is protocol-level, invisible to users.
            send_ack(envelope["envelope_id"])
            payload = envelope["payload"]
            event = payload.get("event", {})
            decision = resolve_event(self.cfg, event, self.bot_user_id)
            if not decision.accepted:
                return  # silent drop: no queue entry, no reaction, no body kept
            record = {
                "event_id": payload.get("event_id"),
                "audience_id": decision.audience.id,
                "scope": decision.audience.scope,
                "channel": event.get("channel"),
                "user": event.get("user"),
                "text": event.get("text", ""),
                "ts": event.get("ts"),
                "thread_ts": event.get("thread_ts") or event.get("ts"),
                "received_at": datetime.now(timezone.utc).isoformat(),
            }
            if self.store.append(record) and self.cfg.ack_reaction:
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


def main() -> None:  # pragma: no cover - live entry point
    parser = argparse.ArgumentParser(description="muse-slack-bridge listener")
    parser.add_argument("--config", default=".local/config.yaml")
    parser.add_argument("--state", default=".local/state")
    args = parser.parse_args()

    app_token = os.environ["SLACK_APP_TOKEN"]
    bot_token = os.environ["SLACK_BOT_TOKEN"]

    from .transport import SocketModeTransport
    from .webapi import SlackWebClient

    cfg = load_config(args.config)
    store = EventStore(args.state)
    web = SlackWebClient(bot_token)
    bot_user_id = web.auth_test()
    listener = Listener(cfg, store, web, bot_user_id)

    backoff = 1
    while True:
        transport = SocketModeTransport(app_token)
        try:
            transport.connect()
            backoff = 1
            listener.run(transport)
        except (Reconnect, ConnectionError, OSError):
            transport.close()
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


if __name__ == "__main__":  # pragma: no cover
    main()
