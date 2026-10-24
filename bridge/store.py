"""Durable local state: an append-only event queue plus a seen-ID store.

Both live under the state dir (inside `.local/` in real deployments).
Writes are fsync'd so a queued event survives a listener restart; event
IDs are deduplicated across restarts if Slack redelivers an event. Replay
after a disconnection is not guaranteed by this local store.
"""
from __future__ import annotations

import json
from pathlib import Path


class EventStore:
    def __init__(self, state_dir: str | Path):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.queue_path = self.state_dir / "queue.jsonl"
        self.seen_path = self.state_dir / "seen_ids.txt"
        self._seen: set[str] = set()
        if self.seen_path.exists():
            self._seen = {
                line.strip()
                for line in self.seen_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            }
        # Recover the crash window between the queue fsync and seen-ID fsync.
        self._seen.update(record["event_id"] for record in self.read_queue())

    def append(self, record: dict) -> bool:
        """Append a record; returns False (and writes nothing) on duplicate."""
        event_id = record["event_id"]
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("event_id must be a nonempty string")
        if event_id in self._seen:
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
        # The queue is authoritative and durable now. A secondary seen-ID
        # failure must not make a same-process redelivery append a duplicate.
        self._seen.add(event_id)
        with self.seen_path.open("a", encoding="utf-8") as fh:
            fh.write(event_id + "\n")
            fh.flush()
            import os

            os.fsync(fh.fileno())
        return True

    def read_queue(self) -> list[dict]:
        if not self.queue_path.exists():
            return []
        return [
            json.loads(line)
            for line in self.queue_path.read_bytes().splitlines(keepends=True)
            if line.endswith(b"\n") and line.strip()
        ]

