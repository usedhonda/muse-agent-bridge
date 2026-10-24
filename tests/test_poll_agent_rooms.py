"""Unsupported poll intake must not shadow verified Socket Mode agent rooms."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bridge.config import Audience, BridgeConfig
from bridge.listener import Listener
from bridge.poller import Poller
from bridge.store import EventStore
from bridge.worker import Worker


class PollRoomTests(unittest.TestCase):
    def test_poll_excludes_agent_room_in_both_handover_orders(self):
        for poll_first in (True, False):
            with self.subTest(poll_first=poll_first), tempfile.TemporaryDirectory() as directory:
                cfg = BridgeConfig('synthetic', None, [Audience(
                    'owner', 'owner', {'U_OWNER'}, {'C_ROOM'}, True,
                    agent_channels={'C_ROOM': {'U_PEER'}})])
                store = EventStore(directory)
                poller = Poller(cfg, store, Path(directory) / 'poller.json')
                poller.ensure_dm_channel = Mock(return_value='D_TEST')
                web = Mock()
                web.private_room_matches.return_value = True
                listener = Listener(cfg, store, web, 'U_BOT')
                message = dict(user='U_OWNER', text='synthetic room text', ts='100.1')
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
                    self.assertEqual(worker.claim()['event']['event_id'], 'EvTest')
                    worker.skip('EvTest')
                    self.assertIsNone(worker.claim())
                finally:
                    worker.close()

    def test_unverified_room_is_not_retained_by_either_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = BridgeConfig('synthetic', None, [Audience(
                'owner', 'owner', {'U_OWNER'}, {'C_ROOM'}, True,
                agent_channels={'C_ROOM': {'U_PEER'}})])
            store = EventStore(directory)
            poller = Poller(cfg, store, Path(directory) / 'poller.json')
            poller.ensure_dm_channel = Mock(return_value='D_TEST')
            with patch('bridge.poller.api', return_value=dict(messages=[])) as api:
                poller.poll_once()
                self.assertEqual([call.args[1]['channel'] for call in api.call_args_list], ['D_TEST'])
            web = Mock()
            web.private_room_matches.return_value = False
            Listener(cfg, store, web, 'U_BOT').handle_envelope(dict(type='events_api',
                envelope_id='env1', payload=dict(event_id='EvTest', event=dict(type='message',
                    user='U_OWNER', text='synthetic', channel='C_ROOM', ts='100.1'))), Mock())
            self.assertEqual(store.read_queue(), [])
