"""The same Slack message arriving via two transports must queue once.

The Socket Mode listener records Events API IDs; the history poller
records slack:{channel}:{ts}. Both identify the same message by
(channel, ts), and EventStore dedupes on that pair so a transport
handover (listener down -> poller backfill, or vice versa) never
double-queues a message the worker would then answer twice.
"""
import tempfile
import unittest
from pathlib import Path

from bridge.store import EventStore


def record(event_id, channel="C1", ts="100.0001", text="hi", transport=None):
    rec = {
        "event_id": event_id,
        "audience_id": "owner",
        "scope": "owner",
        "channel": channel,
        "user": "U1",
        "text": text,
        "ts": ts,
        "thread_ts": ts,
    }
    if transport is not None:
        rec["transport"] = transport
    return rec


class CrossTransportDedupeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = EventStore(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_listener_then_poller_same_message(self):
        self.assertTrue(self.store.append(record("Ev123", transport="socket")))
        self.assertFalse(
            self.store.append(record("slack:C1:100.0001", transport="poll")))
        self.assertEqual(len(self.store.read_queue()), 1)

    def test_poller_then_listener_same_message(self):
        self.assertTrue(
            self.store.append(record("slack:C1:100.0001", transport="poll")))
        self.assertFalse(self.store.append(record("Ev123", transport="socket")))
        self.assertEqual(len(self.store.read_queue()), 1)

    def test_untagged_records_keep_event_id_semantics(self):
        # Fixtures and older producers do not name a transport; two such
        # records sharing (channel, ts) are distinct events.
        self.assertTrue(self.store.append(record("Ev123")))
        self.assertTrue(self.store.append(record("Ev124")))
        self.assertEqual(len(self.store.read_queue()), 2)

    def test_different_ts_is_a_different_message(self):
        self.assertTrue(self.store.append(record("Ev123", transport="socket")))
        self.assertTrue(
            self.store.append(record("Ev124", ts="100.0002", transport="poll")))
        self.assertEqual(len(self.store.read_queue()), 2)

    def test_dedupe_survives_reopen(self):
        self.store.append(record("Ev123", transport="socket"))
        reopened = EventStore(Path(self.tmp.name))
        self.assertFalse(
            reopened.append(record("slack:C1:100.0001", transport="poll")))


if __name__ == "__main__":
    unittest.main()
