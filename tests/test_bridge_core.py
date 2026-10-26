"""Offline tests for the platform-neutral bridge core.

Covers the state store (locking, atomic save, fail-loud corruption
handling) and the queue's claim discipline (dedupe, one batch in
flight per conversation lane, claim tokens, the sending lifecycle,
replied items are never recycled, released or send-failed, stale
recycle of unreplied claims only, one conversation lane per batch). The concurrency test runs real parallel
processes against one state file, mirroring the poller + woken turns
+ operator commands that share the live bridge state.
"""
import json
import multiprocessing
import tempfile
import time
import unittest
from pathlib import Path

from bridge_core import queue as queue_core
from bridge_core.store import JsonStateStore, StateStoreError


def enqueue_n(state, n, platform="telegram", start=0):
    for i in range(start, start + n):
        queue_core.enqueue(
            state,
            platform=platform,
            external_id=1000 + i,
            chat_id=-1,
            chat_type="group",
            sender_name="@someone",
            text=f"message {i}",
        )


class StoreTests(unittest.TestCase):
    def test_transaction_persists_and_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            store = JsonStateStore(path)
            with store.transaction() as state:
                state["marker"] = 1
            self.assertTrue(path.exists())
            self.assertTrue(store.snapshot_path.exists())
            self.assertEqual(store.load()["marker"], 1)

    def test_p3_corrupt_main_state_raises_instead_of_rollback(self):
        """P3: a corrupt main file must fail loud. Silently adopting
        the .good snapshot would roll back completion/reply records
        and could resurrect already-answered items."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            store = JsonStateStore(path)
            with store.transaction() as state:
                state["marker"] = "good"
            path.write_text("{ this is not json", encoding="utf-8")
            with self.assertRaises(StateStoreError):
                store.load()
            with self.assertRaises(StateStoreError):
                with store.transaction():
                    pass

    def test_p3_missing_main_with_snapshot_raises(self):
        """P3: main file gone but .good present means probable
        deletion or corruption; the operator restores deliberately,
        the store never adopts the snapshot on its own."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            store = JsonStateStore(path)
            with store.transaction() as state:
                state["marker"] = 1
            self.assertTrue(store.snapshot_path.exists())
            path.unlink()
            with self.assertRaises(StateStoreError):
                store.load()

    def test_p3_fresh_deployment_starts_empty(self):
        """P3: with neither the main file nor a snapshot present, a
        brand-new deployment still starts from an empty state."""
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStateStore(Path(tmp) / "state.json")
            self.assertEqual(store.load(), {})
            with store.transaction() as state:
                state["marker"] = 1
            self.assertEqual(store.load()["marker"], 1)

    def test_p3_snapshot_write_failure_surfaces(self):
        """P3: if the .good snapshot cannot be written, the save
        fails loudly instead of pretending success while the
        operator's only restore source silently goes stale."""
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStateStore(Path(tmp) / "state.json")
            with store.transaction() as state:
                state["marker"] = 1
            # A directory where the snapshot file should land makes
            # the snapshot's atomic replace fail.
            store.snapshot_path.unlink()
            store.snapshot_path.mkdir()
            with self.assertRaises(OSError):
                with store.transaction() as state:
                    state["marker"] = 2

    def test_failed_transaction_saves_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStateStore(Path(tmp) / "state.json")
            with store.transaction() as state:
                state["marker"] = 1
            with self.assertRaises(RuntimeError):
                with store.transaction() as state:
                    state["marker"] = 2
                    raise RuntimeError("boom")
            self.assertEqual(store.load()["marker"], 1)


