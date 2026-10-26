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

    def test_stale_item_is_refused_and_claim_preserved_without_sending(self):
        """Issue #17, review finding 1: a claimed item older than
        the maximum reply age is refused at the fence with stage
        "stale" and the send command is never invoked — but the
        fence does NOT close the work. This item is a private
        (owner DM) request: it stays claimed under the caller's
        token, with no done marking, preserved for reassessment."""
        with self.store.transaction() as state:
            queue_core.find_item(state, "telegram", 101)["received_at"] -= 7200
        result, sender = self.fenced("123456789")
        self.assertEqual(result, {"ok": False, "stage": "stale"})
        sender.assert_not_called()
        item = self.item()
        self.assertEqual(item["status"], "claimed")
        self.assertEqual(item["claim_token"], self.token)
        self.assertIsNone(item["sending_at"])
        self.assertIsNone(item["done_at"])
        self.assertNotIn("done_reason", item)

    def test_stale_group_item_is_preserved_for_reassessment(self):
        """Finding 1 for group work: a long-running claimed group
        item past the maximum reply age is refused as stale, and
        likewise stays claimed and unclosed — never done/expired."""
        store = JsonStateStore(Path(self.temp.name) / "group-state.json")
        with store.transaction() as state:
            queue_core.enqueue(
                state, platform="telegram", external_id=202,
                chat_id=-777, chat_type="supergroup", sender_id=222,
                text="long-running group work",
                item_class=queue_core.CLASS_REQUEST,
            )
            token = queue_core.claim_batch(state, platform="telegram")[
                "claim_token"
            ]
            queue_core.find_item(state, "telegram", 202)[
                "received_at"
            ] -= 7200
        with mock.patch.dict(os.environ, {telegram.SEND_CMD_ENV: "/fake/sender"}):
            with mock.patch.object(telegram, "_run_send_command") as sender:
                result = telegram.send_fenced(
                    store, 202, token, "-777", "late reply"
                )
        self.assertEqual(result, {"ok": False, "stage": "stale"})
        sender.assert_not_called()
        item = queue_core.find_item(store.load(), "telegram", 202)
        self.assertEqual(item["status"], "claimed")
        self.assertEqual(item["claim_token"], token)
        self.assertIsNone(item["done_at"])
        self.assertNotIn("done_reason", item)

    def test_stale_send_with_wrong_token_is_a_plain_fence_refusal(self):
        """The stale refusal belongs to the claim owner: a caller
        that does not own the claim gets the plain fence refusal
        (never the stale verdict), and the item stays claimed and
        unclosed either way."""
        with self.store.transaction() as state:
            queue_core.find_item(state, "telegram", 101)["received_at"] -= 7200
        with mock.patch.dict(os.environ, {telegram.SEND_CMD_ENV: "/fake/sender"}):
            with mock.patch.object(telegram, "_run_send_command") as sender:
                result = telegram.send_fenced(
                    self.store, 101, "bogus-token", "123456789", "late reply"
                )
        self.assertEqual(result, {"ok": False, "stage": "fence"})
        sender.assert_not_called()
        item = self.item()
        self.assertEqual(item["status"], "claimed")
        self.assertNotIn("done_reason", item)

    def test_non_finite_max_reply_age_is_refused_before_any_mutation(self):
        """Finding 5 at the fence: a NaN or infinite maximum reply
        age is a configuration error, refused with stage "input"
        before any state change — not silently treated as no
        guard (NaN) or as an odd limit."""
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(max_reply_age=bad):
                with mock.patch.dict(
                    os.environ, {telegram.SEND_CMD_ENV: "/fake/sender"}
                ):
                    with mock.patch.object(
                        telegram, "_run_send_command"
                    ) as sender:
                        result = telegram.send_fenced(
                            self.store, 101, self.token, "123456789",
                            "reply", max_reply_age=bad,
                        )
                self.assertEqual(result, {"ok": False, "stage": "input"})
                sender.assert_not_called()
                item = self.item()
                self.assertEqual(item["status"], "claimed")
                self.assertIsNone(item["sending_at"])

    def test_item_at_exactly_the_max_reply_age_still_sends(self):
        """The guard refuses only ABOVE the limit: an item whose age
        equals the maximum reply age follows the normal fence path
        and still sends."""
        received = self.item()["received_at"]
        with mock.patch.dict(os.environ, {telegram.SEND_CMD_ENV: "/fake/sender"}):
            with mock.patch.object(
                telegram, "_run_send_command", return_value=("sent", "777")
            ) as sender:
                result = telegram.send_fenced(
                    self.store, 101, self.token, "123456789", "on time",
                    now=received + telegram.DEFAULT_MAX_REPLY_AGE,
                )
        self.assertEqual(result, {"ok": True, "message_id": "777"})
        sender.assert_called_once()
        self.assertEqual(self.item()["status"], "sending")

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
