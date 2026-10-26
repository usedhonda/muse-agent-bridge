"""Offline tests for the Telegram adapter CLI (bridge_core.telegram).

The Bot API fetch is stubbed, so no network or credentials are used.
The tests drive the adapter through its real CLI modes -- the same
ones the wake hook and woken turns call -- and check the wake payload
shape, the in-flight rule, token-checked completion, reply recording,
and cross-poll dedupe.
"""
import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from bridge_core import queue as queue_core
from bridge_core import telegram
from bridge_core.store import JsonStateStore

OWNER = 111
GROUP = -777


def msg_update(uid, chat_id, chat_type, sender_id, text, username=None, is_bot=False):
    user = {"id": sender_id, "is_bot": is_bot}
    if username:
        user["username"] = username
    return {
        "update_id": uid,
        "message": {
            "from": user,
            "chat": {"id": chat_id, "type": chat_type},
            "text": text,
            "date": 1700000000 + uid,
        },
    }


class FakeFetch:
    """Returns one canned getUpdates response until replaced."""

    def __init__(self, updates=()):
        self.updates = list(updates)
        self.offsets = []

    def __call__(self, offset):
        self.offsets.append(offset)
        return {"ok": True, "result": list(self.updates)}


def run_cli(path, argv, fetch=None, stdin_text=""):
    out = io.StringIO()
    old_stdin = sys.stdin
    sys.stdin = io.StringIO(stdin_text)
    try:
        with redirect_stdout(out):
            telegram.main(argv, fetch=fetch or FakeFetch(), path=path)
    finally:
        sys.stdin = old_stdin
    text = out.getvalue().strip()
    return json.loads(text) if text else None


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"
        run_cli(self.path, ["--adopt", str(OWNER)])

    def tearDown(self):
        self.tmp.cleanup()

    def test_wake_payload_shape_and_legacy_fields(self):
        fetch = FakeFetch(
            [
                msg_update(1, OWNER, "private", OWNER, "hello dm", "boss"),
                msg_update(2, GROUP, "group", OWNER, "first in group", "boss"),
                msg_update(3, GROUP, "group", 222, "agent note", "kai_bot", True),
                msg_update(4, 999, "private", 999, "stranger, ignored"),
            ]
        )
        # P5 lane discipline: the oldest pending item is the owner's
        # DM, so the first wake carries that conversation lane alone.
        payload = run_cli(self.path, [], fetch=fetch)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(len(payload["items"]), 1)
        self.assertTrue(payload["claim_token"])
        # Legacy single-item fields mirror the first item.
        self.assertEqual(payload["update_id"], 1)
        self.assertEqual(payload["text"], "hello dm")
        for key in ("chat_id", "chat_type", "sender_name", "is_bot"):
            self.assertEqual(payload[key], payload["items"][0][key])
        # DM items carry no conversation context.
        self.assertNotIn("context", payload["items"][0])
        run_cli(self.path, ["--done", "1", payload["claim_token"]])
        # The next wake takes the group lane: both group items, with
        # context attached.
        payload = run_cli(self.path, [], fetch=FakeFetch())
        self.assertEqual(payload["count"], 2)
        self.assertEqual(
            [item["update_id"] for item in payload["items"]], [2, 3]
        )
        for key in ("chat_id", "chat_type", "sender_name", "is_bot"):
            self.assertEqual(payload[key], payload["items"][0][key])
        self.assertIn("context", payload["items"][0])
        self.assertIn("context", payload["items"][1])
        # Group adoption happened through the owner's group message.
        state = JsonStateStore(self.path).load()
        self.assertEqual(state["group_id"], GROUP)
        self.assertEqual(state["offset"], 5)

    def test_in_flight_batch_blocks_second_wake(self):
        fetch = FakeFetch([msg_update(1, OWNER, "private", OWNER, "one")])
        self.assertIsNotNone(run_cli(self.path, [], fetch=fetch))
        # Nothing new arrives and the first batch is still claimed:
        # the next poll must stay silent instead of re-waking the item.
        fetch.updates = [msg_update(2, OWNER, "private", OWNER, "two")]
        self.assertIsNone(run_cli(self.path, [], fetch=fetch))

    def test_token_checked_done_release_replied_flow(self):
        fetch = FakeFetch(
            [
                msg_update(1, OWNER, "private", OWNER, "one"),
                msg_update(2, OWNER, "private", OWNER, "two"),
            ]
        )
        payload = run_cli(self.path, [], fetch=fetch)
        token = payload["claim_token"]
        # Wrong token: no-op, item stays claimed.
        self.assertEqual(
            run_cli(self.path, ["--done", "1", "bogus"]), {"ok": False}
        )
        state = JsonStateStore(self.path).load()
        item1 = next(i for i in state["queue"] if i["external_id"] == "1")
        self.assertEqual(item1["status"], "claimed")
        # Send lifecycle: --sending before the send (token-checked),
        # then --replied, then --done from the sending state.
        self.assertEqual(
            run_cli(self.path, ["--sending", "1", "bogus"]), {"ok": False}
        )
        self.assertEqual(
            run_cli(self.path, ["--sending", "1", token]), {"ok": True}
        )
        self.assertEqual(
            run_cli(self.path, ["--replied", "1", "555", token]), {"ok": True}
        )
        self.assertEqual(run_cli(self.path, ["--done", "1", token]), {"ok": True})
        # Release the second item back to pending; it can be claimed again.
        self.assertEqual(
            run_cli(self.path, ["--release", "2", token]), {"ok": True}
        )
        fetch.updates = []
        nxt = run_cli(self.path, [], fetch=fetch)
        self.assertEqual(nxt["count"], 1)
        self.assertEqual(nxt["update_id"], 2)
        state = JsonStateStore(self.path).load()
        item1 = next(i for i in state["queue"] if i["external_id"] == "1")
        self.assertEqual(item1["status"], "done")
        self.assertEqual(item1["reply_message_id"], "555")
        self.assertIsNotNone(item1["replied_at"])
        self.assertIsNotNone(item1["received_at"])

    def test_p1_p2_p4_send_lifecycle_via_cli(self):
        """P1/P2/P4: the CLI enforces the send lifecycle —
        token-checked --sending before the send, --replied only with
        the batch token, no --release of a replied item, and
        --send-failed as the only way back from a failed send."""
        fetch = FakeFetch(
            [
                msg_update(1, OWNER, "private", OWNER, "one"),
                msg_update(2, OWNER, "private", OWNER, "two"),
            ]
        )
        payload = run_cli(self.path, [], fetch=fetch)
        token = payload["claim_token"]
        # P4: --replied without the batch token records nothing.
        self.assertEqual(
            run_cli(self.path, ["--replied", "1", "555"]), {"ok": False}
        )
        # P1 definitive-failure path: sending -> send-failed -> pending.
        self.assertEqual(
            run_cli(self.path, ["--sending", "2", token]), {"ok": True}
        )
        self.assertEqual(
            run_cli(self.path, ["--send-failed", "2", "bogus"]), {"ok": False}
        )
        self.assertEqual(
            run_cli(self.path, ["--send-failed", "2", token]), {"ok": True}
        )
        # P1 success path + P2: sending -> replied -> release refused
        # -> done.
        self.assertEqual(
            run_cli(self.path, ["--sending", "1", token]), {"ok": True}
        )
        self.assertEqual(
            run_cli(self.path, ["--replied", "1", "555", token]), {"ok": True}
        )
        self.assertEqual(
            run_cli(self.path, ["--release", "1", token]), {"ok": False}
        )
        self.assertEqual(run_cli(self.path, ["--done", "1", token]), {"ok": True})
        state = JsonStateStore(self.path).load()
        by_id = {i["external_id"]: i for i in state["queue"]}
        self.assertEqual(by_id["1"]["status"], "done")
        self.assertEqual(by_id["1"]["reply_message_id"], "555")
        self.assertIsNotNone(by_id["1"]["replied_at"])
        self.assertEqual(by_id["2"]["status"], "pending")

    def test_redelivery_is_deduped_across_polls(self):
        fetch = FakeFetch([msg_update(1, OWNER, "private", OWNER, "same")])
        payload = run_cli(self.path, [], fetch=fetch)
        run_cli(self.path, ["--done", "1", payload["claim_token"]])
        # Telegram redelivers update 1 (e.g. after an offset hiccup):
        # it must not queue or wake a second time.
        self.assertIsNone(run_cli(self.path, [], fetch=fetch))
        state = JsonStateStore(self.path).load()
        self.assertEqual(len(state["queue"]), 1)


