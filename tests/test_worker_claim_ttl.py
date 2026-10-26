"""Stale claims must not silence a conversation forever.

A wake turn that dies mid-claim (runtime restart, session replacement)
leaves its job in 'claimed'; per-conversation ordering then blocks
every later message in that conversation. Worker.claim preserves even old claims; operators must verify the old
runtime stopped before deliberately resuming.
"""
import tempfile
import time
import unittest
from pathlib import Path

from bridge.config import Audience, BridgeConfig
from bridge.store import EventStore
from bridge.worker import CLAIM_TTL_SECONDS, Worker


def record(event_id="Ev1"):
    return dict(event_id=event_id, channel="D_TEST", thread_ts="100.1",
                ts="100.1", user="U_TEST_OWNER", text="hello",
                audience_id="owner", scope="owner")


class ClaimTtlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = EventStore(Path(self.tmp.name))
        self.cfg = BridgeConfig("test", None, [
            Audience("owner", "owner", {"U_TEST_OWNER"}, set(), True)])
        self.worker = Worker(self.cfg, self.store)
        self.store.append(record())

    def tearDown(self):
        self.worker.close()
        self.tmp.cleanup()

    def backdate_claim(self, seconds):
        with self.worker.db:
            self.worker.db.execute(
                "UPDATE jobs SET claimed_at=? WHERE event_id='Ev1'",
                (time.time() - seconds,))

    def test_fresh_claim_is_not_released(self):
        self.assertIsNotNone(self.worker.claim())
        self.assertIsNone(self.worker.claim())

    def test_stale_claim_requires_deliberate_recovery(self):
        self.assertIsNotNone(self.worker.claim())
        self.backdate_claim(CLAIM_TTL_SECONDS + 60)
        self.assertIsNone(self.worker.claim())
        self.assertEqual(self.worker.status()["counts"]["stale_claimed"], 1)
        packet = self.worker.resume("Ev1")
        self.assertEqual(self.worker.status()["counts"]["stale_claimed"], 0)
        self.assertIsNotNone(packet)
        self.assertEqual(packet["event"]["event_id"], "Ev1")

    def test_stale_sending_is_not_released(self):
        self.assertIsNotNone(self.worker.claim())
        with self.worker.db:
            self.worker.db.execute(
                "UPDATE jobs SET status='sending', claimed_at=? WHERE event_id='Ev1'",
                (time.time() - CLAIM_TTL_SECONDS - 60,))
        # 'sending' means delivery is uncertain; only reconcile may move it.
        self.assertIsNone(self.worker.claim())
        rows = self.worker.pending()
        self.assertEqual([r["event_id"] for r in rows], ["Ev1"])


if __name__ == "__main__":
    unittest.main()
