"""Muse wake-hook interface: claim one owner event, then submit its reply.

No model endpoint is assumed. Muse's existing runtime invokes these commands
on its VM. The reply command accepts text only; routing comes from the claim.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

from .config import load_config
from .store import EventStore


class Worker:
    def __init__(self, cfg, store: EventStore):
        self.cfg = cfg
        self.store = store
        self.db = sqlite3.connect(store.state_dir / "worker.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS jobs (
            event_id TEXT PRIMARY KEY, record TEXT NOT NULL,
            status TEXT NOT NULL, reply TEXT, slack_ts TEXT)""")
        self.db.commit()

    def close(self):
        self.db.close()

    def _allowed(self, record):
        audience = self.cfg.audience_for_user(record.get("user", ""))
        if (audience is None or audience.id != "owner" or audience.scope != "owner"
                or record.get("audience_id") != "owner" or record.get("scope") != "owner"):
            return False  # M4 is not open; never load guest context into Muse.
        channel = record.get("channel", "")
        if not all(isinstance(record.get(k), str) and record[k]
                   for k in ("event_id", "channel", "thread_ts", "ts")):
            return False
        if channel.startswith("D"):
            return audience.dm_enabled
        return channel in audience.channel_allowlist

    def claim(self):
        """Atomically claim one new event. Repeated polls never start it again."""
        records = self.store.read_queue()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            for record in records:
                if not self._allowed(record):
                    continue
                active = [json.loads(row[0]) for row in self.db.execute(
                    "SELECT record FROM jobs WHERE status != 'done'"
                )]
                if any(all(other.get(k) == record.get(k)
                           for k in ("scope", "audience_id", "channel", "thread_ts"))
                       for other in active):
                    continue  # preserve turn order and history within each thread
                inserted = self.db.execute(
                    "INSERT OR IGNORE INTO jobs(event_id, record, status) VALUES (?, ?, 'claimed')",
                    (record["event_id"], json.dumps(record)),
                ).rowcount
                if inserted:
                    return self._packet(record)
        return None

    def _packet(self, record):
        history = []
        for job in self.db.execute("SELECT record, reply FROM jobs WHERE status='done' ORDER BY rowid"):
            previous = json.loads(job["record"])
            if all(previous.get(k) == record.get(k)
                   for k in ("scope", "audience_id", "channel", "thread_ts")):
                history.append({"user": previous["text"], "assistant": job["reply"]})
        return {"event": record, "history": history}

    def pending(self):
        # Metadata only: this is an operator recovery view, not another turn.
        return [dict(row) for row in self.db.execute(
            "SELECT event_id, status, slack_ts FROM jobs WHERE status != 'done' ORDER BY rowid"
        )]

    def resume(self, event_id):
        job = self.db.execute("SELECT * FROM jobs WHERE event_id=?", (event_id,)).fetchone()
        if job is None or job["status"] != "claimed":
            raise ValueError("only an existing claimed turn can be resumed")
        record = json.loads(job["record"])
        if not self._allowed(record):
            raise ValueError("event no longer allowed by current config")
        return self._packet(record)

    def reconcile(self, event_id, sent_ts=None):
        """Operator-only recovery after checking the actual Slack thread.

        A timestamp records confirmed delivery; None explicitly confirms the
        attempted post never arrived and makes a deliberate retry possible.
        """
        with self.db:
            changed = self.db.execute(
                "UPDATE jobs SET status=?, slack_ts=? WHERE event_id=? AND status='sending'",
                ("done" if sent_ts else "claimed", sent_ts, event_id),
            ).rowcount
            if not changed:
                raise ValueError("only an uncertain sending job can be reconciled")

    def reply(self, event_id, text, web):
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise ValueError("reply must contain 1 to 4000 characters")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            job = self.db.execute("SELECT * FROM jobs WHERE event_id=?", (event_id,)).fetchone()
            if job is None:
                raise ValueError("event is not claimed")
            if job["status"] == "done":
                return job["slack_ts"]
            if job["status"] != "claimed":
                raise ValueError("delivery outcome uncertain; reconcile in Slack before retrying")
            record = json.loads(job["record"])
            if not self._allowed(record):
                raise ValueError("event no longer allowed by current config")
            self.db.execute("UPDATE jobs SET status='sending', reply=? WHERE event_id=?", (text, event_id))
        # Persist BEFORE network I/O. A crash/timeout cannot cause auto-resend.
        slack_ts = web.post_message(record["channel"], record["thread_ts"], text)
        with self.db:
            self.db.execute("UPDATE jobs SET status='done', slack_ts=? WHERE event_id=?", (slack_ts, event_id))
        return slack_ts


def main():
    parser = argparse.ArgumentParser(description="Muse queue wake-hook interface")
    parser.add_argument("--config", default=".local/config.yaml")
    parser.add_argument("--state", default=".local/state")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("next", help="claim one new owner event; JSON null if none")
    sub.add_parser("pending", help="show unfinished job metadata without restarting turns")
    reply = sub.add_parser("reply", help="post UTF-8 stdin to the claimed event's thread")
    reply.add_argument("event_id")
    resume = sub.add_parser("resume", help="recover a claimed turn; never creates a new job")
    resume.add_argument("event_id")
    reconcile = sub.add_parser("reconcile", help="operator recovery after verifying Slack delivery")
    reconcile.add_argument("event_id")
    outcome = reconcile.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--sent-ts")
    outcome.add_argument("--confirmed-not-sent", action="store_true")
    args = parser.parse_args()
    worker = Worker(load_config(args.config), EventStore(args.state))
    try:
        if args.action == "next":
            result = worker.claim()
        elif args.action == "pending":
            result = worker.pending()
        elif args.action == "resume":
            result = worker.resume(args.event_id)
        elif args.action == "reconcile":
            worker.reconcile(args.event_id, args.sent_ts)
            result = {"reconciled": True}
        else:
            from .webapi import SlackWebClient
            result = {"slack_ts": worker.reply(args.event_id, sys.stdin.read(),
                                              SlackWebClient(os.environ["SLACK_BOT_TOKEN"]))}
        print(json.dumps(result, ensure_ascii=False))
    finally:
        worker.close()


if __name__ == "__main__":
    main()