class QueueTests(unittest.TestCase):
    def test_dedupe_by_platform_and_external_id(self):
        state = {}
        first = queue_core.enqueue(
            state, platform="telegram", external_id=42, text="hi"
        )
        dupe = queue_core.enqueue(
            state, platform="telegram", external_id=42, text="hi again"
        )
        other = queue_core.enqueue(
            state, platform="slack", external_id=42, text="same id, other platform"
        )
        self.assertIsNotNone(first)
        self.assertIsNone(dupe)
        self.assertIsNotNone(other)
        self.assertEqual(len(state["queue"]), 2)

    def test_batch_claim_up_to_n_oldest_first(self):
        state = {}
        enqueue_n(state, 7)
        batch = queue_core.claim_batch(state, batch_size=5, now=1000.0)
        self.assertEqual(len(batch["items"]), 5)
        self.assertEqual(
            [item["external_id"] for item in batch["items"]],
            ["1000", "1001", "1002", "1003", "1004"],
        )
        for item in batch["items"]:
            self.assertEqual(item["status"], queue_core.CLAIMED)
            self.assertEqual(item["claim_token"], batch["claim_token"])
            self.assertEqual(item["claimed_at"], 1000.0)
            self.assertIsNotNone(item["received_at"])

    def test_claim_blocked_while_batch_in_flight(self):
        state = {}
        enqueue_n(state, 4)
        batch = queue_core.claim_batch(state, batch_size=2, now=1000.0)
        self.assertIsNotNone(batch)
        # Two items remain pending, but the in-flight batch blocks more.
        self.assertIsNone(queue_core.claim_batch(state, batch_size=2, now=1001.0))
        # Partial completion does not unblock either.
        first = batch["items"][0]
        self.assertTrue(
            queue_core.mark_done(
                state, "telegram", first["external_id"], batch["claim_token"]
            )
        )
        self.assertIsNone(queue_core.claim_batch(state, batch_size=2, now=1002.0))
        # Completing the batch unblocks the rest.
        second = batch["items"][1]
        self.assertTrue(
            queue_core.mark_done(
                state, "telegram", second["external_id"], batch["claim_token"]
            )
        )
        nxt = queue_core.claim_batch(state, batch_size=2, now=1003.0)
        self.assertEqual(len(nxt["items"]), 2)

    def test_wrong_token_done_and_release_are_noops(self):
        state = {}
        enqueue_n(state, 1)
        batch = queue_core.claim_batch(state, now=1000.0)
        item = batch["items"][0]
        self.assertFalse(
            queue_core.mark_done(state, "telegram", item["external_id"], "bogus")
        )
        self.assertFalse(
            queue_core.release(state, "telegram", item["external_id"], "bogus")
        )
        self.assertEqual(item["status"], queue_core.CLAIMED)
        self.assertTrue(
            queue_core.release(state, "telegram", item["external_id"], batch["claim_token"])
        )
        self.assertEqual(item["status"], queue_core.PENDING)
        self.assertIsNone(item["claimed_at"])

    def test_stale_recycle_only_unreplied(self):
        state = {}
        enqueue_n(state, 2)
        batch = queue_core.claim_batch(
            state, batch_size=2, stale_ttl=100.0, now=1000.0
        )
        replied, silent = batch["items"]
        self.assertTrue(
            queue_core.mark_replied(
                state, "telegram", replied["external_id"], 777, batch["claim_token"]
            )
        )
        self.assertEqual(replied["reply_message_id"], "777")
        self.assertIsNotNone(replied["replied_at"])
        # Long past the TTL: the replied item still blocks (never
        # recycled), so no new claim is possible yet.
        self.assertIsNone(
            queue_core.claim_batch(state, batch_size=2, stale_ttl=100.0, now=5000.0)
        )
        self.assertEqual(replied["status"], queue_core.CLAIMED)
        # But the unreplied sibling was recycled back to pending.
        self.assertEqual(silent["status"], queue_core.PENDING)
        self.assertIsNone(silent["claim_token"])
        # Closing the replied item unblocks the recycled one.
        self.assertTrue(
            queue_core.mark_done(
                state, "telegram", replied["external_id"], batch["claim_token"]
            )
        )
        nxt = queue_core.claim_batch(state, batch_size=2, stale_ttl=100.0, now=5001.0)
        self.assertEqual(
            [item["external_id"] for item in nxt["items"]], [silent["external_id"]]
        )

    def test_fresh_claim_is_not_recycled(self):
        state = {}
        enqueue_n(state, 1)
        queue_core.claim_batch(state, stale_ttl=100.0, now=1000.0)
        # 50s < TTL: still in flight, no recycle, no second claim.
        self.assertIsNone(
            queue_core.claim_batch(state, stale_ttl=100.0, now=1050.0)
        )

    def test_p1_sending_item_is_never_recycled_and_blocks_batches(self):
        """P1: an item in 'sending' is never recycled, however old —
        its send outcome is unknown — and it keeps the batch in
        flight so nothing else is claimed behind it."""
        state = {}
        enqueue_n(state, 3)
        batch = queue_core.claim_batch(
            state, batch_size=2, stale_ttl=100.0, now=1000.0
        )
        sending, sibling = batch["items"]
        self.assertTrue(
            queue_core.mark_sending(
                state, "telegram", sending["external_id"],
                batch["claim_token"], now=1001.0,
            )
        )
        self.assertEqual(sending["status"], queue_core.SENDING)
        self.assertEqual(sending["sending_at"], 1001.0)
        # Far past the TTL: the unreplied claimed sibling recycles to
        # pending, but the sending item does not — and it still
        # blocks any new claim.
        self.assertIsNone(
            queue_core.claim_batch(
                state, batch_size=2, stale_ttl=100.0, now=99999.0
            )
        )
        self.assertEqual(sending["status"], queue_core.SENDING)
        self.assertEqual(sibling["status"], queue_core.PENDING)
        # Operator reconciliation (closing it, with the evidence F2
        # requires for a send-unknown item) unblocks the queue.
        self.assertTrue(
            queue_core.mark_done(
                state, "telegram", sending["external_id"],
                batch["claim_token"],
                evidence="history: no reply found in the chat",
            )
        )
        nxt = queue_core.claim_batch(
            state, batch_size=2, stale_ttl=100.0, now=100000.0
        )
        self.assertEqual(
            {item["external_id"] for item in nxt["items"]},
            {sibling["external_id"], "1002"},
        )

    def test_p1_mark_sending_requires_ownership(self):
        """P1: mark_sending applies only to a claimed item and only
        with the batch's token; it is a one-way transition."""
        state = {}
        enqueue_n(state, 1)
        item = state["queue"][0]
        # Still pending: nothing to send yet.
        self.assertFalse(
            queue_core.mark_sending(state, "telegram", item["external_id"], "x")
        )
        batch = queue_core.claim_batch(state, now=1000.0)
        self.assertFalse(
            queue_core.mark_sending(
                state, "telegram", item["external_id"], "bogus"
            )
        )
        self.assertEqual(item["status"], queue_core.CLAIMED)
        self.assertTrue(
            queue_core.mark_sending(
                state, "telegram", item["external_id"], batch["claim_token"]
            )
        )
        # Already sending: a second mark_sending is a no-op.
        self.assertFalse(
            queue_core.mark_sending(
                state, "telegram", item["external_id"], batch["claim_token"]
            )
        )

    def test_mark_sending_refuses_replied_item(self):
        """A claimed item with a recorded reply must not move to
        'sending': its reply already went out, so entering the send
        lifecycle would expose an answered item to a second send.
        mark_sending refuses and changes nothing — the same guard
        mark_send_failed has."""
        state = {}
        enqueue_n(state, 1)
        batch = queue_core.claim_batch(state, now=1000.0)
        item = batch["items"][0]
        token = batch["claim_token"]
        self.assertTrue(
            queue_core.mark_replied(
                state, "telegram", item["external_id"], 42, token
            )
        )
        self.assertFalse(
            queue_core.mark_sending(state, "telegram", item["external_id"], token)
        )
        self.assertEqual(item["status"], queue_core.CLAIMED)
        self.assertIsNone(item["sending_at"])
        self.assertIsNotNone(item["replied_at"])

    def test_p1_send_failed_only_from_sending_with_token(self):
        """P1: mark_send_failed is the only way back from 'sending',
        and only with the batch token (a definitive API failure)."""
        state = {}
        enqueue_n(state, 1)
        batch = queue_core.claim_batch(state, now=1000.0)
        item = batch["items"][0]
        token = batch["claim_token"]
        # Not yet sending: does not apply.
        self.assertFalse(
            queue_core.mark_send_failed(state, "telegram", item["external_id"], token)
        )
        self.assertTrue(
            queue_core.mark_sending(state, "telegram", item["external_id"], token)
        )
        # Wrong token: refused, the item stays in sending.
        self.assertFalse(
            queue_core.mark_send_failed(state, "telegram", item["external_id"], "bogus")
        )
        self.assertEqual(item["status"], queue_core.SENDING)
        # Right token: back to pending and claimable again.
        self.assertTrue(
            queue_core.mark_send_failed(state, "telegram", item["external_id"], token)
        )
        self.assertEqual(item["status"], queue_core.PENDING)
        self.assertIsNone(item["claim_token"])
        self.assertIsNone(item["sending_at"])
        nxt = queue_core.claim_batch(state, now=2000.0)
        self.assertEqual(nxt["items"][0]["external_id"], item["external_id"])

    def test_p1_recycled_batch_rejects_stale_turn_calls(self):
        """P1 (generation safety): after a stale claim is recycled
        and the item re-claimed under a new token, the dead turn's
        old token authorizes nothing any more."""
        state = {}
        enqueue_n(state, 1)
        first = queue_core.claim_batch(state, stale_ttl=100.0, now=1000.0)
        item = first["items"][0]
        second = queue_core.claim_batch(state, stale_ttl=100.0, now=5000.0)
        self.assertIsNotNone(second)
        self.assertNotEqual(first["claim_token"], second["claim_token"])
        old = first["claim_token"]
        self.assertFalse(
            queue_core.mark_sending(state, "telegram", item["external_id"], old)
        )
        self.assertFalse(
            queue_core.mark_replied(state, "telegram", item["external_id"], 1, old)
        )
        self.assertFalse(
            queue_core.mark_done(state, "telegram", item["external_id"], old)
        )
        self.assertFalse(
            queue_core.release(state, "telegram", item["external_id"], old)
        )
        self.assertFalse(
            queue_core.mark_send_failed(state, "telegram", item["external_id"], old)
        )
        self.assertEqual(item["status"], queue_core.CLAIMED)
        self.assertIsNone(item["replied_at"])

    def test_p2_release_refuses_replied_and_sending_items(self):
        """P2: release must not resurrect an item whose reply already
        went out (replied_at set) or that is mid-send."""
        state = {}
        enqueue_n(state, 2)
        batch = queue_core.claim_batch(state, batch_size=2, now=1000.0)
        replied, sending = batch["items"]
        token = batch["claim_token"]
        self.assertTrue(
            queue_core.mark_replied(
                state, "telegram", replied["external_id"], 42, token
            )
        )
        self.assertFalse(
            queue_core.release(state, "telegram", replied["external_id"], token)
        )
        self.assertEqual(replied["status"], queue_core.CLAIMED)
        self.assertIsNotNone(replied["replied_at"])
        self.assertTrue(
            queue_core.mark_sending(state, "telegram", sending["external_id"], token)
        )
        self.assertFalse(
            queue_core.release(state, "telegram", sending["external_id"], token)
        )
        self.assertEqual(sending["status"], queue_core.SENDING)
        # Neither item can be claimed again while unresolved.
        self.assertIsNone(queue_core.claim_batch(state, now=1001.0))

    def test_p4_mark_replied_requires_matching_token(self):
        """P4: mark_replied without a token, or with a wrong one,
        records nothing — a worker that does not own the claim
        cannot record a reply on it."""
        state = {}
        enqueue_n(state, 1)
        batch = queue_core.claim_batch(state, now=1000.0)
        item = batch["items"][0]
        self.assertFalse(
            queue_core.mark_replied(state, "telegram", item["external_id"], 42, None)
        )
        self.assertIsNone(item["replied_at"])
        self.assertFalse(
            queue_core.mark_replied(
                state, "telegram", item["external_id"], 42, "bogus"
            )
        )
        self.assertIsNone(item["replied_at"])
        self.assertIsNone(item["reply_message_id"])
        self.assertTrue(
            queue_core.mark_replied(
                state, "telegram", item["external_id"], 42, batch["claim_token"]
            )
        )
        self.assertEqual(item["reply_message_id"], "42")
        self.assertIsNotNone(item["replied_at"])

    def test_done_records_completion_reason(self):
        """Closing an item with a reason stores done_reason, so an
        audit can tell silently-closed items from answered ones;
        closing without a reason leaves the field absent."""
        state = {}
        enqueue_n(state, 2)
        batch = queue_core.claim_batch(state, now=1000.0)
        first, second = batch["items"]
        self.assertTrue(
            queue_core.mark_done(
                state, "telegram", first["external_id"],
                batch["claim_token"], reason="superseded",
            )
        )
        self.assertEqual(first["done_reason"], "superseded")
        self.assertTrue(
            queue_core.mark_done(
                state, "telegram", second["external_id"], batch["claim_token"]
            )
        )
        self.assertNotIn("done_reason", second)

    def test_f1_replied_item_cannot_be_send_failed_or_reclaimed(self):
        """F1: mark_send_failed must refuse a replied item — after a
        successful send the item sits in 'sending' with replied_at
        set until mark_done, and bouncing it to pending would let a
        later claim answer it twice. Defense in depth: even if a
        replied item somehow sits in pending (legacy or hand-edited
        state), claim_batch never selects it."""
        state = {}
        enqueue_n(state, 1)
        batch = queue_core.claim_batch(state, now=1000.0)
        item = batch["items"][0]
        token = batch["claim_token"]
        self.assertTrue(
            queue_core.mark_sending(state, "telegram", item["external_id"], token)
        )
        self.assertTrue(
            queue_core.mark_replied(state, "telegram", item["external_id"], 42, token)
        )
        # The reply went out: send-failed is refused, nothing changes.
        self.assertFalse(
            queue_core.mark_send_failed(state, "telegram", item["external_id"], token)
        )
        self.assertEqual(item["status"], queue_core.SENDING)
        self.assertIsNotNone(item["replied_at"])
        self.assertEqual(item["claim_token"], token)
        # Simulate a legacy/hand-edited state where the replied item
        # sits in pending: a claim must skip it entirely and take
        # only the genuinely unanswered item in the other lane.
        item["status"] = queue_core.PENDING
        queue_core.enqueue(
            state, platform="telegram", external_id="other", chat_id=999,
            chat_type="private", text="other",
        )
        nxt = queue_core.claim_batch(state, platform="telegram", now=2000.0)
        self.assertEqual(
            [i["external_id"] for i in nxt["items"]], ["other"]
        )
        self.assertEqual(item["status"], queue_core.PENDING)
        self.assertEqual(item["claim_token"], token)
        # And with only the replied item left pending, nothing claims.
        self.assertIsNone(queue_core.claim_batch(state, platform="telegram", now=3000.0))

    def test_f2_done_from_sending_requires_evidence_without_reply_record(self):
        """F2 (evidence rule): closing a 'sending' item with no reply
        record drops a send-unknown item, so mark_done requires
        reconciliation EVIDENCE there — a reference to how the send
        outcome was established. A reason alone no longer suffices,
        and without evidence the call changes nothing. Given
        evidence is stored as done_evidence, separate from the
        optional done_reason. A replied sending item closes
        normally, and a claimed (never sent) item needs neither (see
        test_done_records_completion_reason)."""
        state = {}
        enqueue_n(state, 2)
        batch = queue_core.claim_batch(state, batch_size=2, now=1000.0)
        unknown, replied = batch["items"]
        token = batch["claim_token"]
        for item in (unknown, replied):
            self.assertTrue(
                queue_core.mark_sending(
                    state, "telegram", item["external_id"], token
                )
            )
        self.assertTrue(
            queue_core.mark_replied(
                state, "telegram", replied["external_id"], 7, token
            )
        )
        # No evidence: refused, item untouched. A reason alone is
        # refused too — it is context, not reconciliation evidence.
        self.assertFalse(
            queue_core.mark_done(state, "telegram", unknown["external_id"], token)
        )
        self.assertFalse(
            queue_core.mark_done(
                state, "telegram", unknown["external_id"], token,
                reason="send-outcome-unknown",
            )
        )
        self.assertFalse(
            queue_core.mark_done(
                state, "telegram", unknown["external_id"], token, evidence=""
            )
        )
        self.assertEqual(unknown["status"], queue_core.SENDING)
        self.assertIsNone(unknown["done_at"])
        self.assertNotIn("done_reason", unknown)
        self.assertNotIn("done_evidence", unknown)
        # With evidence: closes. The evidence is stored as
        # done_evidence, kept separate from the optional reason.
        self.assertTrue(
            queue_core.mark_done(
                state, "telegram", unknown["external_id"], token,
                reason="reconciled-from-history",
                evidence="history: reply found as message 7 in the chat",
            )
        )
        self.assertEqual(unknown["status"], queue_core.DONE)
        self.assertEqual(
            unknown["done_evidence"],
            "history: reply found as message 7 in the chat",
        )
        self.assertEqual(unknown["done_reason"], "reconciled-from-history")
        # A replied sending item closes normally, no evidence needed.
        self.assertTrue(
            queue_core.mark_done(state, "telegram", replied["external_id"], token)
        )
        self.assertEqual(replied["status"], queue_core.DONE)

    def test_f3_in_flight_guard_is_scoped_to_the_lane(self):
        """F3: a batch in flight blocks only its own (platform, chat)
        lane. Another chat on the same platform still claims, a
        different platform is unaffected even with the same chat id,
        and the busy lane itself does not double-claim."""
        state = {}
        queue_core.enqueue(
            state, platform="telegram", external_id="a1", chat_id=-100,
            chat_type="group", text="a1", now=10.0,
        )
        queue_core.enqueue(
            state, platform="telegram", external_id="b1", chat_id=555,
            chat_type="private", text="b1", now=11.0,
        )
        queue_core.enqueue(
            state, platform="slack", external_id="s1", chat_id=-100,
            text="s1", now=12.0,
        )
        by_id = {i["external_id"]: i for i in state["queue"]}
        batch_a = queue_core.claim_batch(
            state, platform="telegram", batch_size=5, now=100.0
        )
        self.assertEqual(
            [i["external_id"] for i in batch_a["items"]], ["a1"]
        )
        # Lane A is in flight, but the telegram DM lane still claims.
        batch_b = queue_core.claim_batch(
            state, platform="telegram", batch_size=5, now=101.0
        )
        self.assertEqual(
            [i["external_id"] for i in batch_b["items"]], ["b1"]
        )
        # A new item in the busy lane A does not double-claim.
        queue_core.enqueue(
            state, platform="telegram", external_id="a2", chat_id=-100,
            chat_type="group", text="a2", now=13.0,
        )
        self.assertIsNone(
            queue_core.claim_batch(state, platform="telegram", now=102.0)
        )
        a2 = queue_core.find_item(state, "telegram", "a2")
        self.assertEqual(a2["status"], queue_core.PENDING)
        # A different platform is a different lane even with the
        # same chat id: the slack item claims despite lane A.
        slack_batch = queue_core.claim_batch(
            state, platform="slack", batch_size=5, now=103.0
        )
        self.assertEqual(
            [i["external_id"] for i in slack_batch["items"]], ["s1"]
        )
        self.assertEqual(by_id["a1"]["status"], queue_core.CLAIMED)
        self.assertEqual(by_id["b1"]["status"], queue_core.CLAIMED)

    def test_f3_busy_oldest_lane_does_not_block_free_lane(self):
        """F3 (head-of-line): lane selection skips busy lanes. With
        lane A in flight, an OLDER pending item in A must not block
        a NEWER pending item in free lane B — B is claimed. When
        every pending item's lane is busy, the claim is None."""
        state = {}
        queue_core.enqueue(
            state, platform="telegram", external_id="a1", chat_id=-100,
            chat_type="group", text="a1", now=10.0,
        )
        queue_core.enqueue(
            state, platform="telegram", external_id="a2", chat_id=-100,
            chat_type="group", text="a2", now=11.0,
        )
        queue_core.enqueue(
            state, platform="telegram", external_id="b1", chat_id=555,
            chat_type="private", text="b1", now=12.0,
        )
        # Claim only a1 so lane A is in flight while a2 (older) and
        # b1 (newer) both stay pending.
        first = queue_core.claim_batch(
            state, platform="telegram", batch_size=1, now=100.0
        )
        self.assertEqual(
            [i["external_id"] for i in first["items"]], ["a1"]
        )
        # The oldest pending item (a2) sits in busy lane A; the
        # claim skips that lane and takes free lane B instead of
        # returning None.
        second = queue_core.claim_batch(
            state, platform="telegram", batch_size=5, now=101.0
        )
        self.assertEqual(
            [i["external_id"] for i in second["items"]], ["b1"]
        )
        a2 = queue_core.find_item(state, "telegram", "a2")
        self.assertEqual(a2["status"], queue_core.PENDING)
        # Only a2 is left pending and its lane is busy: no claim.
        self.assertIsNone(
            queue_core.claim_batch(state, platform="telegram", now=102.0)
        )
        self.assertEqual(a2["status"], queue_core.PENDING)

    def test_p5_claim_batch_takes_one_lane_for_requested_platform(self):
        """P5: with a mixed-platform, mixed-chat backlog, a claim
        for one platform takes only the (platform, chat) lane of
        that platform's oldest pending item."""
        state = {}
        queue_core.enqueue(
            state, platform="telegram", external_id="g1", chat_id=-100,
            chat_type="group", text="g1", now=10.0,
        )
        queue_core.enqueue(
            state, platform="telegram", external_id="d1", chat_id=555,
            chat_type="private", text="d1", now=11.0,
        )
        queue_core.enqueue(
            state, platform="slack", external_id="s1", chat_id=-100,
            text="s1", now=12.0,
        )
        queue_core.enqueue(
            state, platform="telegram", external_id="g2", chat_id=-100,
            chat_type="group", text="g2", now=13.0,
        )
        queue_core.enqueue(
            state, platform="telegram", external_id="g3", chat_id=-100,
            chat_type="group", text="g3", now=14.0,
        )
        batch = queue_core.claim_batch(
            state, platform="telegram", batch_size=2, now=100.0
        )
        # Only the telegram group lane, oldest first, capped at 2.
        self.assertEqual(
            [item["external_id"] for item in batch["items"]], ["g1", "g2"]
        )
        by_id = {item["external_id"]: item for item in state["queue"]}
        self.assertEqual(by_id["d1"]["status"], queue_core.PENDING)
        self.assertEqual(by_id["s1"]["status"], queue_core.PENDING)
        self.assertEqual(by_id["g3"]["status"], queue_core.PENDING)
        # Finish the batch: the next telegram claim takes the DM lane
        # (oldest remaining telegram item), still not slack, and not
        # the leftover group item.
        for item in batch["items"]:
            self.assertTrue(
                queue_core.mark_done(
                    state, "telegram", item["external_id"], batch["claim_token"]
                )
            )
        nxt = queue_core.claim_batch(
            state, platform="telegram", batch_size=5, now=200.0
        )
        self.assertEqual(
            [item["external_id"] for item in nxt["items"]], ["d1"]
        )
        self.assertEqual(by_id["g3"]["status"], queue_core.PENDING)
        # A slack claim in turn takes only the slack lane.
        self.assertTrue(
            queue_core.mark_done(state, "telegram", "d1", nxt["claim_token"])
        )
        slack_batch = queue_core.claim_batch(
            state, platform="slack", batch_size=5, now=300.0
        )
        self.assertEqual(
            [item["external_id"] for item in slack_batch["items"]], ["s1"]
        )


