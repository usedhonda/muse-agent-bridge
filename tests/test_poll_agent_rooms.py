"""Unsupported poll intake must not shadow verified Socket Mode agent rooms."""
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
    def test_poll_excludes_agent_room_in_both_handover_orders(self):
        for poll_first, thread in product((True, False), (None, "90.1")):
            with self.subTest(poll_first=poll_first, thread=thread), tempfile.TemporaryDirectory() as directory:
                cfg = BridgeConfig('synthetic', None, [Audience(
                    'owner', 'owner', {'U_OWNER'}, {'C_ROOM'}, True,
                    agent_channels={'C_ROOM': {'U_PEER'}})])
                store = EventStore(directory)
                poller = Poller(cfg, store, Path(directory) / 'poller.json')
                poller.ensure_dm_channel = Mock(return_value='D_TEST')
                web = Mock()
                web.private_room_matches.return_value = True
                web.has_thread.return_value = False
                web.post_message.return_value = "200.1"
                listener = Listener(cfg, store, web, 'U_BOT')
                message = dict(user='U_OWNER', text='synthetic room text', ts='100.1')
                if thread is not None:
                    message['thread_ts'] = thread
                envelope = dict(type='events_api', envelope_id='env1', payload=dict(
                    event_id='EvTest', event=dict(type='message', channel='C_ROOM', **message)))

                def history(method, params):
                    self.assertEqual(method, 'conversations.history')
                    self.assertEqual(params['channel'], 'D_TEST')
                    return dict(messages=[])

                with patch('bridge.poller.api', side_effect=history):
                    if poll_first:
                        self.assertEqual(poller.poll_once(), 0)
                        self.assertEqual(store.read_queue(), [])
                    listener.handle_envelope(envelope, Mock())
                    if not poll_first:
                        self.assertEqual(poller.poll_once(), 0)
                web.private_room_matches.assert_called_once_with(
                    'C_ROOM', {'U_OWNER', 'U_PEER', 'U_BOT'})
                self.assertEqual(len(store.read_queue()), 1)
                self.assertEqual(store.read_queue()[0]['transport'], 'socket')
                worker = Worker(cfg, store)
                try:
                    event = worker.claim()['event']
                    self.assertEqual(event['event_id'], 'EvTest')
                    self.assertEqual(event['ts'], '100.1')
                    self.assertEqual(event['thread_ts'], thread or '100.1')
                    self.assertEqual(event['reply_thread_ts'], thread)
                    self.assertEqual(event['conversation_ts'], thread or '')
                    worker.reply('EvTest', 'synthetic reply', web)
                    web.post_message.assert_called_once_with('C_ROOM', thread, 'synthetic reply')
                    if thread is not None:
                        web.has_thread.assert_not_called()
                    listener.handle_envelope(envelope, Mock())  # Socket redelivery too
                    self.assertIsNone(worker.claim())
                    self.assertEqual(len(store.read_queue()), 1)
                    web.post_message.assert_called_once()
                finally:
                    worker.close()

    def test_unverified_room_is_not_retained_by_either_transport(self):
        for poll_first, thread, failure in product((True, False), (None, '90.1'),
                                                  ('mismatch', 'unavailable')):
            with self.subTest(poll_first=poll_first, thread=thread, failure=failure), \
                    tempfile.TemporaryDirectory() as directory:
                cfg = BridgeConfig('synthetic', None, [Audience(
                    'owner', 'owner', {'U_OWNER'}, {'C_ROOM'}, True,
                    agent_channels={'C_ROOM': {'U_PEER'}})])
                store = EventStore(directory)
                poller = Poller(cfg, store, Path(directory) / 'poller.json')
                poller.ensure_dm_channel = Mock(return_value='D_TEST')
                web = Mock()
                web.private_room_matches.return_value = False
                if failure == 'unavailable':
                    web.private_room_matches.side_effect = ConnectionError('synthetic API failure')
                message = dict(type='message', user='U_OWNER', text='synthetic room text',
                               channel='C_ROOM', ts='100.1')
                if thread is not None:
                    message['thread_ts'] = thread
                listener = Listener(cfg, store, web, 'U_BOT')
                envelope = dict(type='events_api', envelope_id='env1',
                                payload=dict(event_id='EvTest', event=message))
                ack = Mock()
                with patch('bridge.poller.api', return_value=dict(messages=[])) as api:
                    if poll_first:
                        poller.poll_once()
                    if failure == 'unavailable':
                        with self.assertRaises(ConnectionError):
                            listener.handle_envelope(envelope, ack)
                        ack.assert_not_called()
                    else:
                        listener.handle_envelope(envelope, ack)
                        ack.assert_called_once_with('env1')
                    if not poll_first:
                        poller.poll_once()
                    self.assertEqual([call.args[1]['channel'] for call in api.call_args_list], ['D_TEST'])
                self.assertEqual(store.read_queue(), [])
                for path in Path(directory).iterdir():
                    if path.is_file():
                        self.assertNotIn(b'synthetic room text', path.read_bytes())
                web.post_message.assert_not_called()
