"""Offline worker contract tests; no real Slack or Muse invocation."""
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from bridge.config import Audience, BridgeConfig
from bridge.store import EventStore
from bridge.worker import Worker
from bridge.webapi import SlackWebClient


def record(event_id="Ev1", channel="D_TEST", thread="100.1"):
    return dict(event_id=event_id, channel=channel, thread_ts=thread, ts=thread,
                user="U_TEST_OWNER", text="hello", audience_id="owner", scope="owner")


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = EventStore(self.tmp.name)
        self.cfg = BridgeConfig("test", None, [
            Audience("owner", "owner", {"U_TEST_OWNER"}, {"C_TEST"}, True)])
        self.worker = Worker(self.cfg, self.store)
        self.web = Mock()
        self.web.post_message.return_value = "101.1"

    def tearDown(self):
        self.worker.close()
        self.tmp.cleanup()

    def test_listener_queue_to_pinned_reply_once_across_restart(self):
        from bridge.listener import Listener
        listener = Listener(self.cfg, self.store, self.web, "U_TEST_BOT")
        listener.handle_envelope({"type": "events_api", "envelope_id": "env1", "payload": {
            "event_id": "Ev1", "event": {"type": "message", "user": "U_TEST_OWNER",
            "channel": "D_TEST", "channel_type": "im", "text": "hello", "ts": "100.1"}}}, Mock())
        self.assertEqual(self.worker.claim()["event"]["text"], "hello")
        self.assertIsNone(self.worker.claim())
        self.assertEqual(self.worker.reply("Ev1", "answer", self.web), "101.1")
        self.worker.close()
        self.worker = Worker(self.cfg, self.store)
        self.assertEqual(self.worker.reply("Ev1", "answer", self.web), "101.1")
        self.web.post_message.assert_called_once_with("D_TEST", "100.1", "answer")
        self.web.reactions_add.assert_not_called()
        # The bot's own response cannot start another job.
        listener.handle_envelope({"type": "events_api", "envelope_id": "env2", "payload": {
            "event_id": "Ev2", "event": {"type": "message", "user": "U_TEST_BOT",
            "channel": "D_TEST", "text": "answer", "ts": "101.1"}}}, Mock())
        self.assertIsNone(self.worker.claim())

    def test_thread_order_and_history_isolation(self):
        for item in (record(), record("Ev2", thread="200.1"), record("Ev3")):
            self.store.append(item)
        self.assertEqual(self.worker.claim()["event"]["event_id"], "Ev1")
        self.assertEqual(self.worker.claim()["event"]["event_id"], "Ev2")
        self.assertIsNone(self.worker.claim())  # Ev3 waits for Ev1
        self.worker.reply("Ev1", "first answer", self.web)
        packet = self.worker.claim()
        self.assertEqual(packet["event"]["event_id"], "Ev3")
        self.assertEqual(packet["history"], [{"user": "hello", "assistant": "first answer"}])

    def test_two_workers_cannot_claim_or_send_same_job(self):
        self.store.append(record())
        other = Worker(self.cfg, self.store)
        try:
            self.worker.claim()
            self.assertIsNone(other.claim())
            self.worker.reply("Ev1", "answer", self.web)
            other.reply("Ev1", "answer", self.web)
            self.web.post_message.assert_called_once()
        finally:
            other.close()

    def test_uncertain_delivery_never_retries(self):
        self.store.append(record())
        self.worker.claim()
        self.web.post_message.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            self.worker.reply("Ev1", "answer", self.web)
        self.worker.close()
        self.worker = Worker(self.cfg, self.store)
        with self.assertRaisesRegex(ValueError, "uncertain"):
            self.worker.reply("Ev1", "answer", self.web)
        self.assertEqual(self.worker.pending()[0]["status"], "sending")
        self.web.post_message.assert_called_once()

    def test_guests_strangers_and_unapproved_channels_never_reach_muse(self):
        for idx, changes in enumerate(({"scope": "shared:guest", "audience_id": "guest"},
                                       {"user": "U_TEST_OTHER"}, {"channel": "C_OTHER"})):
            self.store.append(dict(record("Ev" + str(idx)), **changes))
        self.assertIsNone(self.worker.claim())

    def test_revocation_rechecked_before_reply(self):
        self.store.append(record())
        self.worker.claim()
        self.worker.cfg = replace(self.cfg, audiences=[])
        with self.assertRaisesRegex(ValueError, "no longer allowed"):
            self.worker.reply("Ev1", "answer", self.web)
        self.web.post_message.assert_not_called()

    def test_reply_input_validation(self):
        self.store.append(record())
        self.worker.claim()
        for text in ("", "  ", "x" * 4001):
            with self.assertRaises(ValueError):
                self.worker.reply("Ev1", text, self.web)
        self.web.post_message.assert_not_called()


