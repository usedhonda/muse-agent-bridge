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


def msg_update(uid, chat_id, chat_type, sender_id, text, username=None, is_bot=False, reply_to=None):
    user = {"id": sender_id, "is_bot": is_bot}
    if username:
        user["username"] = username
    message = {
        "from": user,
        "chat": {"id": chat_id, "type": chat_type},
        "text": text,
        "date": 1700000000 + uid,
    }
    if reply_to is not None:
        message["reply_to_message"] = {"message_id": reply_to}
    return {
        "update_id": uid,
        "message": message,
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
                item_class=queue_core.CLASS_CONVERSATION,
            )
            queue_core.enqueue(
                state, platform="telegram", external_id="g-mid",
                chat_id=GROUP, chat_type="group", text="mid group",
                now=now - 300,
                item_class=queue_core.CLASS_CONVERSATION,
            )
            queue_core.enqueue(
                state, platform="telegram", external_id="dm-old",
                chat_id=OWNER, chat_type="private", text="old dm",
                now=now - 86400,
                item_class=queue_core.CLASS_REQUEST,
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

    def test_purge_rejects_non_finite_older_than_without_mutation(self):
        """Finding 5 regression: float() parses "nan"/"inf", but a
        NaN threshold would expire every eligible item and an
        infinite one none — both are input errors, rejected with
        the usage error before any state changes."""
        for bad in ("nan", "inf", "-inf"):
            with self.subTest(older_than=bad):
                out = io.StringIO()
                with redirect_stdout(out):
                    rc = telegram.main(
                        ["--purge-expired", "--older-than", bad],
                        fetch=FakeFetch(),
                        path=self.path,
                    )
                self.assertEqual(rc, 2)
                self.assertFalse(json.loads(out.getvalue())["ok"])
                for external_id in ("g-old", "g-mid", "dm-old"):
                    item = self.item(external_id)
                    self.assertEqual(item["status"], "pending")
                    self.assertNotIn("done_reason", item)


class IntakeClassificationTests(unittest.TestCase):
    """Classification protects owner/addressed/unknown input at intake."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"
        run_cli(self.path, ["--adopt", str(OWNER)])

    def tearDown(self):
        self.tmp.cleanup()

    def classes(self):
        state = JsonStateStore(self.path).load()
        return {
            i["external_id"]: i["item_class"] for i in state["queue"]
        }

    def test_intake_classification_rules(self):
        fetch = FakeFetch(
            [
                # The owner's group message adopts the group; the
                # sender being the owner makes it a request.
                msg_update(1, GROUP, "group", OWNER, "opening the room"),
                # A bot's unfamiliar statement stays protected.
                msg_update(2, GROUP, "group", 222, "plain agent chatter",
                           "kai_bot", True),
                # A mention of the bot (any case) is a request.
                msg_update(3, GROUP, "group", 333, "hey @Usedhonda_Muse_Bot check this"),
                # A bot command is a request.
                msg_update(4, GROUP, "group", 333, "/status please"),
                # A bot addressing this bot is a request, exactly
                # like a human addressing it.
                msg_update(5, GROUP, "group", 222, "@usedhonda_muse_bot take this",
                           "kai_bot", True),
                # The owner's DM is a request.
                msg_update(6, OWNER, "private", OWNER, "a dm ask"),
            ]
        )
        run_cli(self.path, [], fetch=fetch)
        self.assertEqual(
            self.classes(),
            {
                "1": queue_core.CLASS_REQUEST,
                "2": queue_core.CLASS_REQUEST,
                "3": queue_core.CLASS_REQUEST,
                "4": queue_core.CLASS_REQUEST,
                "5": queue_core.CLASS_REQUEST,
                "6": queue_core.CLASS_REQUEST,
            },
        )


class IntakeProtectionClassificationTests(unittest.TestCase):
    """Only positive greetings in an idle lane may expire; unknown work survives."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"
        run_cli(self.path, ["--adopt", str(OWNER)])

    def tearDown(self):
        self.tmp.cleanup()

    def classes(self):
        state = JsonStateStore(self.path).load()
        return {
            i["external_id"]: i["item_class"] for i in state["queue"]
        }

    def item(self, external_id):
        state = JsonStateStore(self.path).load()
        return next(
            i for i in state["queue"] if i["external_id"] == str(external_id)
        )

    def test_protection_signals_and_unknown_talk_survive_purge(self):
        # Poll 1 claims the owner's opening message, so poll 2's
        # arrivals stay pending for the purge below.
        first = run_cli(
            self.path, [],
            fetch=FakeFetch(
                [msg_update(1, GROUP, "group", OWNER, "opening the room")]
            ),
        )
        self.assertIsNotNone(first)
        arrivals = [
            # A non-owner's reply to a bot's message: a follow-up
            # directed at earlier work — protected by the reply
            # alone, though its text is plain.
            msg_update(2, GROUP, "group", 222, "了解、その通りだね",
                       "kai_bot", True, reply_to=900),
            # A mention of ANOTHER participant (not this bot).
            msg_update(3, GROUP, "group", 333,
                       "@chiisamabot その案でいいと思う"),
            # A standalone correction, mentioning nobody.
            msg_update(4, GROUP, "group", 333, "さっきの件、やっぱりなしで"),
            # A standalone cancellation from a bot.
            msg_update(5, GROUP, "group", 222, "その作業は中止して",
                       "kai_bot", True),
            # A question-form follow-up to ongoing work.
            msg_update(6, GROUP, "group", 333, "それ、どうなった？"),
            # A request marker ("tell me").
            msg_update(7, GROUP, "group", 222, "あとで結果を教えて",
                       "kai_bot", True),
            # A plain statement is not evidence of ordinary talk.
            msg_update(8, GROUP, "group", 222, "今日のメモをここに置いておく",
                       "kai_bot", True),
        ]
        self.assertIsNone(
            run_cli(self.path, [], fetch=FakeFetch(arrivals))
        )
        self.assertEqual(
            self.classes(),
            {
                "1": queue_core.CLASS_REQUEST,
                "2": queue_core.CLASS_REQUEST,
                "3": queue_core.CLASS_REQUEST,
                "4": queue_core.CLASS_REQUEST,
                "5": queue_core.CLASS_REQUEST,
                "6": queue_core.CLASS_REQUEST,
                "7": queue_core.CLASS_REQUEST,
                "8": queue_core.CLASS_REQUEST,
            },
        )
        # A legacy record with no classification is protected too.
        now = time.time()
        store = JsonStateStore(self.path)
        with store.transaction() as state:
            queue_core.enqueue(
                state, platform="telegram", external_id="legacy-1",
                chat_id=GROUP, chat_type="group", text="legacy record",
                now=now - 7200,
            )
            # Age every pending arrival well past the group TTL.
            for uid in range(2, 9):
                queue_core.find_item(state, "telegram", uid)[
                    "received_at"
                ] -= 7200
        self.assertEqual(
            run_cli(self.path, ["--done", "1", first["claim_token"]]),
            {"ok": True},
        )
        result = run_cli(self.path, ["--purge-expired"])
        self.assertEqual(result["expired"], 0)
        self.assertEqual(result["external_ids"], [])
        for uid in range(2, 9):
            self.assertEqual(self.item(uid)["status"], "pending")
        self.assertEqual(self.item("legacy-1")["status"], "pending")

    def test_exact_greetings_only_and_protection_overrides(self):
        for text in ("hello", "  HELLO  ", "こんにちは", "こんばんは"):
            with self.subTest(text=text):
                self.assertEqual(telegram.classify_message(
                    text, sender_is_owner=False), queue_core.CLASS_CONVERSATION)
                for signal in ({"sender_is_owner": True}, {"is_reply": True},
                               {"has_active_work": True}):
                    options = {"sender_is_owner": False, **signal}
                    self.assertEqual(telegram.classify_message(text, **options),
                                     queue_core.CLASS_REQUEST)
        # Unseen expressions and greeting prefixes cannot inherit eligibility.
        for text in ("その処理は保留", "対象をBに変更", "Please stop the job",
                     "Switch to the second target", "Wait for the green light",
                     "それは後回し", "Proceed with option C", "OK", "thanks",
                     "hello; pause the task", "hello\nuse B", "hi?", "", "🛑"):
            with self.subTest(text=text):
                self.assertEqual(telegram.classify_message(
                    text, sender_is_owner=False), queue_core.CLASS_REQUEST)

    def test_unknown_intake_survives_purge_and_claim_sweep(self):
        texts = ["その処理は保留", "対象をBに変更", "Please stop the job",
                 "Switch to the second target", "Wait for the green light"]
        store = JsonStateStore(self.path)
        with store.transaction() as state:
            state["group_id"] = GROUP
            telegram.ingest(state, {"ok": True, "result": [
                msg_update(uid, GROUP, "group", 222, text)
                for uid, text in enumerate(texts, 20)
            ]})
            for item in state["queue"]:
                item["received_at"] -= 7200
        self.assertTrue(all(c == queue_core.CLASS_REQUEST
                            for c in self.classes().values()))
        self.assertEqual(run_cli(self.path, ["--purge-expired"])["expired"], 0)
        payload = run_cli(self.path, [])
        self.assertEqual([item["text"] for item in payload["items"]], texts)
        self.assertTrue(all(item["status"] == "claimed"
                            for item in store.load()["queue"]))

    def test_idle_greeting_expires_but_active_work_greeting_survives(self):
        store = JsonStateStore(self.path)
        for work_class in (queue_core.CLASS_REQUEST, None,
                           queue_core.CLASS_CONVERSATION):
            with self.subTest(work_class=work_class):
                with store.transaction() as state:
                    state["queue"] = []
                    state["group_id"] = GROUP
                    queue_core.enqueue(
                        state, platform="telegram", external_id=90,
                        chat_id=GROUP, chat_type="group", text="work",
                        item_class=work_class)
                    # Even previously claimed ordinary talk is active work.
                    if work_class == queue_core.CLASS_CONVERSATION:
                        queue_core.claim_batch(state, platform="telegram")
                    telegram.ingest(state, {"ok": True, "result": [
                        msg_update(91, GROUP, "group", 222, "hello")
                    ]})
                    queue_core.find_item(state, "telegram", 91)["received_at"] -= 7200
                self.assertEqual(self.item(91)["item_class"], queue_core.CLASS_REQUEST)
                self.assertEqual(run_cli(self.path, ["--purge-expired"])["expired"], 0)
        with store.transaction() as state:
            for item in state["queue"]:
                item["status"] = queue_core.DONE
            # Work in a different chat does not block idle-room greetings.
            queue_core.enqueue(state, platform="telegram", external_id=92,
                               chat_id=OWNER, text="other lane work")
            telegram.ingest(state, {"ok": True, "result": [
                msg_update(93, GROUP, "group", 222, "hello")
            ]})
            queue_core.find_item(state, "telegram", 93)["received_at"] -= 7200
        self.assertEqual(self.item(93)["item_class"], queue_core.CLASS_CONVERSATION)
        self.assertEqual(run_cli(self.path, ["--purge-expired"])["external_ids"], ["93"])


class ExpiredContextTests(unittest.TestCase):
    """Finding 4 regression: group buffer entries carry their
    queue record's external_id, and an expired record's text is
    excluded from the actionable context of later wakes — expired
    work cannot re-enter through a fresh item's context."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"
        run_cli(self.path, ["--adopt", str(OWNER)])

    def tearDown(self):
        self.tmp.cleanup()

    def item(self, external_id):
        state = JsonStateStore(self.path).load()
        return next(
            i for i in state["queue"] if i["external_id"] == str(external_id)
        )

    def test_expired_item_is_absent_from_later_wake_context(self):
        # Finish the opener, then ingest ordinary talk without claiming it.
        first = run_cli(
            self.path, [],
            fetch=FakeFetch(
                [msg_update(10, GROUP, "group", OWNER, "room open")]
            ),
        )
        self.assertIsNotNone(first)
        run_cli(self.path, ["--done", "10", first["claim_token"]])
        with JsonStateStore(self.path).transaction() as state:
            telegram.ingest(state, {"ok": True, "result": [
                msg_update(11, GROUP, "group", 222, "こんにちは", "kai_bot", True),
                msg_update(12, GROUP, "group", 222, "こんばんは", "kai_bot", True),
            ]})
        # Buffer entries are tied to their queue records.
        state = JsonStateStore(self.path).load()
        by_text = {e["text"]: e for e in state["group_buffer"]}
        self.assertEqual(by_text["こんにちは"]["external_id"], "11")
        self.assertEqual(by_text["こんばんは"]["external_id"], "12")
        # Age only the first arrival, then purge: it expires; the
        # fresh one stays pending.
        store = JsonStateStore(self.path)
        with store.transaction() as state:
            queue_core.find_item(state, "telegram", 11)[
                "received_at"
            ] -= 7200
        result = run_cli(self.path, ["--purge-expired"])
        self.assertEqual(result["expired"], 1)
        self.assertEqual(result["external_ids"], ["11"])
        self.assertEqual(self.item(11)["status"], "done")
        self.assertEqual(self.item(11)["done_reason"], "expired")
        self.assertEqual(self.item(12)["status"], "pending")
        # A fresh arrival's wake keeps live context and drops expired talk.
        payload = run_cli(
            self.path, [],
            fetch=FakeFetch(
                [msg_update(13, GROUP, "group", OWNER, "fresh question")]
            ),
        )
        self.assertEqual(payload["count"], 2)
        for view in payload["items"]:
            texts = [entry["text"] for entry in view["context"]]
            self.assertIn("room open", texts)
            self.assertIn("こんばんは", texts)
            self.assertIn("fresh question", texts)
            self.assertNotIn("こんにちは", texts)


class NumericInputValidationTests(unittest.TestCase):
    """Finding 5 regression at the adapter layer: non-finite TTL /
    max-reply-age environment values fall back to the documented
    defaults before they can reach any mutation, while a finite
    negative keeps the disable convention."""

    def test_env_ttls_reject_non_finite_values(self):
        for bad in ("nan", "inf", "-inf"):
            with self.subTest(value=bad):
                with mock.patch.dict(
                    os.environ, {telegram.GROUP_PENDING_TTL_ENV: bad}
                ):
                    group_ttl, private_ttl = telegram._expiry_ttls()
                self.assertEqual(
                    group_ttl, queue_core.DEFAULT_GROUP_PENDING_TTL
                )
                self.assertIsNone(private_ttl)
                with mock.patch.dict(
                    os.environ, {telegram.MAX_REPLY_AGE_ENV: bad}
                ):
                    self.assertEqual(
                        telegram._max_reply_age(),
                        telegram.DEFAULT_MAX_REPLY_AGE,
                    )

    def test_env_ttls_keep_finite_and_negative_conventions(self):
        with mock.patch.dict(
            os.environ, {telegram.GROUP_PENDING_TTL_ENV: "120"}
        ):
            self.assertEqual(telegram._expiry_ttls()[0], 120.0)
        with mock.patch.dict(
            os.environ, {telegram.GROUP_PENDING_TTL_ENV: "-5"}
        ):
            self.assertIsNone(telegram._expiry_ttls()[0])
        with mock.patch.dict(
            os.environ, {telegram.PRIVATE_PENDING_TTL_ENV: "nan"}
        ):
            self.assertIsNone(telegram._expiry_ttls()[1])


if __name__ == "__main__":
    unittest.main()
