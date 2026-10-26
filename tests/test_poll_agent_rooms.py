"""Shared poll/Socket normalization preserves privacy, identity and send guards."""
import tempfile
import unittest
from pathlib import Path
from itertools import product
from unittest.mock import Mock, patch

from bridge.config import Audience, BridgeConfig
from bridge.listener import Listener
from bridge.poller import Poller
from bridge.store import EventStore
from bridge.worker import Worker


class PollRoomTests(unittest.TestCase):
    def setup_room(self, directory):
        cfg = BridgeConfig('synthetic', None, [Audience('owner', 'owner', {'U_OWNER'}, {'C_ROOM'},
                          True, agent_channels={'C_ROOM': {'U_PEER'}})])
        store = EventStore(directory)
        web = Mock()
        web.auth_test.return_value = 'U_BOT'
        web.private_room_matches.return_value = True
        web.has_thread.return_value = False
        web.post_message.return_value = '200.1'
        poller = Poller(cfg, store, Path(directory) / 'poller.json')
        poller.web = web
        poller.ensure_dm_channel = Mock(return_value='D_TEST')
        return cfg, store, web, poller, Listener(cfg, store, web, 'U_BOT')

    def test_both_arrival_orders_author_and_thread_preserve_one_eligible_identity(self):
        for poll_first, thread, user in product((True, False), (None, '90.1'), ('U_OWNER', 'U_PEER')):
            with self.subTest(poll_first=poll_first, thread=thread, user=user), tempfile.TemporaryDirectory() as directory:
                cfg, store, web, poller, listener = self.setup_room(directory)
                message = dict(user=user, text='synthetic room text', ts='100.1')
                if user == 'U_PEER':
                    message.update(bot_id='B_PEER', subtype='bot_message')
                if thread:
                    message['thread_ts'] = thread
                envelope = dict(type='events_api', envelope_id='env1', payload=dict(event_id='EvTest',
                                event=dict(type='message', channel='C_ROOM', **message)))
                def history(method, params):
                    self.assertEqual(method, 'conversations.history')
                    return {'messages': [message] if params['channel'] == 'C_ROOM' else []}
                with patch('bridge.poller.api', side_effect=history):
                    if poll_first:
                        self.assertEqual(poller.poll_once(), 1)
                    listener.handle_envelope(envelope, Mock())
                    if not poll_first:
                        self.assertEqual(poller.poll_once(), 0)
                    self.assertEqual(poller.poll_once(), 0)  # inclusive boundary replay
                records = store.read_queue()
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]['transport'], 'poll' if poll_first else 'socket')
                worker = Worker(cfg, store)
                try:
                    row = worker.claim()['event']
                    key = row['event_id']
                    self.assertEqual(key, 'slack:C_ROOM:100.1' if poll_first else 'EvTest')
                    self.assertEqual(row['ts'], '100.1')
                    self.assertEqual(row['thread_ts'], thread or '100.1')
                    self.assertEqual(row['reply_thread_ts'], thread)
                    self.assertEqual(row['conversation_ts'], thread or '')
                    receipt = worker.context(key, web)['receipt']['id']
                    worker.reply(key, 'synthetic reply', web, receipt)
                    web.post_message.assert_called_once_with('C_ROOM', thread, 'synthetic reply')
                    listener.handle_envelope(envelope, Mock())
                    self.assertIsNone(worker.claim())
                    self.assertEqual(len(store.read_queue()), 1)
                    if thread:
                        web.has_thread.assert_not_called()
                finally:
                    worker.close()
                for call in web.private_room_matches.call_args_list:
                    self.assertEqual(call.args, ('C_ROOM', {'U_OWNER', 'U_PEER', 'U_BOT'}))

    def test_unverified_room_body_is_not_retained_by_either_transport(self):
        for poll_first, thread, failure in product((True, False), (None, '90.1'), ('mismatch', 'unavailable')):
            with self.subTest(poll_first=poll_first, thread=thread, failure=failure), tempfile.TemporaryDirectory() as directory:
                _, store, web, poller, listener = self.setup_room(directory)
                web.private_room_matches.return_value = False
                if failure == 'unavailable':
                    web.private_room_matches.side_effect = ConnectionError('synthetic')
                message = dict(type='message', user='U_OWNER', text='rejected synthetic body', ts='100.1')
                if thread:
                    message['thread_ts'] = thread
                def history(method, params):
                    return {'messages': [message] if params['channel'] == 'C_ROOM' else []}
                envelope = dict(type='events_api', envelope_id='env1', payload=dict(event_id='EvTest',
                                event=dict(message, channel='C_ROOM')))
                ack = Mock()
                def socket():
                    if failure == 'unavailable':
                        with self.assertRaises(ConnectionError):
                            listener.handle_envelope(envelope, ack)
                        ack.assert_not_called()
                    else:
                        listener.handle_envelope(envelope, ack)
                def poll():
                    if failure == 'unavailable':
                        with self.assertRaises(ConnectionError):
                            poller.poll_once()
                    else:
                        self.assertEqual(poller.poll_once(), 0)
                with patch('bridge.poller.api', side_effect=history):
                    (poll if poll_first else socket)()
                    (socket if poll_first else poll)()
                self.assertEqual(store.read_queue(), [])
                for path in Path(directory).iterdir():
                    if path.is_file():
                        self.assertNotIn(b'rejected synthetic body', path.read_bytes())
                web.post_message.assert_not_called()
