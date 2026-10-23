"""Offline tests for the M1 listener skeleton (fake web client, no network)."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge.config import load_config
from bridge.listener import Listener
from bridge.store import EventStore

CONFIG_YAML = """
deployment: test
ackReaction: eyes
audiences:
  - id: owner
    scope: owner
    userIds: ["U_OWNER"]
    channelAllowlist: ["C_PRIVATE"]
    dmEnabled: true
    mentionsOnlyInChannels: true
  - id: guest1
    scope: shared:guest1
    userIds: ["U_GUEST"]
    channelAllowlist: ["C_GUEST"]
    dmEnabled: false
    mentionsOnlyInChannels: true
"""

BOT_ID = "U_BOT"


class FakeWeb:
    def __init__(self):
        self.reactions = []

    def reactions_add(self, channel, timestamp, name):
        self.reactions.append((channel, timestamp, name))


def envelope(event, event_id="Ev1"):
    return {
        "envelope_id": f"env-{event_id}",
        "type": "events_api",
        "payload": {"event_id": event_id, "event": event},
    }


def dm_event(user, text="hello", ts="100.1"):
    return {"type": "message", "user": user, "channel": "D1",
            "channel_type": "im", "text": text, "ts": ts}


def channel_event(user, channel, text, ts="200.1", etype="message"):
    return {"type": etype, "user": user, "channel": channel,
            "channel_type": "channel", "text": text, "ts": ts}


class ListenerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        cfg_path = root / "config.yaml"
        cfg_path.write_text(CONFIG_YAML, encoding="utf-8")
        self.cfg = load_config(cfg_path)
        self.store = EventStore(root / "state")
        self.web = FakeWeb()
        self.listener = Listener(self.cfg, self.store, self.web, BOT_ID)
        self.acks = []

    def tearDown(self):
        self.tmp.cleanup()

    def handle(self, env):
        self.listener.handle_envelope(env, self.acks.append)

    def test_owner_dm_queued_with_owner_scope_and_reaction(self):
        self.handle(envelope(dm_event("U_OWNER")))
        queue = self.store.read_queue()
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["scope"], "owner")
        self.assertEqual(queue[0]["audience_id"], "owner")
        self.assertEqual(self.web.reactions, [("D1", "100.1", "eyes")])
        self.assertEqual(self.acks, ["env-Ev1"])  # envelope ack still sent

    def test_stranger_dropped_silently(self):
        self.handle(envelope(dm_event("U_STRANGER")))
        self.assertEqual(self.store.read_queue(), [])
        self.assertEqual(self.web.reactions, [])  # no reaction for strangers

    def test_duplicate_event_id_queued_once(self):
        env = envelope(dm_event("U_OWNER"))
        self.handle(env)
        self.handle(env)
        self.assertEqual(len(self.store.read_queue()), 1)
        self.assertEqual(len(self.web.reactions), 1)

    def test_channel_requires_mention_unless_app_mention(self):
        self.handle(envelope(channel_event("U_OWNER", "C_PRIVATE", "just chatting"), "Ev2"))
        self.assertEqual(self.store.read_queue(), [])
        self.handle(envelope(channel_event("U_OWNER", "C_PRIVATE", f"hi <@{BOT_ID}>"), "Ev3"))
        self.handle(envelope(channel_event("U_OWNER", "C_PRIVATE", "hi", etype="app_mention"), "Ev4"))
        self.assertEqual(len(self.store.read_queue()), 2)

    def test_channel_not_in_audience_allowlist_dropped(self):
        self.handle(envelope(channel_event("U_OWNER", "C_GUEST", f"<@{BOT_ID}> hi"), "Ev5"))
        self.assertEqual(self.store.read_queue(), [])

    def test_guest_scoped_to_own_domain(self):
        self.handle(envelope(channel_event("U_GUEST", "C_GUEST", f"<@{BOT_ID}> hi"), "Ev6"))
        queue = self.store.read_queue()
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["scope"], "shared:guest1")

    def test_guest_dm_disabled(self):
        self.handle(envelope(dm_event("U_GUEST"), "Ev7"))
        self.assertEqual(self.store.read_queue(), [])

    def test_bot_message_ignored(self):
        event = dm_event("U_OWNER")
        event["bot_id"] = "B1"
        event["subtype"] = "bot_message"
        self.handle(envelope(event, "Ev8"))
        self.assertEqual(self.store.read_queue(), [])


class ConfigTests(unittest.TestCase):
    def test_config_rejects_token_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(
                "deployment: x\nbotToken: xoxb-fake\naudiences:\n"
                "  - {id: owner, scope: owner, userIds: ['U1']}\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_config(path)

    def test_config_rejects_mismatched_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(
                "audiences:\n  - {id: a, scope: shared:other, userIds: ['U1']}\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