class StoreRecoveryTests(unittest.TestCase):
    def test_seen_recovers_from_queue_when_secondary_file_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = EventStore(tmp)
            store.queue_path.write_text(json.dumps(record()) + "\n", encoding="utf-8")
            self.assertFalse(EventStore(tmp).append(record()))

    def test_incomplete_tail_not_consumed_or_appended_over(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = EventStore(tmp)
            store.queue_path.write_bytes((json.dumps(record()) + "\n").encode() + b'{"text":"\xe3')
            self.assertEqual(store.read_queue(), [record()])
            with self.assertRaisesRegex(ValueError, "incomplete"):
                store.append(record("Ev2"))


class WebClientTests(unittest.TestCase):
    def test_posts_in_thread_as_bot_without_broadcast(self):
        web = SlackWebClient("fake-test-credential")
        web._post = Mock(return_value={"ok": True, "ts": "101.1"})
        self.assertEqual(web.post_message("D_TEST", "100.1", "answer"), "101.1")
        method, params = web._post.call_args.args
        self.assertEqual(method, "chat.postMessage")
        self.assertEqual(params, {"channel": "D_TEST", "thread_ts": "100.1", "text": "answer",
                                  "reply_broadcast": False, "unfurl_links": False, "unfurl_media": False})

class RecoveryTests(unittest.TestCase):
    setUp = WorkerTests.setUp
    tearDown = WorkerTests.tearDown

    def test_resume_uses_existing_claim_and_rechecks_access(self):
        self.store.append(record())
        self.worker.claim()
        self.assertEqual(self.worker.resume('Ev1')['event'], record())
        self.assertIsNone(self.worker.claim())
        self.worker.cfg = replace(self.cfg, audiences=[])
        with self.assertRaises(ValueError):
            self.worker.resume('Ev1')

    def test_reconcile_records_verified_delivery_without_resending(self):
        self.store.append(record())
        self.worker.claim()
        self.web.post_message.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            self.worker.reply('Ev1', 'answer', self.web)
        self.worker.reconcile('Ev1', '101.1')
        self.assertEqual(self.worker.reply('Ev1', 'answer', self.web), '101.1')
        self.web.post_message.assert_called_once()

    def test_explicit_non_delivery_allows_deliberate_retry(self):
        self.store.append(record())
        self.worker.claim()
        self.web.post_message.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            self.worker.reply('Ev1', 'answer', self.web)
        self.worker.reconcile('Ev1')
        self.assertIsNone(self.worker.claim())
        self.web.post_message.side_effect = None
        self.assertEqual(self.worker.reply('Ev1', 'answer', self.web), '101.1')

    def test_group_dm_dropped_even_for_owner(self):
        from bridge.listener import Listener
        listener = Listener(self.cfg, self.store, self.web, 'U_TEST_BOT')
        listener.handle_envelope({'type': 'events_api', 'envelope_id': 'env1', 'payload': {
            'event_id': 'Ev1', 'event': {'type': 'message', 'channel_type': 'mpim',
            'user': 'U_TEST_OWNER', 'channel': 'G_TEST', 'text': 'hello', 'ts': '100.1'}}}, Mock())
        self.assertEqual(self.store.read_queue(), [])
        self.web.reactions_add.assert_not_called()


if __name__ == "__main__":
    unittest.main()
