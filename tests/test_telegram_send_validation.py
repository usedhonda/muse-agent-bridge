"""Offline fail-closed tests for Telegram reply destination and text checks.

No Muse credentials, Telegram calls, or external sender are used.
"""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from bridge_core import queue as queue_core
from bridge_core import telegram
from bridge_core.store import JsonStateStore


class TelegramSendValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        self.store = JsonStateStore(self.path)
        with self.store.transaction() as state:
            queue_core.enqueue(
                state, platform="telegram", external_id=101,
                chat_id=123456789, chat_type="private", sender_id=123456789,
                text="original message"
            )
            self.token = queue_core.claim_batch(state, platform="telegram")["claim_token"]

    def item(self):
        return queue_core.find_item(self.store.load(), "telegram", 101)

    def fenced(self, destination, text="approved reply"):
        with mock.patch.dict(os.environ, {telegram.SEND_CMD_ENV: "/fake/sender"}):
            with mock.patch.object(
                telegram, "_run_send_command", return_value=("sent", "777")
            ) as sender:
                result = telegram.send_fenced(
                    self.store, 101, self.token, destination, text
                )
        return result, sender

    def test_wrong_chat_is_refused_before_sender_or_reservation(self):
        result, sender = self.fenced("987654321")
        self.assertEqual(result, {"ok": False, "stage": "fence"})
        sender.assert_not_called()
        self.assertEqual(self.item()["status"], "claimed")
        self.assertIsNone(self.item()["sending_at"])

    def test_correct_chat_uses_persisted_integer_destination(self):
        result, sender = self.fenced("123456789")
        self.assertEqual(result, {"ok": True, "message_id": "777"})
        sender.assert_called_once_with("/fake/sender", 123456789, "approved reply")
        self.assertEqual(self.item()["status"], "sending")

    def test_malformed_queued_chat_refuses_even_if_argument_matches(self):
        with self.store.transaction() as state:
            queue_core.find_item(state, "telegram", 101)["chat_id"] = "123456789"
        result, sender = self.fenced("123456789")
        self.assertEqual(result, {"ok": False, "stage": "fence"})
        sender.assert_not_called()
        self.assertEqual(self.item()["status"], "claimed")

    def test_blank_and_oversized_text_do_not_strand_claim(self):
        for bad_text in ("", " \n ", "x" * 4097):
            with self.subTest(text_length=len(bad_text)):
                result, sender = self.fenced("123456789", bad_text)
                self.assertEqual(result, {"ok": False, "stage": "input"})
                sender.assert_not_called()
                self.assertEqual(self.item()["status"], "claimed")
        result, sender = self.fenced("123456789", "valid reply")
        self.assertTrue(result["ok"])
        sender.assert_called_once()

    def test_cli_rejects_wrong_destination_without_invoking_sender(self):
        output = io.StringIO()
        with mock.patch.dict(os.environ, {telegram.SEND_CMD_ENV: "/fake/sender"}):
            with mock.patch.object(telegram, "_run_send_command") as sender:
                with mock.patch("sys.stdin", io.StringIO("private content")):
                    with redirect_stdout(output):
                        self.assertEqual(
                            telegram.main(
                                ["--send-fenced", "101", self.token, "999"],
                                path=self.path,
                            ),
                            0,
                        )
        self.assertEqual(json.loads(output.getvalue()),
                         {"ok": False, "stage": "fence"})
        sender.assert_not_called()
        self.assertEqual(self.item()["status"], "claimed")


if __name__ == "__main__":
    unittest.main()
