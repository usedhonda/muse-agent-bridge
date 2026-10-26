"""Locked, atomic JSON state storage that fails loud on corruption.

Every mutation runs inside one transaction: an exclusive ``flock``
serializes load -> modify -> save across processes (the poller, woken
turns marking items done, operator commands), and the save itself is a
per-process temp file plus an atomic rename. The last successfully
saved bytes are also kept as a ``.good`` snapshot — but only as
forensic evidence for the operator, never as an automatic fallback.

Loads deliberately do NOT recover on their own. Silently adopting an
older snapshot (or an empty state) after corruption would roll the
queue back in time: completion and reply records would be lost and
already-answered items could be resurrected and answered twice. So a
main file that exists but does not parse — or a main file that is
missing while a snapshot exists — raises :class:`StateStoreError`
and an operator restores the snapshot deliberately. Only a brand-new
deployment (neither file exists) starts empty.

This replaces the ad-hoc pattern (fixed temp name, no lock) under which
two writers could interleave into the same temp file and corrupt the
live state.
"""
from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path


class StateStoreError(RuntimeError):
    """The state on disk is corrupt or inconsistent.

    Raised instead of silently rolling back to an older snapshot; an
    operator must inspect and restore the state deliberately.
    """


class JsonStateStore:
    """A single JSON document guarded by a lock file.

    ``path`` is the state file. Two side files are managed next to it:
    ``<name>.lock`` (never written, only flocked) and ``<name>.good``
    (copy of the last successfully saved state, kept for the
    operator; never adopted automatically).
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.snapshot_path = self.path.with_name(self.path.name + ".good")

    # -- internals -----------------------------------------------------

    def _read_json(self, path: Path) -> dict | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _load_unlocked(self) -> dict:
        if self.path.exists():
            state = self._read_json(self.path)
            if state is None:
                raise StateStoreError(
                    f"state file {self.path} exists but is not a valid "
                    "JSON object; refusing to roll back to the snapshot "
                    f"{self.snapshot_path} automatically — inspect the "
                    "file and restore it deliberately"
                )
            return state
        if self.snapshot_path.exists():
            raise StateStoreError(
                f"state file {self.path} is missing but the snapshot "
                f"{self.snapshot_path} exists; refusing to adopt it "
                "automatically — restore it deliberately if it is the "
                "state you want"
            )
        return {}

    def _write_atomic(self, target: Path, raw: str) -> None:
        tmp = target.with_name(f"{target.name}.tmp.{os.getpid()}")
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)

    def _save_unlocked(self, state: dict) -> None:
        raw = json.dumps(state, ensure_ascii=False)
        self._write_atomic(self.path, raw)
        # The snapshot is part of the save contract: if it cannot be
        # written, the save has failed and the caller must know —
        # otherwise the operator's only restore source silently goes
        # stale.
        self._write_atomic(self.snapshot_path, raw)

    # -- public API ------------------------------------------------------

    @contextmanager
    def transaction(self):
        """Yield the state dict under the exclusive lock; save on success.

        If the body raises, nothing is saved, so a half-applied update
        (e.g. a failed fetch in the middle of ingest) never persists.
        """
        with open(self.lock_path, "a+") as lock_handle:
            fcntl.flock(lock_handle, fcntl.LOCK_EX)
            state = self._load_unlocked()
            try:
                yield state
            except BaseException:
                raise
            self._save_unlocked(state)

    def load(self) -> dict:
        """Read the current state under a shared lock.

        Raises :class:`StateStoreError` when the state on disk is
        corrupt or the main file is missing while a snapshot exists.
        """
        with open(self.lock_path, "a+") as lock_handle:
            fcntl.flock(lock_handle, fcntl.LOCK_SH)
            return self._load_unlocked()