class PendingExpiryTests(unittest.TestCase):
    """Issue #17: the claim-time expiry sweep closes stale
    unclaimed pendings by class TTL (group conversation expires,
    owner DMs do not by default) and never touches claimed,
    sending or replied items."""

    def _seed(self, state):
        # Ages are evaluated against now=10_000.0 in the tests.
        queue_core.enqueue(
            state, platform="telegram", external_id="g-old",
            chat_id=-100, chat_type="group", text="old", now=9_000.0,
        )
        queue_core.enqueue(
            state, platform="telegram", external_id="g-fresh",
            chat_id=-100, chat_type="group", text="fresh", now=9_700.0,
        )
        queue_core.enqueue(
            state, platform="telegram", external_id="sg-old",
            chat_id=-200, chat_type="supergroup", text="old", now=9_000.0,
        )
        queue_core.enqueue(
            state, platform="telegram", external_id="dm-old",
            chat_id=555, chat_type="private", text="dm", now=1_000.0,
        )
        queue_core.enqueue(
            state, platform="telegram", external_id="untyped",
            chat_id=-300, text="no chat type", now=1_000.0,
        )
        queue_core.enqueue(
            state, platform="slack", external_id="s-old",
            chat_id=-100, chat_type="group", text="slack", now=9_000.0,
        )

    def test_sweep_expires_only_eligible_pendings(self):
        state = {}
        self._seed(state)
        by_id = {i["external_id"]: i for i in state["queue"]}
        # A dry run reports the eligible items and mutates nothing.
        would = queue_core.expire_stale_pending(
            state, platform="telegram", dry_run=True, now=10_000.0
        )
        self.assertEqual(
            {i["external_id"] for i in would}, {"g-old", "sg-old"}
        )
        self.assertTrue(
            all(i["status"] == queue_core.PENDING for i in state["queue"])
        )
        expired = queue_core.expire_stale_pending(
            state, platform="telegram", now=10_000.0
        )
        self.assertEqual(
            {i["external_id"] for i in expired}, {"g-old", "sg-old"}
        )
        for external_id in ("g-old", "sg-old"):
            item = by_id[external_id]
            self.assertEqual(item["status"], queue_core.DONE)
            self.assertEqual(item["done_reason"], "expired")
            self.assertEqual(item["done_at"], 10_000.0)
            self.assertIsNone(item["claim_token"])
        # The fresh group item, the old DM (no default TTL), the
        # untyped item and the other platform's item are untouched.
        for external_id in ("g-fresh", "dm-old", "untyped", "s-old"):
            self.assertEqual(by_id[external_id]["status"], queue_core.PENDING)
            self.assertNotIn("done_reason", by_id[external_id])
        self.assertEqual(queue_core.counts(state)["done"], 2)
        # A second sweep finds nothing left to expire.
        self.assertEqual(
            queue_core.expire_stale_pending(
                state, platform="telegram", now=10_001.0
            ),
            [],
        )

    def test_sweep_never_expires_claimed_sending_or_replied(self):
        state = {}
        for i, ext in enumerate(("c1", "c2", "c3", "c4")):
            queue_core.enqueue(
                state, platform="telegram", external_id=ext,
                chat_id=-100, chat_type="group", text=ext, now=1_000.0 + i,
            )
        # The setup claim disables the sweep (group_pending_ttl=None)
        # so the protected states can be arranged at all.
        batch = queue_core.claim_batch(
            state, platform="telegram", batch_size=4,
            group_pending_ttl=None, now=2_000.0,
        )
        token = batch["claim_token"]
        by_id = {i["external_id"]: i for i in state["queue"]}
        self.assertTrue(
            queue_core.mark_sending(state, "telegram", "c2", token)
        )
        self.assertTrue(
            queue_core.mark_replied(state, "telegram", "c3", 42, token)
        )
        self.assertTrue(
            queue_core.mark_done(state, "telegram", "c4", token)
        )
        # A pending item with a recorded reply (legacy or
        # hand-edited state) is also left for operator
        # reconciliation, exactly as claim_batch leaves it.
        queue_core.enqueue(
            state, platform="telegram", external_id="legacy",
            chat_id=-100, chat_type="group", text="legacy", now=1_500.0,
        )
        legacy = queue_core.find_item(state, "telegram", "legacy")
        legacy["replied_at"] = 1_600.0
        # Far past every TTL: nothing in a protected state expires.
        expired = queue_core.expire_stale_pending(
            state, platform="telegram", now=10_000_000.0
        )
        self.assertEqual(expired, [])
        self.assertEqual(by_id["c1"]["status"], queue_core.CLAIMED)
        self.assertEqual(by_id["c2"]["status"], queue_core.SENDING)
        self.assertEqual(by_id["c3"]["status"], queue_core.CLAIMED)
        self.assertIsNotNone(by_id["c3"]["replied_at"])
        self.assertEqual(by_id["c4"]["status"], queue_core.DONE)
        self.assertNotIn("done_reason", by_id["c4"])
        self.assertEqual(legacy["status"], queue_core.PENDING)

    def test_claim_batch_sweeps_before_claiming(self):
        """The live backlog pattern: stale group pendings are
        closed by the claim itself, and only the fresh item wakes."""
        state = {}
        for i in range(3):
            queue_core.enqueue(
                state, platform="telegram", external_id=f"stale-{i}",
                chat_id=-100, chat_type="group", text="stale",
                now=1_000.0 + i,
            )
        queue_core.enqueue(
            state, platform="telegram", external_id="fresh",
            chat_id=-100, chat_type="group", text="fresh", now=9_950.0,
        )
        batch = queue_core.claim_batch(
            state, platform="telegram", now=10_000.0
        )
        self.assertEqual(
            [i["external_id"] for i in batch["items"]], ["fresh"]
        )
        for item in state["queue"]:
            if item["external_id"].startswith("stale-"):
                self.assertEqual(item["status"], queue_core.DONE)
                self.assertEqual(item["done_reason"], "expired")
        result = queue_core.counts(state)
        self.assertEqual(result["done"], 3)
        self.assertEqual(result["pending"], 0)
        # Nothing left to claim.
        self.assertIsNone(
            queue_core.claim_batch(state, platform="telegram", now=10_001.0)
        )

    def test_pending_ttl_is_configurable_per_class(self):
        state = {}
        queue_core.enqueue(
            state, platform="telegram", external_id="dm",
            chat_id=555, chat_type="private", text="dm", now=1_000.0,
        )
        queue_core.enqueue(
            state, platform="telegram", external_id="g",
            chat_id=-100, chat_type="group", text="g", now=9_800.0,
        )
        # Defaults: the DM never expires, and the 200s-old group
        # item is under the 600s default TTL.
        self.assertEqual(
            queue_core.expire_stale_pending(state, now=10_000.0), []
        )
        # Opt-in DM expiry plus a shorter group TTL expire both.
        expired = queue_core.expire_stale_pending(
            state, group_ttl=60.0, private_ttl=60.0, now=10_000.0
        )
        self.assertEqual(
            {i["external_id"] for i in expired}, {"dm", "g"}
        )
        # A None group TTL disables group expiry entirely.
        state2 = {}
        queue_core.enqueue(
            state2, platform="telegram", external_id="g2",
            chat_id=-100, chat_type="group", text="g", now=1_000.0,
        )
        self.assertEqual(
            queue_core.expire_stale_pending(
                state2, group_ttl=None, now=10_000.0
            ),
            [],
        )


