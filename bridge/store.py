"""Durable local state: an append-only event queue plus a seen-ID store.

Both live under the state dir (inside `.local/` in real deployments).
Writes are fsync'd so a queued event survives a listener restart; event
IDs are deduplicated across restarts if Slack redelivers an event. Replay
after a disconnection is not guaranteed by this local store.
"""
from __future__ import annotations

import json
import fcntl
from contextlib import contextmanager
from pathlib import Path


class EventStore:
    def __init__(self, state_dir: str | Path):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.queue_path = self.state_dir / "queue.jsonl"
        self.seen_path = self.state_dir / "seen_ids.txt"
        self._reload_seen()

    def _reload_seen(self):
        self._seen: set[str] = set()
        if self.seen_path.exists():
            self._seen = {
                line.strip()
                for line in self.seen_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            }
        # Recover the crash window between the queue fsync and seen-ID fsync.
        queued = self.read_queue()
        self._seen.update(record["event_id"] for record in queued)
        # The same Slack message can arrive through two transports (Socket
        # Mode listener and history poller) under different event IDs.
        # (channel, ts) identifies the message itself; when both records
        # name their transport and the transports differ, the second copy
        # is a handover duplicate and must not queue twice. Records that
        # do not name a transport keep plain event-ID semantics.
        self._seen_messages: dict[tuple[str, str], str] = {}
        for record in queued:
            channel, ts, transport = (record.get("channel"), record.get("ts"),
                                      record.get("transport"))
            if (isinstance(channel, str) and isinstance(ts, str)
                    and isinstance(transport, str)):
                self._seen_messages.setdefault((channel, ts), transport)

    def append(self, record: dict) -> bool:
        """Append a record; returns False (and writes nothing) on duplicate."""
        # A separate lock file covers both persistence files. Refresh under
        # that lock: another long-lived listener/poller may have appended.
        with self.locked():
            self._reload_seen()
            return self._append_locked(record)

    @contextmanager
    def locked(self):
        """Coordinate snapshots/send transitions with cooperating intake writers.

        Lock order is queue first, then worker SQLite. Never hold this lock
        across Slack API calls.
        """
        with (self.state_dir / "append.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def _append_locked(self, record: dict) -> bool:
        event_id = record["event_id"]
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("event_id must be a nonempty string")
        if event_id in self._seen:
            return False
        message_key = (record.get("channel"), record.get("ts"))
        transport = record.get("transport")
        if (isinstance(message_key[0], str) and isinstance(message_key[1], str)
                and isinstance(transport, str)):
            owner = self._seen_messages.get(message_key)  # type: ignore[arg-type]
            if owner is not None and owner != transport:
                return False
        if self.queue_path.exists() and self.queue_path.stat().st_size:
            with self.queue_path.open("rb") as tail:
                tail.seek(-1, 2)
                if tail.read(1) != b"\n":
                    raise ValueError("queue has an incomplete tail; recover before appending")
        with self.queue_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()
            import os

            os.fsync(fh.fileno())
        with self.seen_path.open("a", encoding="utf-8") as fh:
            fh.write(event_id + "\n")
            fh.flush()
            import os

            os.fsync(fh.fileno())
        self._seen.add(event_id)
        if (isinstance(message_key[0], str) and isinstance(message_key[1], str)
                and isinstance(transport, str)):
            self._seen_messages.setdefault(message_key, transport)  # type: ignore[arg-type]
        return True

    def read_queue(self) -> list[dict]:
        if not self.queue_path.exists():
            return []
        return [
            json.loads(line)
            for line in self.queue_path.read_bytes().splitlines(keepends=True)
            if line.endswith(b"\n") and line.strip()
        ]

