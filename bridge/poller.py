"""Polling intake for deployments where the bot credential is vault-held.

The Socket Mode listener (bridge/listener.py) needs the raw app token in
its environment. On Muse's agent VM the bot token instead lives in the
secure connection as `custom.slack-muse-bot`; local processes receive an
`hsurr:*` surrogate from authd and the egress layer swaps it for the real
token on requests to slack.com. No raw token is ever stored on this VM.

This poller is the matching intake for that custody model: it polls the
owner's DM channel (and any allowlisted group channels) with the bot
credential and appends new owner messages to the same durable queue the
Socket Mode listener writes, in the same record format, so
`bridge.worker` claims them identically.

Trade-off: intake latency is the poll interval (default 20s) instead of
Socket Mode's push. Custody and stability win for this deployment; the
Socket Mode listener remains the upgrade path when a raw-token injection
route is sanctioned.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .config import load_config
from .store import EventStore

CREDENTIAL = "custom.slack-muse-bot"
_API = "https://slack.com/api/"


def api(method: str, params: dict) -> dict:
    # Keep offline tests and --help portable; only live polling requires
    # the runtime's credential adapter. Never substitute another credential.
    sys.path.insert(0, "/opt/muse/skills/custom-connector-setup/bin")
    try:
        from dynamic_credentials import add_surrogate_to_request, read_json_response
    except ImportError as exc:
        raise RuntimeError("polling requires the Muse dynamic_credentials adapter; "
                           "use Socket Mode on other hosts") from exc
    request = urllib.request.Request(
        _API + method,
        data=json.dumps(params).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    add_surrogate_to_request(request, CREDENTIAL, allowed_hosts=["slack.com"])
    with urllib.request.urlopen(request, timeout=20) as response:
        result = read_json_response(response)
    if not result.get("ok"):
        raise RuntimeError(f"{method} failed: {result.get('error')}")
    return result


class Poller:
    def __init__(self, cfg, store: EventStore, state_path: Path):
        self.cfg = cfg
        self.store = store
        self.state_path = state_path
        self.state = {"channels": {}}
        if state_path.exists():
            self.state = json.loads(state_path.read_text(encoding="utf-8"))
        owner = cfg.audiences[0]
        self.owner_id = next(iter(owner.user_ids))
        self.channels = set(owner.channel_allowlist)

    def _save(self) -> None:
        self.state_path.write_text(json.dumps(self.state), encoding="utf-8")

    def ensure_dm_channel(self) -> str:
        cached = self.state.get("dm_channel")
        if cached:
            return cached
        result = api("conversations.open", {"users": self.owner_id})
        channel = result["channel"]["id"]
        self.state["dm_channel"] = channel
        self._save()
        return channel

    def poll_once(self) -> int:
        targets = {self.ensure_dm_channel(), *self.channels}
        appended = 0
        for channel in sorted(targets):
            last_ts = self.state["channels"].get(channel, "0")
            result = api(
                "conversations.history",
                {"channel": channel, "oldest": last_ts, "limit": 50},
            )
            messages = sorted(result.get("messages", []), key=lambda m: m["ts"])
            for message in messages:
                ts = message["ts"]
                if ts <= last_ts:
                    continue
                self.state["channels"][channel] = ts
                if message.get("user") != self.owner_id:
                    continue
                if message.get("subtype") or message.get("bot_id"):
                    continue
                record = {
                    "event_id": f"slack:{channel}:{ts}",
                    "transport": "poll",
                    "audience_id": "owner",
                    "scope": "owner",
                    "channel": channel,
                    "user": message["user"],
                    "text": message.get("text", ""),
                    "ts": ts,
                    "thread_ts": message.get("thread_ts") or ts,
                    "received_at": datetime.now(timezone.utc).isoformat(),
                }
                if self.store.append(record):
                    appended += 1
            self._save()
        return appended

    def run(self, interval: float) -> None:
        while True:
            try:
                self.poll_once()
            except Exception as exc:  # keep the loop alive; systemd restarts on exit
                print(f"poller: {exc}", file=sys.stderr, flush=True)
            time.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="muse-slack-bridge poller")
    parser.add_argument("--config", default=".local/config.yaml")
    parser.add_argument("--state", default=".local/state")
    parser.add_argument("--interval", type=float, default=20.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    cfg = load_config(args.config)
    store = EventStore(args.state)
    poller = Poller(cfg, store, Path(args.state) / "poller.json")
    if args.once:
        print(poller.poll_once())
    else:
        poller.run(args.interval)


if __name__ == "__main__":
    main()
