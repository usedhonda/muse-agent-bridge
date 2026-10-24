"""Offline agent-room sender binding; all identities and API results are fictional."""
import tempfile
import unittest
from unittest.mock import Mock, call

from bridge.config import Audience, BridgeConfig
from bridge.store import EventStore
from bridge.webapi import SlackWebClient
from bridge.worker import Worker


class WorkerIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = EventStore(self.tmp.name)
        cfg = BridgeConfig('test', None, [Audience(
            'owner', 'owner', {'U_OWNER'}, {'C_ROOM'}, True,
            agent_channels={'C_ROOM': {'U_PEER'}})])
        self.worker = Worker(cfg, self.store)
        self.record = dict(event_id='Ev1', audience_id='owner', scope='owner',
                           channel='C_ROOM', user='U_PEER', text='hello',
                           ts='100.1', thread_ts='100.1', reply_thread_ts=None,
                           conversation_ts='', bot_user_id='U_MUSE')
        self.store.append(self.record)
        self.worker.claim()
        self.web = SlackWebClient('fake-test-only')
        self.web._post = Mock(side_effect=self.api_post)
        self.web._get = Mock(side_effect=self.api_get)
        self.identity = {'ok': True, 'bot_id': 'B_MUSE', 'user_id': 'U_MUSE'}

    def tearDown(self):
        self.worker.close()
        self.tmp.cleanup()

    def api_post(self, method, params):
        if method == 'auth.test':
            return self.identity
        if method == 'chat.postMessage':
            return {'ok': True, 'ts': '200.1'}
        raise AssertionError('unexpected API method')

    def api_get(self, method, params):
        if method == 'conversations.info':
            return {'ok': True, 'channel': {'is_private': True}}
        if method == 'conversations.members':
            return {'ok': True, 'members': ['U_OWNER', 'U_PEER', 'U_MUSE']}
        if method == 'conversations.replies':
            return {'ok': True, 'messages': [{'reply_count': 0}]}
        raise AssertionError('unexpected API method')

    def assert_unmodified_claim(self):
        row = self.worker.db.execute(
            'SELECT status, reply, slack_ts FROM jobs WHERE event_id=?', ('Ev1',)
        ).fetchone()
        self.assertEqual(tuple(row), ('claimed', None, None))
        self.web._post.assert_called_once_with('auth.test', {})
        self.web._get.assert_not_called()

    def test_other_room_bot_cannot_reply_as_listener(self):
        self.identity = {'ok': True, 'bot_id': 'B_PEER', 'user_id': 'U_PEER'}
        with self.assertRaises(ValueError) as error:
            self.worker.reply('Ev1', 'answer', self.web)
        self.assertEqual(str(error.exception),
                         'reply bot identity does not match queued listener identity')
        self.assert_unmodified_claim()

    def test_matching_identity_posts_normally(self):
        self.assertEqual(self.worker.reply('Ev1', 'answer', self.web), '200.1')
        self.assertEqual([c.args[0] for c in self.web._post.call_args_list],
                         ['auth.test', 'chat.postMessage'])
        self.assertEqual(self.worker.pending(), [])

    def test_auth_failure_leaves_claimed(self):
        self.web._post.side_effect = ConnectionError('synthetic auth failure')
        with self.assertRaises(ConnectionError):
            self.worker.reply('Ev1', 'answer', self.web)
        self.assert_unmodified_claim()

    def test_human_identity_leaves_claimed(self):
        self.identity = {'ok': True, 'user_id': 'U_OWNER'}
        with self.assertRaisesRegex(RuntimeError, 'bot identity'):
            self.worker.reply('Ev1', 'answer', self.web)
        self.assert_unmodified_claim()

    def test_uncertain_delivery_does_not_reauthenticate_or_resend(self):
        self.web._post.side_effect = [self.identity, TimeoutError()]
        with self.assertRaises(TimeoutError):
            self.worker.reply('Ev1', 'answer', self.web)
        self.assertEqual(self.worker.pending()[0]['status'], 'sending')
        before = list(self.web._post.call_args_list)
        with self.assertRaisesRegex(ValueError, 'uncertain'):
            self.worker.reply('Ev1', 'answer', self.web)
        self.assertEqual(self.web._post.call_args_list, before)

    def test_legacy_dm_does_not_require_absent_listener_identity(self):
        self.store.append(dict(event_id='EvDM', audience_id='owner', scope='owner',
                               channel='D_TEST', user='U_OWNER', text='hello',
                               ts='300.1', thread_ts='300.1'))
        self.assertEqual(self.worker.claim()['event']['event_id'], 'EvDM')
        self.assertEqual(self.worker.reply('EvDM', 'answer', self.web), '200.1')
        self.web._get.assert_not_called()
        self.assertEqual(self.web._post.call_args_list[0], call('auth.test', {}))


if __name__ == '__main__':
    unittest.main()
