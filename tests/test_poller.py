"""Offline tests for the polling intake (fake Slack API, no network)."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bridge.poller as poller_module
from bridge.config import load_config
from bridge.store import EventStore

CONFIG_YAML = """
deployment: test
ackReaction: null
audiences:
  - id: owner
    scope: owner
    userIds: ["U_OWNER"]
    channelAllowlist: []
    dmEnabled: true
    mentionsOnlyInChannels: true
"""


class FakeSlack:
    def __init__(self):
        self.messages = []

    def __call__(self, method, params):
        if method == "auth.test":
            return {"ok": True, "user_id": "U_BOT", "bot_id": "B_SELF"}
        if method == "conversations.open":
            return {"ok": True, "channel": {"id": "D_OWNER"}}
        if method == "conversations.history":
            return {"ok": True, "messages": list(self.messages)}
        raise AssertionError(method)


class PollerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        cfg_path = root / "config.yaml"
        cfg_path.write_text(CONFIG_YAML, encoding="utf-8")
        self.store = EventStore(root / "state")
        self.fake = FakeSlack()
        self.original_api = poller_module.api
        poller_module.api = self.fake
        self.poller = poller_module.Poller(
            load_config(cfg_path), self.store, root / "state" / "poller.json"
        )

    def tearDown(self):
        poller_module.api = self.original_api
        self.tmp.cleanup()

    def test_owner_message_queued_in_worker_format(self):
        self.fake.messages = [
            {"ts": "10.5", "user": "U_OWNER", "text": "hello"}
        ]
        self.assertEqual(self.poller.poll_once(), 1)
        queue = self.store.read_queue()
        self.assertEqual(len(queue), 1)
        record = queue[0]
        self.assertEqual(record["event_id"], "slack:D_OWNER:10.5")
        self.assertEqual(record["scope"], "owner")
        self.assertEqual(record["thread_ts"], "10.5")

    def test_second_poll_does_not_duplicate(self):
        self.fake.messages = [{"ts": "10.5", "user": "U_OWNER", "text": "hi"}]
        self.poller.poll_once()
        self.assertEqual(self.poller.poll_once(), 0)
        self.assertEqual(len(self.store.read_queue()), 1)

    def test_other_users_and_bots_skipped_but_watermark_advances(self):
        self.fake.messages = [
            {"ts": "11.0", "user": "U_OTHER", "text": "not owner"},
            {"ts": "12.0", "user": "U_OWNER", "text": "bot echo", "bot_id": "B1"},
        ]
        self.assertEqual(self.poller.poll_once(), 0)
        self.assertEqual(self.store.read_queue(), [])
        # A later owner message is still picked up.
        self.fake.messages.append({"ts": "13.0", "user": "U_OWNER", "text": "real"})
        self.assertEqual(self.poller.poll_once(), 1)


if __name__ == "__main__":
    unittest.main()
