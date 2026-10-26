"""Fail-closed context receipts and atomic send boundaries, entirely offline."""
import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from pathlib import Path

from bridge.config import Audience, BridgeConfig
from bridge.store import EventStore
from bridge.worker import Worker, CONTEXT_TTL_SECONDS, main
from bridge.recovery import plan


def event(key='EvOld', ts='100.1', **changes):
    return dict(dict(event_id=key, ts=ts, thread_ts=ts, text='synthetic original',
                     channel='C_ROOM', user='U_OWNER', scope='owner', audience_id='owner',
                     reply_thread_ts=None, conversation_ts='', bot_user_id='U_BOT', transport='socket'), **changes)


class SendGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = EventStore(self.tmp.name)
        self.cfg = BridgeConfig('test', None, [Audience('owner', 'owner', {'U_OWNER'},
            {'C_ROOM', 'C_OTHER'}, True, agent_channels={'C_ROOM': {'U_PEER'}, 'C_OTHER': {'U_PEER'}})])
        self.worker = Worker(self.cfg, self.store)
        self.web = Mock()
        self.web.auth_test.return_value = 'U_BOT'
        self.web.private_room_matches.return_value = True
        self.web.has_thread.return_value = False
        self.web.post_message.return_value = '200.1'
        self.store.append(event())
        self.worker.claim()

    def tearDown(self):
        self.worker.close()
        self.tmp.cleanup()

    def receipt(self):
        return self.worker.context('EvOld', self.web)['receipt']['id']

    def reject(self, receipt=None):
        with self.assertRaises(ValueError):
            self.worker.reply('EvOld', 'synthetic answer', self.web, receipt)
        self.assertEqual(self.worker.pending()[0]['status'], 'claimed')
        self.web.post_message.assert_not_called()

    def test_absent_unknown_wrong_event_stale_or_future_receipt_refused(self):
        self.reject()
        self.reject(12345)
        receipt = self.receipt()
        with self.worker.db:
            self.worker.db.execute('UPDATE context_receipts SET event_id=?', ('EvDifferent',))
        self.reject(receipt)
        for age in (CONTEXT_TTL_SECONDS + 1, -10):
            receipt = self.receipt()
            with self.worker.db:
                self.worker.db.execute('UPDATE context_receipts SET fetched_at=fetched_at-? WHERE receipt_id=?',
                                       (age, receipt))
            self.reject(receipt)

    def test_failed_refresh_invalidates_older_success_preserves_job(self):
        receipt = self.receipt()
        self.web.auth_test.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            self.worker.context('EvOld', self.web)
        self.web.auth_test.side_effect = None
        self.reject(receipt)
        self.worker.reply('EvOld', 'reconsidered answer', self.web, self.receipt())
        self.web.post_message.assert_called_once()

    def test_later_correction_and_cancellation_require_new_receipt_without_consuming(self):
        for key, text in [('EvCorrection', 'synthetic correction'), ('EvCancel', 'synthetic cancellation')]:
            receipt = self.receipt()
            self.store.append(event(key, '101.1' if key == 'EvCorrection' else '102.1', text=text))
            self.reject(receipt)
            snapshot = self.worker.context('EvOld', self.web)
            self.assertIn(text, [row['text'] for row in snapshot['events']])
            self.assertEqual(self.worker.pending()[0]['status'], 'claimed')
            self.assertEqual(self.worker.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 1)
        self.worker.reply('EvOld', 'original work retained and cancellation considered', self.web, snapshot['receipt']['id'])
        self.assertEqual(self.worker.status()['counts']['queued'], 2)

    def test_unrelated_room_and_thread_do_not_invalidate(self):
        receipt = self.receipt()
        self.store.append(event('EvOtherRoom', '101.1', channel='C_OTHER'))
        self.store.append(event('EvThread', '102.1', thread_ts='90.1', reply_thread_ts='90.1', conversation_ts='90.1'))
        self.worker.reply('EvOld', 'answer', self.web, receipt)
        self.web.post_message.assert_called_once()

    def test_original_changed_or_thread_appeared_refuses(self):
        receipt = self.receipt()
        self.web.has_thread.return_value = True
        self.reject(receipt)
        self.web.has_thread.return_value = False
        row = event(text='changed original')
        self.store.queue_path.write_text(json.dumps(row) + '\n')
        self.reject(receipt)

    def test_parallel_sender_and_dryrun_observe_uncertain_state_without_second_send(self):
        receipt = self.receipt()
        entered, finish = threading.Event(), threading.Event()
        errors = []
        def post(*args):
            entered.set()
            if not finish.wait(5):
                raise AssertionError('test barrier timeout')
            raise TimeoutError('synthetic unknown delivery')
        self.web.post_message.side_effect = post
        def send():
            worker = Worker(self.cfg, EventStore(self.tmp.name))
            try:
                worker.reply('EvOld', 'answer', self.web, receipt)
            except Exception as error:
                errors.append(error)
            finally:
                worker.close()
        thread = threading.Thread(target=send)
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            before = self.store.queue_path.read_bytes()
            self.assertEqual(plan(self.tmp.name, 'EvOld')['action'], 'hold_uncertain_send')
            with self.assertRaisesRegex(ValueError, 'uncertain'):
                self.worker.reply('EvOld', 'second answer', self.web, receipt)
            self.assertEqual(self.store.queue_path.read_bytes(), before)
        finally:
            finish.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual([type(error) for error in errors], [TimeoutError])
        with self.assertRaises(ValueError):
            self.worker.context('EvOld', self.web)
        with self.assertRaisesRegex(ValueError, 'uncertain'):
            self.worker.reply('EvOld', 'retry', self.web, receipt)
        self.web.post_message.assert_called_once()
        self.assertEqual(self.worker.pending()[0]['status'], 'sending')

    def test_dryrun_is_read_only_missing_metadata_and_done_are_not_replayed(self):
        legacy = event('EvLegacy', '105.1')
        del legacy['reply_thread_ts']
        del legacy['bot_user_id']
        self.store.append(legacy)
        before = {path.name: path.read_bytes() for path in Path(self.tmp.name).iterdir() if path.is_file()}
        result = plan(self.tmp.name, 'EvOld')
        self.assertEqual(result['action'], 'hold_existing_claim')
        self.assertFalse(result['apply_supported'])
        self.assertFalse(result['send_supported'])
        self.assertNotIn('text', result)
        legacy_plan = plan(self.tmp.name, 'EvLegacy')
        self.assertEqual(legacy_plan['missing_room_metadata'], ['reply_thread_ts', 'bot_user_id'])
        self.assertEqual(legacy_plan['job_state'], None)
        self.assertEqual(before, {path.name: path.read_bytes() for path in Path(self.tmp.name).iterdir() if path.is_file()})
        self.worker.reply('EvOld', 'answer', self.web, self.receipt())
        self.assertEqual(plan(self.tmp.name, 'EvOld')['action'], 'verify_delivery_do_not_resend')
        self.web.post_message.assert_called_once()

    def test_different_event_id_for_same_message_cannot_send_twice(self):
        # Simulate historical transportless duplicate rows; no rescue mutation.
        duplicate = event('EvAlias', transport=None)
        self.store.append(duplicate)
        self.worker.reply('EvOld', 'first', self.web, self.receipt())
        self.assertEqual(self.worker.claim()['event']['event_id'], 'EvAlias')
        alias_receipt = self.worker.context('EvAlias', self.web)['receipt']['id']
        with self.assertRaisesRegex(ValueError, 'same message'):
            self.worker.reply('EvAlias', 'duplicate', self.web, alias_receipt)
        self.web.post_message.assert_called_once()

    def test_resume_and_explicit_non_delivery_require_new_context(self):
        receipt = self.receipt()
        self.worker.resume('EvOld')
        self.reject(receipt)
        receipt = self.receipt()
        self.web.post_message.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            self.worker.reply('EvOld', 'answer', self.web, receipt)
        self.worker.reconcile('EvOld')
        self.web.post_message.reset_mock()
        self.web.post_message.side_effect = None
        self.reject(receipt)
        self.worker.reply('EvOld', 'reconsidered', self.web, self.receipt())
        self.web.post_message.assert_called_once()

    def test_claim_generation_change_during_network_preflight_refuses(self):
        receipt = self.receipt()
        def resumed(*args):
            self.worker.resume('EvOld')
            return True
        self.web.private_room_matches.side_effect = resumed
        self.reject(receipt)
        self.web.private_room_matches.side_effect = None
        self.web.auth_test.side_effect = lambda: (self.worker.resume('EvOld'), 'U_BOT')[1]
        with self.assertRaisesRegex(ValueError, 'claim or context attempt changed'):
            self.worker.context('EvOld', self.web)
        self.web.post_message.assert_not_called()

    def test_saved_routing_metadata_mutation_is_original_mismatch(self):
        receipt = self.receipt()
        self.store.queue_path.write_text(json.dumps(event(conversation_ts='90.1', reply_thread_ts='90.1')) + '\n')
        self.reject(receipt)

    def test_reply_cli_passes_exact_receipt_to_guard(self):
        receipt = self.receipt()
        output = io.StringIO()
        argv = ['worker', '--state', self.tmp.name, 'reply', 'EvOld', '--context-receipt', str(receipt)]
        with patch('sys.argv', argv), patch('sys.stdin', io.StringIO('synthetic answer')), \
                patch.dict(os.environ, SLACK_BOT_TOKEN='synthetic'), \
                patch('bridge.worker.load_config', return_value=self.cfg), \
                patch('bridge.webapi.SlackWebClient', return_value=self.web), \
                contextlib.redirect_stdout(output):
            main()
        self.assertEqual(json.loads(output.getvalue()), {'slack_ts': '200.1'})
        self.web.post_message.assert_called_once_with('C_ROOM', None, 'synthetic answer')

    def test_dryrun_without_existing_lock_or_database_creates_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            queue = Path(directory) / 'queue.jsonl'
            queue.write_text(json.dumps(event()) + '\n')
            before = queue.read_bytes()
            result = plan(directory, 'EvOld')
            self.assertFalse(result['coordinated_snapshot'])
            self.assertIsNone(result['job_state'])
            self.assertEqual(list(Path(directory).iterdir()), [queue])
            self.assertEqual(queue.read_bytes(), before)
