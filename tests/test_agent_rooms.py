"""Private-room admission, identity, routing and history contracts (offline)."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from bridge.config import Audience, BridgeConfig, load_config
from bridge.listener import Listener
from bridge.store import EventStore
from bridge.worker import Worker
from bridge.webapi import SlackWebClient


class AgentRoomTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.audience = Audience('owner', 'owner', {'U_OWNER'}, {'C_ROOM', 'C_OTHER'}, True,
                                 agent_channels={'C_ROOM': {'U_PEER', 'U_SECOND'}})
        self.cfg = BridgeConfig('test', None, [self.audience])
        self.store = EventStore(self.tmp.name)
        self.web = Mock()
        self.web.auth_test.return_value = 'U_MUSE'
        self.web.private_room_matches.return_value = True
        self.web.has_thread.return_value = False
        self.web.post_message.return_value = '300.1'
        self.listener = Listener(self.cfg, self.store, self.web, 'U_MUSE')
        self.worker = Worker(self.cfg, self.store)

    def tearDown(self):
        self.worker.close()
        self.tmp.cleanup()

    def intake(self, key='Ev1', **changes):
        event = dict(type='message', user='U_PEER', bot_id='B_PEER', subtype='bot_message',
                     channel='C_ROOM', channel_type='group', text='Muse, what do you think?', ts='100.1')
        event.update(changes)
        self.listener.handle_envelope(dict(type='events_api', envelope_id=key, payload={
            'event_id': key, 'event': event}), Mock())

    def test_natural_agent_conversation_and_top_level_reply(self):
        self.intake()
        packet = self.worker.claim()
        self.assertEqual(packet['event']['user'], 'U_PEER')
        self.assertIn('not new owner authorization', packet['conversation_policy'])
        self.worker.reply('Ev1', 'A useful answer', self.web)
        self.web.post_message.assert_called_once_with('C_ROOM', None, 'A useful answer')
        self.web.private_room_matches.assert_called_with(
            'C_ROOM', {'U_OWNER', 'U_PEER', 'U_SECOND', 'U_MUSE'})

    def test_existing_thread_stays_in_place(self):
        self.intake(thread_ts='90.1')
        self.worker.claim()
        self.worker.reply('Ev1', 'In this thread', self.web)
        self.web.post_message.assert_called_once_with('C_ROOM', '90.1', 'In this thread')
        self.web.has_thread.assert_not_called()

    def test_thread_created_after_intake_is_used(self):
        self.intake()
        self.worker.claim()
        self.web.has_thread.return_value = True
        self.worker.reply('Ev1', 'Thread appeared', self.web)
        self.web.post_message.assert_called_once_with('C_ROOM', '100.1', 'Thread appeared')

    def test_unknown_self_other_room_edits_and_agent_dms_rejected(self):
        for n, change in enumerate([
            {'user': 'U_UNKNOWN'}, {'user': 'U_MUSE'},
            {'channel': 'C_OTHER'}, {'channel': 'D_DM', 'channel_type': 'im'},
            {'channel_type': 'mpim'}, {'subtype': 'message_changed'},
            {'user': 'U_OWNER'},  # a bot payload cannot impersonate the owner
        ]):
            self.intake('Ev' + str(n), **change)
        self.assertEqual(self.store.read_queue(), [])

    def test_membership_gate_both_before_intake_and_send(self):
        self.web.private_room_matches.return_value = False
        self.intake()
        self.assertEqual(self.store.read_queue(), [])
        self.web.private_room_matches.return_value = True
        self.intake()
        self.worker.claim()
        self.web.private_room_matches.return_value = False
        with self.assertRaisesRegex(ValueError, 'membership changed'):
            self.worker.reply('Ev1', 'no', self.web)
        self.web.post_message.assert_not_called()
        self.assertEqual(self.worker.pending()[0]['status'], 'claimed')

    def test_room_context_order_and_dm_thread_isolation(self):
        self.intake()
        self.intake('Ev2', user='U_SECOND', ts='101.1')
        self.intake('Ev3', ts='102.1', thread_ts='90.1')
        self.assertEqual(self.worker.claim()['event']['event_id'], 'Ev1')
        self.assertEqual(self.worker.claim()['event']['event_id'], 'Ev3')
        self.assertIsNone(self.worker.claim())
        self.worker.reply('Ev1', 'first', self.web)
        packet = self.worker.claim()
        self.assertEqual(packet['event']['event_id'], 'Ev2')
        self.assertEqual(packet['history'], [{'user': 'Muse, what do you think?',
                                             'assistant': 'first', 'sender': 'U_PEER'}])
        self.assertEqual(self.worker.resume('Ev3')['history'], [])

    def test_agent_revocation_and_room_removal_rechecked(self):
        self.intake()
        self.worker.claim()
        self.audience.agent_channels['C_ROOM'].remove('U_PEER')
        with self.assertRaisesRegex(ValueError, 'no longer allowed'):
            self.worker.reply('Ev1', 'no', self.web)
        self.web.post_message.assert_not_called()

    def test_skip_acknowledgement_releases_room_without_reply(self):
        self.intake(text='Thanks, confirmed.')
        self.intake('Ev2', ts='101.1')
        self.worker.claim()
        self.worker.skip('Ev1')
        packet = self.worker.claim()
        self.assertEqual(packet['event']['event_id'], 'Ev2')
        self.assertEqual(packet['history'], [])
        self.web.post_message.assert_not_called()

    def test_malformed_room_record_is_rejected_without_key_error(self):
        self.intake()
        record = self.store.read_queue()[0]
        del record['thread_ts']
        self.assertFalse(self.worker._allowed(record))


class ApiAndConfigTests(unittest.TestCase):
    def test_top_level_omits_thread_and_never_impersonates_owner(self):
        with self.assertRaises(ValueError):
            SlackWebClient('xoxp-test-only')
        web = SlackWebClient('xoxb-test-only')
        web._post = Mock(return_value={'ok': True, 'ts': '200.1', 'bot_id': 'B_TEST', 'user_id': 'U_TEST_BOT'})
        web.post_message('C_ROOM', None, 'hello')
        params = web._post.call_args.args[1]
        for key in ('thread_ts', 'as_user', 'username', 'icon_url'):
            self.assertNotIn(key, params)
        self.assertFalse(params['reply_broadcast'])

    def test_user_identity_cannot_post_even_with_opaque_credential(self):
        web = SlackWebClient('opaque-test-surrogate')
        web._post = Mock(return_value={'ok': True, 'user_id': 'U_OWNER'})
        with self.assertRaisesRegex(RuntimeError, 'bot identity'):
            web.post_message('C_ROOM', None, 'do not send')
        web._post.assert_called_once_with('auth.test', {})

    def test_private_exact_membership_fails_closed(self):
        web = SlackWebClient('xoxb-test-only')
        for room, members in [
            ({'is_private': True}, {'ok': True, 'members': ['U_OWNER', 'U_AGENT']}),
            ({'is_private': False}, {}),
            ({'is_private': True, 'is_shared': True}, {}),
            ({'is_private': True, 'is_mpim': True}, {}),
            ({'is_private': True}, {'ok': True, 'members': ['U_OWNER', 'U_EXTRA']}),
            ({'is_private': True}, {'ok': True, 'members': ['U_OWNER', 'U_AGENT'],
                                  'response_metadata': {'next_cursor': 'more'}}),
        ]:
            web._get = Mock(side_effect=[{'ok': True, 'channel': room}, members])
            expected = room == {'is_private': True} and members == {
                'ok': True, 'members': ['U_OWNER', 'U_AGENT']}
            self.assertEqual(web.private_room_matches('C_ROOM', {'U_OWNER', 'U_AGENT'}), expected)

    def test_room_config_restricts_owner_audience_and_allowlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.yaml'
            base = ('audiences:\n- id: owner\n  scope: owner\n  userIds: [U_OWNER]\n'
                    '  channelAllowlist: [C_ROOM]\n  agentChannels: {C_ROOM: [U_PEER]}\n')
            path.write_text(base)
            self.assertEqual(load_config(path).audience_for_sender('U_PEER', 'C_ROOM').id, 'owner')
            for bad in (base.replace('[C_ROOM]', '[C_OTHER]'),
                        base.replace('scope: owner', 'scope: public'),
                        base.replace('[U_PEER]', 'U_PEER')):
                path.write_text(bad)
                with self.assertRaises(ValueError):
                    load_config(path)


if __name__ == '__main__':
    unittest.main()
