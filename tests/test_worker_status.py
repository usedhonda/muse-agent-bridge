"""Operator status distinguishes stages without exposing conversation contents."""
import json
import tempfile
import unittest
from bridge.config import Audience, BridgeConfig
from bridge.store import EventStore
from bridge.worker import Worker


def record(event_id, thread='1', user='U_OWNER'):
    return dict(event_id=event_id, channel='D_TEST', ts=thread, thread_ts=thread,
                user=user, text='sensitive test content', scope='owner', audience_id='owner')


class StatusTests(unittest.TestCase):
    def test_counts_and_tail_do_not_claim_or_leak_content(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(directory)
            cfg = BridgeConfig('test', None, [Audience('owner', 'owner', {'U_OWNER'}, set(), True)])
            worker = Worker(cfg, store)
            try:
                for rec in [record('first'), record('blocked'), record('ready', '2'),
                            record('excluded', '3', 'U_OTHER')]:
                    store.append(rec)
                worker.claim()
                with store.queue_path.open('ab') as queue:
                    queue.write(b'{"partial":')
                status = worker.status()
                self.assertEqual(status['counts'], dict(queued=1, blocked=1, ineligible=1,
                    claimed=1, stale_claimed=0, sending=0, done=0))
                self.assertTrue(status['incomplete_queue_tail'])
                self.assertNotIn('sensitive test content', json.dumps(status))
                self.assertNotIn('U_OWNER', json.dumps(status))
                self.assertEqual(len(worker.pending()), 1)
            finally:
                worker.close()

    def test_sending_is_distinct_from_received_and_completed(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(directory)
            store.append(record('first'))
            worker = Worker(BridgeConfig('test', None, [
                Audience('owner', 'owner', {'U_OWNER'}, set(), True)]), store)
            try:
                worker.claim()
                with worker.db:
                    worker.db.execute("UPDATE jobs SET status='sending'")
                self.assertEqual(worker.status()['counts']['sending'], 1)
                self.assertIsNone(worker.claim())
                worker.reconcile('first', '10.1')
                self.assertEqual(worker.status()['counts']['done'], 1)
            finally:
                worker.close()
