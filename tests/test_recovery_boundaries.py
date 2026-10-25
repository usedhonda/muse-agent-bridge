"""Recovery commands must not manufacture jobs for legacy ineligible intake."""
import tempfile
import unittest
from unittest.mock import Mock

from bridge.config import Audience, BridgeConfig
from bridge.store import EventStore
from bridge.worker import Worker


class RecoveryBoundaryTests(unittest.TestCase):
    def test_ineligible_unclaimed_record_and_uncertain_send_survive_recovery_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(directory)
            legacy = dict(event_id='EvLegacy', channel='C_ROOM', ts='100.1',
                          thread_ts='100.1', text='synthetic unresolved work', user='U_OWNER',
                          audience_id='owner', scope='owner', transport='poll')
            current = dict(legacy, event_id='EvCurrent', ts='101.1', thread_ts='90.1',
                           transport='socket', reply_thread_ts='90.1', conversation_ts='90.1',
                           bot_user_id='U_BOT')
            store.append(legacy)
            store.append(current)
            cfg = BridgeConfig('test', None, [Audience('owner', 'owner', {'U_OWNER'}, {'C_ROOM'},
                                True, agent_channels={'C_ROOM': {'U_PEER'}})])
            worker = Worker(cfg, store)
            web = Mock()
            web.auth_test.return_value = "U_BOT"
            web.private_room_matches.return_value = True
            web.post_message.side_effect = TimeoutError()
            try:
                self.assertEqual(worker.claim()['event']['event_id'], 'EvCurrent')
                with self.assertRaises(TimeoutError):
                    worker.reply('EvCurrent', 'synthetic progress', web, context_receipt=worker.context('EvCurrent', web)["receipt"]["id"])
                # Upgrading code does not retroactively repair an older poll
                # record: dedupe still protects its message identity.
                socket_copy = dict(current, event_id='EvSocketLegacy', ts='100.1',
                                   thread_ts='100.1', reply_thread_ts=None, conversation_ts='')
                self.assertFalse(store.append(socket_copy))
                queue_before = store.queue_path.read_bytes()
                jobs_before = [tuple(row) for row in worker.db.execute('SELECT * FROM jobs')]
                operations = [lambda: worker.resume('EvLegacy'), lambda: worker.skip('EvLegacy'),
                              lambda: worker.release_unstarted('EvLegacy'),
                              lambda: worker.reconcile('EvLegacy'),
                              lambda: worker.context('EvLegacy', web),
                              lambda: worker.reply('EvLegacy', 'must not post', web)]
                for operation in operations:
                    with self.assertRaises(ValueError):
                        operation()
                self.assertEqual(store.queue_path.read_bytes(), queue_before)
                self.assertEqual([tuple(row) for row in worker.db.execute('SELECT * FROM jobs')], jobs_before)
                self.assertEqual(worker.status()['counts']['ineligible'], 1)
                self.assertEqual(worker.status()['counts']['sending'], 1)
                self.assertIsNone(worker.claim())
                web.post_message.assert_called_once()
            finally:
                worker.close()
