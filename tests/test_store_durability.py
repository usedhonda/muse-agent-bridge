"""Offline regressions for primary queue / secondary seen-ID write boundaries."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bridge.config import Audience, BridgeConfig
from bridge.listener import Listener
from bridge.store import EventStore


class StoreDurabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = EventStore(self.tmp.name)
        self.record = {'event_id': 'EvOfflineStore', 'text': 'synthetic input'}

    def test_seen_open_failure_dedupes_in_same_process_and_after_restart(self):
        original_open = Path.open
        def open_except_seen(path, *args, **kwargs):
            if path == self.store.seen_path:
                raise OSError('synthetic secondary open failure')
            return original_open(path, *args, **kwargs)
        with patch.object(Path, 'open', open_except_seen):
            with self.assertRaisesRegex(OSError, 'secondary open failure'):
                self.store.append(self.record)
        self.assertEqual(self.store.read_queue(), [self.record])
        self.assertFalse(self.store.append(self.record))
        restarted = EventStore(self.tmp.name)
        self.assertFalse(restarted.append(self.record))
        self.assertEqual(restarted.read_queue(), [self.record])

    def test_seen_fsync_failure_dedupes_in_same_process_and_after_restart(self):
        real_fsync = os.fsync
        calls = []
        def fail_second_sync(fd):
            calls.append(fd)
            if len(calls) == 2:
                raise OSError('synthetic secondary fsync failure')
            return real_fsync(fd)
        with patch('os.fsync', side_effect=fail_second_sync):
            with self.assertRaisesRegex(OSError, 'secondary fsync failure'):
                self.store.append(self.record)
        self.assertFalse(self.store.append(self.record))
        restarted = EventStore(self.tmp.name)
        self.assertFalse(restarted.append(self.record))
        self.assertEqual(restarted.read_queue(), [self.record])

    def test_secondary_failure_propagates_then_redelivery_acks_without_duplicate(self):
        cfg = BridgeConfig('offline', None, [Audience('owner', 'owner', {'U_OWNER'}, dm_enabled=True)])
        listener = Listener(cfg, self.store, Mock(), 'U_MUSE')
        ack = Mock()
        envelope = {'type': 'events_api', 'envelope_id': 'env-offline', 'payload': {
            'event_id': self.record['event_id'], 'event': {
                'type': 'message', 'user': 'U_OWNER', 'channel': 'D_OFFLINE',
                'channel_type': 'im', 'text': 'synthetic input', 'ts': '100.1',
            },
        }}
        original_open = Path.open
        def open_except_seen(path, *args, **kwargs):
            if path == self.store.seen_path:
                raise OSError('synthetic secondary failure')
            return original_open(path, *args, **kwargs)
        with patch.object(Path, 'open', open_except_seen):
            with self.assertRaisesRegex(OSError, 'secondary failure'):
                listener.handle_envelope(envelope, ack)
            ack.assert_not_called()
            listener.handle_envelope(envelope, ack)
        ack.assert_called_once_with('env-offline')
        self.assertEqual(len(self.store.read_queue()), 1)

    def test_queue_open_failure_does_not_mark_seen(self):
        with patch.object(Path, 'open', side_effect=OSError('synthetic primary open failure')):
            with self.assertRaises(OSError):
                self.store.append(self.record)
        self.assertNotIn(self.record['event_id'], self.store._seen)
        self.assertTrue(self.store.append(self.record))
        self.assertEqual(self.store.read_queue(), [self.record])

    def test_queue_fsync_failure_does_not_mark_seen_or_ack(self):
        cfg = BridgeConfig('offline', None, [Audience('owner', 'owner', {'U_OWNER'}, dm_enabled=True)])
        listener = Listener(cfg, self.store, Mock(), 'U_MUSE')
        ack = Mock()
        envelope = {'type': 'events_api', 'envelope_id': 'env-offline', 'payload': {
            'event_id': self.record['event_id'], 'event': {
                'type': 'message', 'user': 'U_OWNER', 'channel': 'D_OFFLINE',
                'channel_type': 'im', 'text': 'synthetic input', 'ts': '100.1',
            },
        }}
        with patch('os.fsync', side_effect=OSError('synthetic primary fsync failure')):
            with self.assertRaisesRegex(OSError, 'primary fsync failure'):
                listener.handle_envelope(envelope, ack)
        ack.assert_not_called()
        self.assertNotIn(self.record['event_id'], self.store._seen)
        self.assertFalse(self.store.seen_path.exists())
        # A failed fsync may leave visible bytes. Their crash durability is
        # unknown: deliberately do not assert exactly-once retry semantics here.


if __name__ == '__main__':
    unittest.main()