def _claim_worker(state_path: str, worker_id: int, result_dir: str) -> None:
    """Claim batches and finish them until the queue drains.

    Claim and done happen in separate transactions, exactly like the
    hook poll (claim) and the woken turn (done) in production.
    """
    store = JsonStateStore(state_path)
    finished = []
    deadline = time.time() + 120
    while time.time() < deadline:
        with store.transaction() as state:
            batch = queue_core.claim_batch(state, batch_size=2)
        if batch is None:
            current = queue_core.counts(store.load())
            if current["pending"] == 0 and current["claimed"] == 0:
                break
            time.sleep(0.02)
            continue
        for item in batch["items"]:
            with store.transaction() as state:
                ok = queue_core.mark_done(
                    state, item["platform"], item["external_id"], batch["claim_token"]
                )
            if not ok:
                raise AssertionError(f"done rejected for {item['external_id']}")
            finished.append(item["external_id"])
    Path(result_dir, f"worker-{worker_id}.json").write_text(
        json.dumps(finished), encoding="utf-8"
    )


class ConcurrencyTests(unittest.TestCase):
    def test_parallel_processes_never_corrupt_or_double_claim(self):
        total = 30
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStateStore(Path(tmp) / "state.json")
            with store.transaction() as state:
                enqueue_n(state, total)
            ctx = multiprocessing.get_context("fork")
            procs = [
                ctx.Process(
                    target=_claim_worker,
                    args=(str(store.path), wid, tmp),
                )
                for wid in range(6)
            ]
            for proc in procs:
                proc.start()
            for proc in procs:
                proc.join(120)
                self.assertEqual(proc.exitcode, 0)
            # The state file is intact and every item is done.
            raw = json.loads(store.path.read_text(encoding="utf-8"))
            self.assertEqual(len(raw["queue"]), total)
            final = queue_core.counts(store.load())
            self.assertEqual(
                final, {"pending": 0, "claimed": 0, "sending": 0, "done": total}
            )
            # No item was finished by two workers (no double claim).
            seen = []
            for wid in range(6):
                seen += json.loads(
                    Path(tmp, f"worker-{wid}.json").read_text(encoding="utf-8")
                )
            self.assertEqual(len(seen), total)
            self.assertEqual(len(set(seen)), total)


if __name__ == "__main__":
    unittest.main()
