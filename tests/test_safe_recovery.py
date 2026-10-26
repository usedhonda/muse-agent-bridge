"""Recovery must not start an active long turn twice or race intake writers."""
import multiprocessing
import tempfile
import time
import unittest

from bridge.config import Audience, BridgeConfig
from bridge.store import EventStore
from bridge.worker import Worker


def event(event_id, transport='socket'):
    return dict(event_id=event_id, channel='D_TEST', ts='100.1', thread_ts='100.1',
                user='U_OWNER', text='private synthetic text', scope='owner',
                audience_id='owner', transport=transport)


def append_after_barrier(directory, event_id, transport, barrier, results):
    store = EventStore(directory)
    barrier.wait(timeout=10)
    results.put(store.append(event(event_id, transport)))


class SafeRecoveryTests(unittest.TestCase):
    def test_long_running_turn_survives_another_worker_poll(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(directory)
            store.append(event('Ev1'))
            cfg = BridgeConfig('test', None, [Audience('owner', 'owner', {'U_OWNER'}, set(), True)])
            first = Worker(cfg, store)
            second = Worker(cfg, store)
            try:
                first.claim()
                with first.db:
                    first.db.execute('UPDATE jobs SET claimed_at=?', (time.time() - 3600,))
                self.assertIsNone(second.claim())
                self.assertEqual(first.resume('Ev1')['event']['event_id'], 'Ev1')
            finally:
                first.close()
                second.close()

    def test_two_already_open_processes_queue_one_handover_message(self):
        ctx = multiprocessing.get_context('spawn')
        with tempfile.TemporaryDirectory() as directory:
            barrier = ctx.Barrier(2)
            results = ctx.Queue()
            children = [ctx.Process(target=append_after_barrier,
                        args=(directory, event_id, transport, barrier, results))
                        for event_id, transport in [('Ev1', 'socket'), ('poll:D_TEST:100.1', 'poll')]]
            for child in children:
                child.start()
            try:
                for child in children:
                    child.join(timeout=15)
                    self.assertEqual(child.exitcode, 0)
                self.assertEqual(sorted([results.get(timeout=2), results.get(timeout=2)]), [False, True])
                self.assertEqual(len(EventStore(directory).read_queue()), 1)
            finally:
                for child in children:
                    if child.is_alive():
                        child.terminate()
                        child.join()
                results.close()

    def test_two_existing_instances_refresh_event_id_dedupe(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second = EventStore(directory), EventStore(directory)
            self.assertTrue(first.append(event('Ev1')))
            self.assertFalse(second.append(event('Ev1')))

    def test_dispatcher_can_retry_confirmed_unstarted_but_not_uncertain_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(directory)
            store.append(event('Ev1'))
            cfg = BridgeConfig('test', None, [Audience('owner', 'owner', {'U_OWNER'}, set(), True)])
            worker = Worker(cfg, store)
            try:
                worker.claim()
                worker.release_unstarted('Ev1')
                self.assertEqual(worker.status()['counts']['queued'], 1)
                self.assertEqual(worker.claim()['event']['event_id'], 'Ev1')
                with worker.db:
                    worker.db.execute("UPDATE jobs SET status='sending'")
                with self.assertRaises(ValueError):
                    worker.release_unstarted('Ev1')
                self.assertIsNone(worker.claim())
            finally:
                worker.close()
