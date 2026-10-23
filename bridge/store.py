"""Durable local state: an append-only event queue plus a seen-ID store.

Both live under the state dir (inside `.local/` in real deployments).
Writes are fsync'd so a queued event survives a listener restart; event
IDs are deduplicated across restarts (Slack redelivers on reconnect).
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

    def append(self, record: dict) -> bool:
        """Append a record; returns False (and writes nothing) on duplicate."""
        event_id = record["event_id"]
        if event_id in self._seen:
            return False
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
        return True

    def read_queue(self) -> list[dict]:
        if not self.queue_path.exists():
            return []
        return [
            json.loads(line)
            for line in self.queue_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
