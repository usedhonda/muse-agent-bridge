"""Polling intake for deployments where the bot credential is vault-held.

The Socket Mode listener (bridge/listener.py) needs an app credential
injected into its environment. On Muse's agent VM the bot token instead lives in the
secure connection as `custom.slack-muse-bot`; local processes receive an
`hsurr:*` surrogate from authd and the egress layer swaps it for the real
token at authorized egress. Real credentials are not exposed to the
agent/runtime cell; they are held in a separate security domain in the VM.

This poller is the matching intake for that custody model: it polls the
owner's DM channel and allowlisted channels, including validated agent rooms, with the bot
credential and appends eligible owner/configured-agent messages to the same durable queue the
Socket Mode listener writes, in the same record format, so
`bridge.worker` claims them identically.

Trade-off: intake latency is the poll interval (default 20s) instead of
Socket Mode's push. Custody and stability win for this deployment; the
Socket Mode listener requires an approved app-credential injection and
WSS route; credential custody alone does not establish lifecycle support.
"""
from __future__ import annotations

import argparse
import fcntl
import os
import json
import tempfile
import sys
import time
import urllib.request
import urllib.parse
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .config import load_config
from .store import EventStore
from .intake import normalize_event
from .webapi import SlackWebClient

CREDENTIAL = "custom.slack-muse-bot"
_API = "https://slack.com/api/"


def _request(method: str, params: dict, query=False) -> dict:
    # Keep offline tests and --help portable; only live polling requires
    # the runtime's credential adapter. Never substitute another credential.
    sys.path.insert(0, "/opt/muse/skills/custom-connector-setup/bin")
    try:
        from dynamic_credentials import add_surrogate_to_request, read_json_response
    except ImportError as exc:
        raise RuntimeError("polling requires the Muse dynamic_credentials adapter; "
                           "use Socket Mode on other hosts") from exc
    if query:
        request = urllib.request.Request(
            _API + method + "?" + urllib.parse.urlencode(params), method="GET")
    else:
        request = urllib.request.Request(
            _API + method, data=json.dumps(params).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
    add_surrogate_to_request(request, CREDENTIAL, allowed_hosts=["slack.com"])
    with urllib.request.urlopen(request, timeout=20) as response:
        result = read_json_response(response)
    if not result.get("ok"):
        raise RuntimeError(f"{method} failed: {result.get('error')}")
    return result


def api(method: str, params: dict) -> dict:
    return _request(method, params)


def api_get(method: str, params: dict) -> dict:
    return _request(method, params, query=True)


class PollWebClient(SlackWebClient):
    """Reuse verified bot/room checks with the existing surrogate adapter.

    No bot credential is copied into this client. Helpers remain lazy imports
    in _request for both GET and POST; offline imports never call them.
    """
    def __init__(self):
        self._bot_user_id = None

    def _post(self, method, params):
        return api(method, params)

    def _get(self, method, params):
        return api_get(method, params)


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("history timestamp must be an exact string")
    try:
        result = Decimal(value)
        if result.is_finite() and result >= 0:
            return result
    except InvalidOperation:
        pass
    raise ValueError("invalid history timestamp")


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
        self.web = PollWebClient()

    def _save(self) -> None:
        # Same-directory atomic replace; interrupted checkpoints retain the old
        # complete watermark and queue dedupe makes a retry safe.
        descriptor, temporary = tempfile.mkstemp(dir=self.state_path.parent,
                                                prefix=self.state_path.name + ".")
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump(self.state, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.state_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def ensure_dm_channel(self) -> str:
        cached = self.state.get("dm_channel")
        if cached:
            return cached
        result = api("conversations.open", {"users": self.owner_id})
        channel = result["channel"]["id"]
        self.state["dm_channel"] = channel
        self._save()
        return channel

    def _history(self, channel, oldest):
        """Do not expose a partial range to checkpointing. Bodies stay in memory."""
        messages, cursors, cursor = [], set(), None
        for _ in range(100):
            params = {"channel": channel, "oldest": oldest, "inclusive": True, "limit": 50}
            if cursor:
                params["cursor"] = cursor
            result = api("conversations.history", params)
            messages.extend(result.get("messages", []))
            following = result.get("response_metadata", {}).get("next_cursor")
            if not following:
                if result.get("has_more"):
                    raise ValueError("history incomplete without next cursor")
                # Validate all exact timestamps before any queue/checkpoint write.
                return sorted(messages, key=lambda message: timestamp(message["ts"]))
            if following in cursors:
                raise ValueError("history cursor did not advance")
            cursors.add(following)
            cursor = following
        raise ValueError("history page bound reached; watermark unchanged")

    def poll_once(self) -> int:
        # Separate from append.lock: HTTP must never block Socket queue appends.
        # Reload under the lock so concurrent pollers cannot overwrite a newer
        # checkpoint with an older in-memory copy.
        with self.state_path.with_suffix(self.state_path.suffix + ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.state = (json.loads(self.state_path.read_text()) if self.state_path.exists()
                          else {"channels": {}})
            bot_user_id = self.web.auth_test()
            targets = {self.ensure_dm_channel(), *self.channels}
            appended = 0
            for channel in sorted(targets):
                last_ts = self.state["channels"].get(channel, "0")
                messages = self._history(channel, last_ts)
                high = last_ts
                for message in messages:
                    ts = message["ts"]
                    if timestamp(ts) <= timestamp(last_ts):
                        continue
                    if timestamp(ts) > timestamp(high):
                        high = ts
                    event = dict(message, type=message.get("type", "message"), channel=channel)
                    record, _ = normalize_event(self.cfg, event, f"slack:{channel}:{ts}",
                                                "poll", self.web, bot_user_id)
                    if record is not None and self.store.append(record):
                        appended += 1
                # No partial history, failed room API check or failed append
                # can checkpoint past unseen/unpersisted work in this channel.
                self.state["channels"][channel] = high
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
    parser = argparse.ArgumentParser(
        description="Muse polling intake with shared Socket/room admission checks")
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
