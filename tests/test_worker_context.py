"""Delayed replies refresh bounded context without consuming or replaying jobs."""
import json
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import Mock

from bridge.config import Audience, BridgeConfig
from bridge.store import EventStore
from bridge.worker import Worker, CONTEXT_MAX_EVENTS, CONTEXT_MAX_TEXT_CHARS


def event(event_id, ts, text, channel='C_ROOM', thread=None, user='U_OWNER'):
    return dict(event_id=event_id, ts=ts, text=text, channel=channel, user=user,
                thread_ts=thread or ts, reply_thread_ts=thread,
                conversation_ts=thread or '', bot_user_id='U_BOT',
                transport='socket', scope='owner', audience_id='owner',
                received_at='2026-01-01T00:00:00+00:00')


class WorkerContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = EventStore(self.tmp.name)
        self.audience = Audience('owner', 'owner', {'U_OWNER'}, {'C_ROOM', 'C_OTHER'}, True,
                                agent_channels={'C_ROOM': {'U_PEER'}, 'C_OTHER': {'U_PEER'}})
        self.worker = Worker(BridgeConfig('test', None, [self.audience]), self.store)
        self.web = Mock()
        self.web.auth_test.return_value = 'U_BOT'
        self.web.private_room_matches.return_value = True
        self.web.has_thread.return_value = False
        self.web.post_message.return_value = '200.1'

    def tearDown(self):
        self.worker.close()
        self.tmp.cleanup()

    def states(self):
        return [tuple(row) for row in self.worker.db.execute(
            'SELECT event_id, status, reply, slack_ts, claimed_at FROM jobs ORDER BY rowid')]

    def test_later_correction_arriving_after_claim_is_visible_before_reply_without_job_changes(self):
        self.store.append(event('EvOld', '100.1', 'original unresolved work'))
        packet = self.worker.claim()
        self.assertEqual(packet['queue_context']['newer_count'], 0)
        self.assertTrue(packet['queue_context']['refresh_before_delayed_reply'])
        correction = event('EvCorrection', '101.1', 'synthetic correction', user='U_PEER')
        self.store.append(correction)
        before = self.states()
        snapshot = self.worker.context('EvOld', self.web)
        self.assertEqual(self.states(), before)
        self.assertEqual(self.worker.status()['counts']['blocked'], 1)
        self.assertTrue(snapshot['read_only'])
        row = snapshot['events'][0]
        for key in ('event_id', 'user', 'ts', 'thread_ts', 'received_at', 'transport', 'text'):
            self.assertEqual(row[key], correction[key])
        self.assertEqual(row['provenance'], 'local_durable_queue')
        self.assertEqual(row['job_state'], 'queued')
        self.assertNotIn('synthetic correction', json.dumps(packet))
        self.worker.reply('EvOld', 'progress updated for correction; original work retained', self.web)
        self.assertEqual(self.worker.claim()['event']['event_id'], 'EvCorrection')
        self.web.post_message.assert_called_once()

    def test_other_rooms_threads_old_timestamps_and_revoked_senders_are_excluded(self):
        self.store.append(event('EvOld', '100.1', 'original'))
        self.worker.claim()
        for row in [event('EvOtherRoom', '101.1', 'other room', channel='C_OTHER'),
                    event('EvOtherThread', '102.1', 'other thread', thread='90.1'),
                    event('EvEarlier', '99.1', 'late arrival with older timestamp'),
                    event('EvUnknown', '103.1', 'not allowed', user='U_UNKNOWN'),
                    event('EvSame', '104.1', 'same room correction')]:
            self.store.append(row)
        snapshot = self.worker.context('EvOld', self.web)
        self.assertEqual([row['event_id'] for row in snapshot['events']], ['EvSame'])
        self.assertEqual(self.worker.pending()[0]['status'], 'claimed')
        self.store.append(event('EvPeer', '105.1', 'previously permitted peer', user='U_PEER'))
        self.worker.cfg = replace(self.worker.cfg, audiences=[replace(self.audience,
            agent_channels={'C_ROOM': {'U_REPLACEMENT'}, 'C_OTHER': {'U_PEER'}})])
        snapshot = self.worker.context('EvOld', self.web)
        self.assertEqual([row['event_id'] for row in snapshot['events']], ['EvSame'])
        self.web.private_room_matches.assert_called_with('C_ROOM', {'U_OWNER', 'U_REPLACEMENT', 'U_BOT'})

    def test_thread_refresh_excludes_room_top_level_and_other_threads(self):
        self.store.append(event('EvOld', '100.1', 'thread original', thread='90.1'))
        self.worker.claim()
        for row in [event('EvTop', '101.1', 'top level'),
                    event('EvOther', '102.1', 'different thread', thread='91.1'),
                    event('EvSame', '103.1', 'thread correction', thread='90.1')]:
            self.store.append(row)
        snapshot = self.worker.context('EvOld', self.web)
        self.assertEqual([row['event_id'] for row in snapshot['events']], ['EvSame'])
        self.assertEqual(snapshot['events'][0]['thread_ts'], '90.1')
        self.worker.reply('EvOld', 'thread progress', self.web)
        self.web.post_message.assert_called_once_with('C_ROOM', '90.1', 'thread progress')

    def test_limits_choose_latest_context_and_label_clipped_text(self):
        self.store.append(event('EvOld', '100.1', 'original'))
        self.worker.claim()
        for index in range(CONTEXT_MAX_EVENTS + 2):
            self.store.append(event('EvNew' + str(index), str(101 + index) + '.1',
                                    'x' * (CONTEXT_MAX_TEXT_CHARS + 1)))
        snapshot = self.worker.context('EvOld', self.web)
        self.assertEqual(snapshot['newer_count'], CONTEXT_MAX_EVENTS + 2)
        self.assertEqual(snapshot['omitted_older_count'], 2)
        self.assertEqual(len(snapshot['events']), CONTEXT_MAX_EVENTS)
        self.assertEqual(snapshot['events'][0]['event_id'], 'EvNew2')
        self.assertTrue(all(row['text_truncated'] for row in snapshot['events']))
        self.assertTrue(all(len(row['text']) == CONTEXT_MAX_TEXT_CHARS for row in snapshot['events']))
        self.assertEqual(len(self.states()), 1)

    def test_membership_identity_and_current_config_fail_closed_without_state_changes(self):
        self.store.append(event('EvOld', '100.1', 'original'))
        self.worker.claim()
        self.store.append(event('EvNew', '101.1', 'protected correction'))
        before = self.states()
        for failure in ('wrong-bot', 'membership', 'api-error', 'revoked'):
            with self.subTest(failure=failure):
                self.web.auth_test.return_value = 'U_WRONG' if failure == 'wrong-bot' else 'U_BOT'
                self.web.private_room_matches.return_value = failure != 'membership'
                self.web.private_room_matches.side_effect = ConnectionError() if failure == 'api-error' else None
                self.worker.cfg = replace(self.worker.cfg, audiences=[] if failure == 'revoked' else [self.audience])
                with self.assertRaises((ValueError, ConnectionError)):
                    self.worker.context('EvOld', self.web)
                self.assertEqual(self.states(), before)
        self.web.post_message.assert_not_called()

    def test_uncertain_send_is_neither_refreshed_nor_retried(self):
        self.store.append(event('EvOld', '100.1', 'original'))
        self.worker.claim()
        self.store.append(event('EvNew', '101.1', 'correction'))
        self.worker.context('EvOld', self.web)
        self.web.post_message.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            self.worker.reply('EvOld', 'progress', self.web)
        before = self.states()
        with self.assertRaises(ValueError):
            self.worker.context('EvOld', self.web)
        with self.assertRaises(ValueError):
            self.worker.reply('EvOld', 'progress again', self.web)
        self.assertEqual(self.states(), before)
        self.assertIsNone(self.worker.claim())
        self.web.post_message.assert_called_once()
