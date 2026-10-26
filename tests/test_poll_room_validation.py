"""Poll adapter uses the exact existing bot/private/unshared membership checks."""
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from bridge.config import Audience, BridgeConfig
from bridge.poller import Poller, api, api_get
from bridge.store import EventStore


class PollValidationTests(unittest.TestCase):
    def test_query_and_post_share_lazy_adapter_import_without_real_credentials_or_http(self):
        credentials = types.ModuleType('dynamic_credentials')
        credentials.add_surrogate_to_request = Mock()
        credentials.read_json_response = Mock(return_value={'ok': True})
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.dict(sys.modules, dynamic_credentials=credentials), patch('sys.path', list(sys.path)), \
                patch('bridge.poller.urllib.request.urlopen', return_value=response) as http:
            self.assertEqual(api_get('conversations.members', {'channel': 'C_TEST'}), {'ok': True})
            self.assertEqual(api('auth.test', {}), {'ok': True})
        requests = [call.args[0] for call in http.call_args_list]
        self.assertEqual([request.get_method() for request in requests], ['GET', 'POST'])
        self.assertIn('channel=C_TEST', requests[0].full_url)
        self.assertEqual(credentials.add_surrogate_to_request.call_count, 2)
        for call in credentials.add_surrogate_to_request.call_args_list:
            self.assertEqual(call.kwargs['allowed_hosts'], ['slack.com'])

    def test_private_shared_member_author_and_bot_boundaries_before_persistence(self):
        cases = [dict(is_private=False), dict(is_shared=True), dict(is_ext_shared=True),
                 dict(is_org_shared=True), dict(pending_shared=True), dict(is_archived=True),
                 dict(is_im=True), dict(is_mpim=True), 'extra-member', 'member-page', 'api-error', 'valid']
        for failure in cases:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                store = EventStore(directory)
                cfg = BridgeConfig('test', None, [Audience('owner', 'owner', {'U_OWNER'}, {'C_ROOM'},
                                   True, agent_channels={'C_ROOM': {'U_PEER'}})])
                poller = Poller(cfg, store, Path(directory) / 'poller.json')
                messages = [dict(user=user, ts=str(index) + '.1', text='synthetic body', **fields)
                    for index, (user, fields) in enumerate([
                        ('U_OWNER', {}), ('U_PEER', dict(bot_id='B_PEER', subtype='bot_message')),
                        ('U_UNKNOWN', {}), ('U_BOT', dict(bot_id='B_SELF')),
                        ('U_OWNER', dict(bot_id='B_WRONG')), ('U_PEER', dict(subtype='message_deleted'))], 100)]
                def post(method, params):
                    if method == 'auth.test':
                        return dict(ok=True, user_id='U_BOT', bot_id='B_SELF')
                    if method == 'conversations.open':
                        return dict(channel={'id': 'D_TEST'})
                    if method == 'conversations.history':
                        return dict(messages=messages if params['channel'] == 'C_ROOM' else [])
                    raise AssertionError(method)
                def get(method, params):
                    if failure == 'api-error':
                        raise ConnectionError('synthetic room API failure')
                    if method == 'conversations.info':
                        room = dict(is_private=True)
                        if isinstance(failure, dict):
                            room.update(failure)
                        return dict(ok=True, channel=room)
                    self.assertEqual(method, 'conversations.members')
                    return dict(ok=True, members=['U_OWNER', 'U_PEER', 'U_BOT'] +
                                (['U_UNKNOWN'] if failure == 'extra-member' else []),
                                response_metadata={'next_cursor': 'more'} if failure == 'member-page' else {})
                with patch('bridge.poller.api', side_effect=post), patch('bridge.poller.api_get', side_effect=get):
                    if failure == 'api-error':
                        with self.assertRaises(ConnectionError):
                            poller.poll_once()
                    else:
                        self.assertEqual(poller.poll_once(), 2 if failure == 'valid' else 0)
                if failure == 'valid':
                    self.assertEqual({row['user'] for row in store.read_queue()}, {'U_OWNER', 'U_PEER'})
                    for row in store.read_queue():
                        self.assertIn('reply_thread_ts', row)
                        self.assertEqual(row['bot_user_id'], 'U_BOT')
                else:
                    self.assertEqual(store.read_queue(), [])
                    for path in Path(directory).iterdir():
                        if path.is_file():
                            self.assertNotIn(b'synthetic body', path.read_bytes())

    def test_membership_is_rechecked_for_each_record_not_cached_for_the_page(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(directory)
            cfg = BridgeConfig('test', None, [Audience('owner', 'owner', {'U_OWNER'}, {'C_ROOM'},
                               True, agent_channels={'C_ROOM': {'U_PEER'}})])
            poller = Poller(cfg, store, Path(directory) / 'poller.json')
            poller.web = Mock()
            poller.web.auth_test.return_value = 'U_BOT'
            poller.web.private_room_matches.side_effect = [True, False]
            poller.ensure_dm_channel = Mock(return_value='D_TEST')
            messages = [dict(user='U_OWNER', ts=ts, text='synthetic') for ts in ('100.1', '101.1')]
            with patch('bridge.poller.api', side_effect=lambda method, params:
                       {'messages': messages if params['channel'] == 'C_ROOM' else []}):
                self.assertEqual(poller.poll_once(), 1)
            self.assertEqual(poller.web.private_room_matches.call_count, 2)
            self.assertEqual([row['ts'] for row in store.read_queue()], ['100.1'])

    def test_root_self_thread_metadata_is_normalized_consistently(self):
        from bridge.intake import normalize_event
        cfg = BridgeConfig('test', None, [Audience('owner', 'owner', {'U_OWNER'}, {'C_ROOM'},
                           True, agent_channels={'C_ROOM': {'U_PEER'}})])
        web = Mock()
        web.private_room_matches.return_value = True
        for replies in (0, 1):
            event = dict(type='message', user='U_OWNER', channel='C_ROOM', ts='100.1',
                         thread_ts='100.1', reply_count=replies, text='synthetic')
            poll, _ = normalize_event(cfg, event, 'EvPoll', 'poll', web, 'U_BOT')
            socket, _ = normalize_event(cfg, event, 'EvSocket', 'socket', web, 'U_BOT')
            self.assertEqual(poll['reply_thread_ts'], '100.1' if replies else None)
            for key in ('user', 'ts', 'thread_ts', 'reply_thread_ts', 'conversation_ts', 'bot_user_id'):
                self.assertEqual(poll[key], socket[key])
