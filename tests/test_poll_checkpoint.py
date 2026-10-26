"""Offline page completeness, checkpoint recovery and concurrent polling."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from bridge.config import Audience, BridgeConfig
from bridge.poller import Poller
from bridge.store import EventStore


def message(ts):
    return dict(user='U_OWNER', ts=ts, text='synthetic page ' + ts)


class PollCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = EventStore(self.tmp.name)
        self.path = Path(self.tmp.name) / 'poller.json'
        self.path.write_text(json.dumps({'channels': {'C_ROOM': '9.999998'}}))
        self.cfg = BridgeConfig('test', None, [Audience('owner', 'owner', {'U_OWNER'}, {'C_ROOM'},
                               True, agent_channels={'C_ROOM': {'U_PEER'}})])
        self.poller = self.make_poller()

    def tearDown(self):
        self.tmp.cleanup()

    def make_poller(self):
        poller = Poller(self.cfg, self.store, self.path)
        poller.web = Mock()
        poller.web.auth_test.return_value = 'U_BOT'
        poller.web.private_room_matches.return_value = True
        poller.ensure_dm_channel = Mock(return_value='D_TEST')
        return poller

    def pages(self, method, params):
        self.assertEqual(method, 'conversations.history')
        if params['channel'] == 'D_TEST':
            return {'messages': []}
        self.assertTrue(params['inclusive'])
        if params.get('cursor'):
            self.assertEqual(params['cursor'], 'page_two')
            return {'messages': [message('9.999999'), message('10.1000')]}
        return {'messages': [message('10.1000')], 'has_more': True,
                'response_metadata': {'next_cursor': 'page_two'}}

    def test_pages_and_inclusive_boundaries_use_exact_numeric_timestamps(self):
        with patch('bridge.poller.api', side_effect=self.pages):
            self.assertEqual(self.poller.poll_once(), 2)
            self.assertEqual(self.poller.poll_once(), 0)
        self.assertEqual([row['ts'] for row in self.store.read_queue()], ['9.999999', '10.1000'])
        self.assertEqual(json.loads(self.path.read_text())['channels']['C_ROOM'], '10.1000')

    def test_page_failure_checkpoints_nothing_and_resume_recovers_all_pages(self):
        original = self.path.read_bytes()
        def partial(method, params):
            if params.get('cursor'):
                raise TimeoutError('synthetic page failure')
            return self.pages(method, params)
        with patch('bridge.poller.api', side_effect=partial):
            with self.assertRaises(TimeoutError):
                self.poller.poll_once()
        self.assertEqual(self.store.read_queue(), [])
        self.assertEqual(self.path.read_bytes(), original)
        with patch('bridge.poller.api', side_effect=self.pages):
            self.assertEqual(self.make_poller().poll_once(), 2)
        self.assertEqual(len(self.store.read_queue()), 2)

    def test_append_failure_and_checkpoint_failure_retry_without_duplicate_identity(self):
        original = self.path.read_bytes()
        append = self.store.append
        count = 0
        def interrupted(record):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError('synthetic storage failure')
            return append(record)
        with patch('bridge.poller.api', side_effect=self.pages), patch.object(self.store, 'append', side_effect=interrupted):
            with self.assertRaises(OSError):
                self.poller.poll_once()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(len(self.store.read_queue()), 1)
        with patch('bridge.poller.api', side_effect=self.pages), patch.object(self.poller, '_save', side_effect=OSError()):
            with self.assertRaises(OSError):
                self.poller.poll_once()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(len(self.store.read_queue()), 2)
        with patch('bridge.poller.api', side_effect=self.pages):
            self.assertEqual(self.make_poller().poll_once(), 0)
        self.assertEqual(len(self.store.read_queue()), 2)

    def test_missing_or_repeating_cursor_holds_watermark_and_does_not_save_bodies(self):
        for result in ({'messages': [message('10.1')], 'has_more': True},
                       {'messages': [message('10.1')], 'response_metadata': {'next_cursor': 'loop'}}):
            original = self.path.read_bytes()
            with self.subTest(result=result), patch('bridge.poller.api', return_value=result):
                with self.assertRaises(ValueError):
                    self.poller.poll_once()
            self.assertEqual(self.path.read_bytes(), original)
            self.assertEqual(self.store.read_queue(), [])

    def test_concurrent_pollers_reload_checkpoint_under_shared_lock(self):
        second = self.make_poller()  # initially holds the same old checkpoint
        entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
        calls, results, errors = [], [], []
        def history(method, params):
            if params['channel'] == 'D_TEST':
                return {'messages': []}
            calls.append(params['oldest'])
            if len(calls) == 1:
                entered.set()
                if not release.wait(5):
                    raise AssertionError('test barrier timed out')
            return {'messages': [message('10.1000')]}
        def run(poller, started=None):
            if started:
                started.set()
            try:
                results.append(poller.poll_once())
            except Exception as error:
                errors.append(error)
        first_thread = threading.Thread(target=run, args=(self.poller,))
        second_thread = threading.Thread(target=run, args=(second, second_started))
        with patch('bridge.poller.api', side_effect=history):
            first_thread.start()
            try:
                self.assertTrue(entered.wait(5))
                second_thread.start()
                self.assertTrue(second_started.wait(5))
            finally:
                release.set()
                first_thread.join(5)
                if second_thread.ident:
                    second_thread.join(5)
        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), [0, 1])
        self.assertEqual(calls, ['9.999998', '10.1000'])
        self.assertEqual(len(self.store.read_queue()), 1)

    def test_completed_boundary_is_not_reinterpreted_as_legacy_rescue(self):
        self.path.write_text(json.dumps({'channels': {'C_ROOM': '10.1000'}}))
        with patch('bridge.poller.api', side_effect=lambda method, params:
                   {'messages': [message('10.1000')] if params['channel'] == 'C_ROOM' else []}):
            self.assertEqual(self.poller.poll_once(), 0)
        self.assertEqual(self.store.read_queue(), [])
        self.poller.web.private_room_matches.assert_not_called()
