"""Offline intake durability regression contracts; no Slack/network access.

Failure-path assertions reproduced ACK-before-persistence defects in the
pinned baseline. All HTTP-capable calls are mocked and networking is guarded.
"""
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from bridge.config import Audience, BridgeConfig
from bridge.listener import Listener
from bridge.store import EventStore
from bridge.webapi import SlackWebClient


class IntakeDurabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = EventStore(self.tmp.name)
        audience = Audience(
            'owner', 'owner', {'U_OWNER'}, {'C_ROOM'}, True,
            agent_channels={'C_ROOM': {'U_PEER'}},
        )
        self.cfg = BridgeConfig('offline-regression', None, [audience])
        # Synthetic credential only; every HTTP-capable method is mocked.
        self.web = SlackWebClient('offline-test-surrogate')
        self.web._get = Mock(side_effect=[
            {'ok': True, 'channel': {'is_private': True}},
            {'ok': True, 'members': ['U_OWNER', 'U_PEER', 'U_MUSE']},
        ])
        self.web._post = Mock(side_effect=AssertionError('No HTTP POST expected'))
        self.listener = Listener(self.cfg, self.store, self.web, 'U_MUSE')
        self.acks = []
        self.env = {
            'type': 'events_api', 'envelope_id': 'env-offline-1',
            'payload': {'event_id': 'EvOffline1', 'event': {
                'type': 'message', 'subtype': 'bot_message',
                'bot_id': 'B_PEER', 'user': 'U_PEER', 'channel': 'C_ROOM',
                'channel_type': 'group', 'text': 'synthetic regression input',
                'ts': '100.1',
            }},
        }
        # A hard guard: accidental unmocked HTTP fails locally, never connects.
        guard = patch('urllib.request.urlopen', side_effect=AssertionError('Network prohibited'))
        guard.start()
        self.addCleanup(guard.stop)

    def test_successfully_verified_room_is_queued_and_acked(self):
        self.listener.handle_envelope(self.env, self.acks.append)
        self.assertEqual(self.acks, ['env-offline-1'])
        self.assertEqual([r['event_id'] for r in self.store.read_queue()], ['EvOffline1'])
        self.assertEqual(self.web._get.call_count, 2)

    def test_success_ack_observes_durable_record_and_duplicate_is_acked(self):
        snapshots = []
        def ack(envelope_id):
            snapshots.append((envelope_id, EventStore(self.tmp.name).read_queue()))
        self.listener.handle_envelope(self.env, ack)
        self.web._get.side_effect = None
        self.web.private_room_matches = Mock(return_value=True)
        self.listener.handle_envelope(self.env, ack)
        self.assertEqual(len(snapshots), 2)
        for envelope_id, queue in snapshots:
            self.assertEqual(envelope_id, 'env-offline-1')
            self.assertEqual([r['event_id'] for r in queue], ['EvOffline1'])

    def test_ack_failure_after_save_is_safe_to_redeliver(self):
        with self.assertRaises(ConnectionError):
            self.listener.handle_envelope(self.env, Mock(side_effect=ConnectionError))
        self.assertEqual(len(EventStore(self.tmp.name).read_queue()), 1)
        self.web.private_room_matches = Mock(return_value=True)
        self.listener = Listener(self.cfg, EventStore(self.tmp.name), self.web, 'U_MUSE')
        self.listener.handle_envelope(self.env, self.acks.append)
        self.assertEqual(self.acks, ['env-offline-1'])
        self.assertEqual(len(self.store.read_queue()), 1)

    def test_reaction_failure_occurs_only_after_durable_save_and_ack(self):
        self.cfg.ack_reaction = 'eyes'
        def fail_reaction(*args):
            self.assertEqual(self.acks, ['env-offline-1'])
            self.assertEqual(len(EventStore(self.tmp.name).read_queue()), 1)
            raise RuntimeError('synthetic reaction failure')
        self.web.reactions_add = Mock(side_effect=fail_reaction)
        with self.assertRaisesRegex(RuntimeError, 'synthetic reaction failure'):
            self.listener.handle_envelope(self.env, self.acks.append)
        self.web.reactions_add.assert_called_once()

    def test_definitively_rejected_room_is_acked_without_raw_storage(self):
        self.web._get.side_effect = [{'ok': True, 'channel': {'is_private': False}}]
        self.listener.handle_envelope(self.env, self.acks.append)
        self.assertEqual(self.acks, ['env-offline-1'])
        self.assertEqual(list(self.store.state_dir.iterdir()), [])

    def test_unknown_sender_is_acked_without_http_or_raw_storage(self):
        self.env['payload']['event']['user'] = 'U_UNKNOWN'
        self.listener.handle_envelope(self.env, self.acks.append)
        self.assertEqual(self.acks, ['env-offline-1'])
        self.web._get.assert_not_called()
        self.assertEqual(list(self.store.state_dir.iterdir()), [])

    def test_membership_http_failure_must_not_ack_unpersisted_event(self):
        self.web._get.side_effect = HTTPError(
            'https://invalid.example/offline-only', 503, 'synthetic failure', {}, None)
        with self.assertRaises(HTTPError):
            self.listener.handle_envelope(self.env, self.acks.append)
        self.assertEqual(self.store.read_queue(), [])
        self.assertEqual(list(self.store.state_dir.iterdir()), [])
        self.assertEqual(self.acks, [], 'Unpersisted, unverified event was already ACKed')

    def test_api_failure_is_not_a_definitive_room_rejection(self):
        for failed_method in ('conversations.info', 'conversations.members'):
            with self.subTest(method=failed_method):
                responses = [{'ok': False, 'error': 'synthetic_internal_error'}]
                if failed_method == 'conversations.members':
                    responses.insert(0, {'ok': True, 'channel': {'is_private': True}})
                self.web._get.side_effect = responses
                with self.assertRaises(ConnectionError) as caught:
                    self.listener.handle_envelope(self.env, self.acks.append)
                self.assertNotIn('synthetic_internal_error', str(caught.exception))
                self.assertEqual(self.acks, [])
                self.assertEqual(list(self.store.state_dir.iterdir()), [])
                self.assertEqual(self.web._get.call_args.args[0], failed_method)

    def test_verified_membership_mismatch_is_acked_without_storage(self):
        self.web._get.side_effect = [
            {'ok': True, 'channel': {'is_private': True}},
            {'ok': True, 'members': ['U_OWNER', 'U_PEER', 'U_MUSE', 'U_EXTRA']},
        ]
        self.listener.handle_envelope(self.env, self.acks.append)
        self.assertEqual(self.acks, ['env-offline-1'])
        self.assertEqual(list(self.store.state_dir.iterdir()), [])

    def test_queue_open_failure_must_not_ack_unpersisted_event(self):
        # A directory at the file path reliably produces a real append failure
        # without permission changes or relying on execution user privileges.
        self.store.queue_path.mkdir()
        with self.assertRaises(IsADirectoryError):
            self.listener.handle_envelope(self.env, self.acks.append)
        self.assertFalse(self.store.seen_path.exists())
        self.assertEqual(self.acks, [], 'Queue append failed but event was already ACKed')


if __name__ == '__main__':
    unittest.main()