class SendFenceTests(unittest.TestCase):
    """--send-fenced: the bridge-side send fence.

    A fake send executable written to a tmp dir stands in for the
    deployment's send command (TELEGRAM_BRIDGE_SEND_CMD). It records
    every invocation (argv + stdin) to a calls file, so tests can
    prove the fence never invokes it when it refuses, and invokes it
    exactly once on a real send. No real paths, ids or credentials
    are used.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "state.json"
        self.calls = self.dir / "send-calls.log"
        run_cli(self.path, ["--adopt", str(OWNER)])

    def tearDown(self):
        self.tmp.cleanup()

    def write_send_cmd(self, body):
        script = self.dir / "fake-send.py"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            f"with open({str(self.calls)!r}, 'a', encoding='utf-8') as fh:\n"
            "    fh.write('argv=' + repr(sys.argv[1:]) + '\\n')\n"
            "    fh.write('stdin=' + sys.stdin.read() + '\\n')\n"
            + body,
            encoding="utf-8",
        )
        script.chmod(0o755)
        return str(script)

    def send_env(self, cmd):
        return mock.patch.dict(os.environ, {telegram.SEND_CMD_ENV: cmd})

    def claim_dm(self, uid=1, text="please reply"):
        fetch = FakeFetch([msg_update(uid, OWNER, "private", OWNER, text)])
        payload = run_cli(self.path, [], fetch=fetch)
        return payload["claim_token"]

    def item(self, external_id):
        state = JsonStateStore(self.path).load()
        return next(
            i for i in state["queue"] if i["external_id"] == str(external_id)
        )

    def call_count(self):
        if not self.calls.exists():
            return 0
        return self.calls.read_text(encoding="utf-8").count("argv=")

    def fenced(self, cmd, target, token, stdin_text="the reply text"):
        with self.send_env(cmd):
            return run_cli(
                self.path,
                ["--send-fenced", str(target), token, str(OWNER)],
                stdin_text=stdin_text,
            )

    def test_fence_refuses_without_any_send_invocation(self):
        """Fence refusals (wrong token, unknown item, already
        replied, never claimed) print stage "fence" and the send
        command is never invoked."""
        token = self.claim_dm()
        cmd = self.write_send_cmd('print("4242")\n')
        self.assertEqual(
            self.fenced(cmd, 1, "bogus"),
            {"ok": False, "stage": "fence"},
        )
        self.assertEqual(
            self.fenced(cmd, 99, token),
            {"ok": False, "stage": "fence"},
        )
        self.assertEqual(self.call_count(), 0)
        self.assertEqual(self.item(1)["status"], "claimed")
        # Already replied: the fence refuses (mark_sending guard).
        self.assertEqual(
            run_cli(self.path, ["--replied", "1", "555", token]),
            {"ok": True},
        )
        self.assertEqual(
            self.fenced(cmd, 1, token),
            {"ok": False, "stage": "fence"},
        )
        self.assertEqual(self.call_count(), 0)
        # Pending but never claimed: refused as well.
        store = JsonStateStore(self.path)
        with store.transaction() as state:
            queue_core.enqueue(
                state, platform="telegram", external_id="50",
                chat_id=OWNER, chat_type="private", text="queued",
            )
        self.assertEqual(
            self.fenced(cmd, 50, token),
            {"ok": False, "stage": "fence"},
        )
        self.assertEqual(self.call_count(), 0)

    def test_fence_happy_path_sends_once_then_replied_and_done(self):
        """The fence marks sending and persists BEFORE the send runs
        once with argv [chat id] and the text on stdin; the last
        stdout line is the message id. The item stays in sending
        until the caller records --replied and closes with --done."""
        token = self.claim_dm()
        cmd = self.write_send_cmd('print("send log line")\nprint("4242")\n')
        result = self.fenced(cmd, 1, token)
        self.assertEqual(result, {"ok": True, "message_id": "4242"})
        self.assertEqual(self.call_count(), 1)
        calls = self.calls.read_text(encoding="utf-8")
        self.assertIn(f"argv=['{OWNER}']", calls)
        self.assertIn("stdin=the reply text", calls)
        item = self.item(1)
        self.assertEqual(item["status"], "sending")
        self.assertIsNotNone(item["sending_at"])
        # Caller closes the loop exactly as in the turn protocol.
        self.assertEqual(
            run_cli(self.path, ["--replied", "1", "4242", token]),
            {"ok": True},
        )
        self.assertEqual(
            run_cli(self.path, ["--done", "1", token]), {"ok": True}
        )
        item = self.item(1)
        self.assertEqual(item["status"], "done")
        self.assertEqual(item["reply_message_id"], "4242")
        self.assertIsNotNone(item["replied_at"])

    def assert_unknown_reservation(self, cmd, token):
        """Unknown delivery holds its token and lane beyond TTL, without retry."""
        item = self.item(1)
        self.assertEqual(item["status"], "sending")
        self.assertEqual(item["claim_token"], token)
        self.assertIsNotNone(item["sending_at"])
        store = JsonStateStore(self.path)
        with store.transaction() as state:
            item = next(i for i in state["queue"] if i["external_id"] == "1")
            item["claimed_at"] = 1.0
            item["sending_at"] = 1.0
        fetch = FakeFetch([msg_update(2, OWNER, "private", OWNER, "another")])
        self.assertIsNone(run_cli(self.path, [], fetch=fetch))
        self.assertEqual(self.item(1)["status"], "sending")
        self.assertEqual(self.item(1)["claim_token"], token)
        self.assertEqual(self.item(2)["status"], "pending")
        self.assertEqual(run_cli(self.path, ["--release", "1", token]), {"ok": False})
        self.assertEqual(run_cli(self.path, ["--done", "1", token, "cancelled"]), {"ok": False})
        self.assertEqual(self.fenced(cmd, 1, token), {"ok": False, "stage": "fence"})
        self.assertEqual(self.call_count(), 1)

    def test_fence_nonzero_exit_is_unknown(self):
        """An arbitrary nonzero exit does not establish non-delivery."""
        token = self.claim_dm()
        cmd = self.write_send_cmd("sys.exit(3)\n")
        result = self.fenced(cmd, 1, token)
        self.assertEqual(result, {"ok": False, "stage": "unknown"})
        self.assert_unknown_reservation(cmd, token)

    def accepted_then_failed(self, failure):
        token = self.claim_dm()
        accepted = self.dir / "fake-remote-acceptance"
        cmd = self.write_send_cmd(
            "from pathlib import Path\n"
            f"Path({str(accepted)!r}).write_text('accepted')\n" + failure
        )
        self.assertEqual(self.fenced(cmd, 1, token), {"ok": False, "stage": "unknown"})
        self.assertEqual(accepted.read_text(), "accepted")
        self.assert_unknown_reservation(cmd, token)

    def test_fence_accepted_then_crashed(self):
        self.accepted_then_failed("raise RuntimeError('synthetic post-send crash')\n")

    def test_fence_accepted_then_response_lost(self):
        self.accepted_then_failed("raise ConnectionError('synthetic lost response')\n")

    def test_fence_accepted_then_signal(self):
        self.accepted_then_failed("import os, signal\nos.kill(os.getpid(), signal.SIGTERM)\n")

    def test_fence_message_id_then_nonzero_is_unknown(self):
        self.accepted_then_failed("print('4242', flush=True)\nsys.exit(1)\n")

    def test_fence_accepted_then_response_missing(self):
        self.accepted_then_failed("")

    def test_fence_accepted_then_undecodable_response(self):
        self.accepted_then_failed("sys.stdout.buffer.write(b'\\xff')\n")

    def test_verified_no_send_requires_explicit_operator_transition(self):
        """No sender exit code authorizes retry, even a pre-send failure.

        Here the fixture verifiably stops before sending. Only the separate
        token-checked operator transition may make it retryable.
        """
        token = self.claim_dm()
        cmd = self.write_send_cmd("sys.exit(3)\n")
        self.assertEqual(self.fenced(cmd, 1, token), {"ok": False, "stage": "unknown"})
        self.assertEqual(run_cli(self.path, ["--send-failed", "1", "wrong"]), {"ok": False})
        self.assertEqual(self.item(1)["status"], "sending")
        self.assertEqual(run_cli(self.path, ["--send-failed", "1", token]), {"ok": True})
        item = self.item(1)
        self.assertEqual(item["status"], "pending")
        self.assertIsNone(item["claim_token"])
        self.assertIsNone(item["sending_at"])
        payload = run_cli(self.path, [], fetch=FakeFetch())
        self.assertEqual(payload["update_id"], 1)
        self.assertNotEqual(payload["claim_token"], token)
        self.assertEqual(self.call_count(), 1)

    def test_fence_ambiguous_outcome_leaves_sending_and_never_recycles(self):
        """Exit 0 without a message id is ambiguous: the item stays
        in sending (send-unknown), a later poll neither recycles it
        nor claims its lane, and the send is never retried."""
        token = self.claim_dm()
        cmd = self.write_send_cmd("")  # exits 0, prints nothing
        result = self.fenced(cmd, 1, token)
        self.assertEqual(result, {"ok": False, "stage": "unknown"})
        self.assertEqual(self.item(1)["status"], "sending")
        # Age the item far beyond the stale TTL.
        store = JsonStateStore(self.path)
        with store.transaction() as state:
            item = next(
                i for i in state["queue"] if i["external_id"] == "1"
            )
            item["claimed_at"] = 1.0
            item["sending_at"] = 1.0
        fetch = FakeFetch([msg_update(2, OWNER, "private", OWNER, "another")])
        self.assertIsNone(run_cli(self.path, [], fetch=fetch))
        self.assertEqual(self.item(1)["status"], "sending")
        self.assertEqual(self.item(2)["status"], "pending")
        self.assertEqual(self.call_count(), 1)

    def test_fence_unstartable_send_command_is_unknown(self):
        """A send command that cannot be started at all is
        ambiguous, not definitive: the item stays in sending."""
        token = self.claim_dm()
        result = self.fenced(str(self.dir / "no-such-send-command"), 1, token)
        self.assertEqual(result, {"ok": False, "stage": "unknown"})
        self.assertEqual(self.item(1)["status"], "sending")

    def test_fence_send_timeout_is_unknown(self):
        """A send command that outlives the runner timeout is
        ambiguous: the send may have gone out, so the item stays in
        sending rather than returning to pending."""
        token = self.claim_dm()
        cmd = self.write_send_cmd("import time\ntime.sleep(30)\n")
        with mock.patch.object(telegram, "SEND_TIMEOUT_SECONDS", 0.5):
            result = self.fenced(cmd, 1, token)
        self.assertEqual(result, {"ok": False, "stage": "unknown"})
        self.assertEqual(self.item(1)["status"], "sending")

    def test_fence_missing_send_cmd_config_leaves_item_claimed(self):
        """Without TELEGRAM_BRIDGE_SEND_CMD the result is stage
        "config" and the fence never marks: the item stays claimed
        (not sending), so nothing about it changes."""
        token = self.claim_dm()
        with mock.patch.dict(os.environ):
            os.environ.pop(telegram.SEND_CMD_ENV, None)
            result = run_cli(
                self.path,
                ["--send-fenced", "1", token, str(OWNER)],
                stdin_text="the reply text",
            )
        self.assertEqual(result, {"ok": False, "stage": "config"})
        item = self.item(1)
        self.assertEqual(item["status"], "claimed")
        self.assertIsNone(item["sending_at"])

    def test_done_from_sending_requires_evidence_via_cli(self):
        """F2 at the CLI: a send-unknown item refuses --done with
        only a reason; with REASON + EVIDENCE it closes and stores
        done_reason and done_evidence separately."""
        fetch = FakeFetch([msg_update(1, OWNER, "private", OWNER, "one")])
        payload = run_cli(self.path, [], fetch=fetch)
        token = payload["claim_token"]
        self.assertEqual(
            run_cli(self.path, ["--sending", "1", token]), {"ok": True}
        )
        self.assertEqual(
            run_cli(self.path, ["--done", "1", token, "reason-only"]),
            {"ok": False},
        )
        self.assertEqual(self.item(1)["status"], "sending")
        self.assertEqual(
            run_cli(
                self.path,
                ["--done", "1", token, "reconciled",
                 "history: reply found as message 900"],
            ),
            {"ok": True},
        )
        item = self.item(1)
        self.assertEqual(item["status"], "done")
        self.assertEqual(item["done_reason"], "reconciled")
        self.assertEqual(
            item["done_evidence"], "history: reply found as message 900"
        )


class PurgeExpiredTests(unittest.TestCase):
    """--purge-expired (Issue #17): the operator form of the
    pending-expiry sweep — same class TTLs and safety lines as the
    claim-time sweep, runnable on demand, with a dry run."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"
        run_cli(self.path, ["--adopt", str(OWNER)])
        now = time.time()
        store = JsonStateStore(self.path)
        with store.transaction() as state:
            queue_core.enqueue(
                state, platform="telegram", external_id="g-old",
                chat_id=GROUP, chat_type="group", text="old group",
                now=now - 3600,
            )
            queue_core.enqueue(
                state, platform="telegram", external_id="g-mid",
                chat_id=GROUP, chat_type="group", text="mid group",
                now=now - 300,
            )
            queue_core.enqueue(
                state, platform="telegram", external_id="dm-old",
                chat_id=OWNER, chat_type="private", text="old dm",
                now=now - 86400,
            )

    def tearDown(self):
        self.tmp.cleanup()

    def item(self, external_id):
        state = JsonStateStore(self.path).load()
        return next(
            i for i in state["queue"] if i["external_id"] == external_id
        )

    def test_purge_dry_run_changes_nothing(self):
        result = run_cli(self.path, ["--purge-expired", "--dry-run"])
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["dry_run"], True)
        self.assertEqual(result["expired"], 1)
        self.assertEqual(result["external_ids"], ["g-old"])
        for external_id in ("g-old", "g-mid", "dm-old"):
            item = self.item(external_id)
            self.assertEqual(item["status"], "pending")
            self.assertNotIn("done_reason", item)

    def test_purge_expires_old_group_items_only(self):
        result = run_cli(self.path, ["--purge-expired"])
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["dry_run"], False)
        self.assertEqual(result["expired"], 1)
        old = self.item("g-old")
        self.assertEqual(old["status"], "done")
        self.assertEqual(old["done_reason"], "expired")
        # The mid-age group item is under the default TTL, and the
        # day-old owner DM never expires by default.
        self.assertEqual(self.item("g-mid")["status"], "pending")
        self.assertEqual(self.item("dm-old")["status"], "pending")

    def test_purge_older_than_overrides_group_ttl(self):
        result = run_cli(
            self.path, ["--purge-expired", "--older-than", "60"]
        )
        self.assertEqual(result["expired"], 2)
        self.assertEqual(
            set(result["external_ids"]), {"g-old", "g-mid"}
        )
        # Even an aggressive threshold never touches the owner DM:
        # --older-than overrides the group class only.
        self.assertEqual(self.item("dm-old")["status"], "pending")

    def test_purge_rejects_bad_arguments(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = telegram.main(
                ["--purge-expired", "--older-than", "soon"],
                fetch=FakeFetch(),
                path=self.path,
            )
        self.assertEqual(rc, 2)
        self.assertFalse(json.loads(out.getvalue())["ok"])
        self.assertEqual(self.item("g-old")["status"], "pending")


if __name__ == "__main__":
    unittest.main()
