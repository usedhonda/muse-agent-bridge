"""Private local state and one-time owner bootstrap regression tests (offline)."""
import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from bridge_core.store import JsonStateStore, StateStoreError
from bridge_core import telegram


class StateSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "private-state" / "state.json"
        self.store = JsonStateStore(self.path)

    def test_new_state_directory_and_both_state_files_are_private(self):
        old_mask = os.umask(0o022)
        try:
            with self.store.transaction() as state:
                state["message"] = "private conversation"
        finally:
            os.umask(old_mask)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        for filename in (self.path, self.store.snapshot_path):
            with self.subTest(filename=filename.name):
                self.assertEqual(stat.S_IMODE(filename.stat().st_mode), 0o600)
                self.assertIn("private conversation", filename.read_text())
        self.assertEqual(list(self.path.parent.glob("*.tmp.*")), [])

    def test_existing_world_readable_state_is_private_after_new_write(self):
        self.path.write_text('{"message":"legacy"}', encoding="utf-8")
        self.path.chmod(0o644)
        with self.store.transaction() as state:
            state["updated"] = True
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.store.snapshot_path.stat().st_mode), 0o600)
        self.assertTrue(self.store.load()["updated"])

    def test_snapshot_is_never_silently_adopted_after_main_missing(self):
        with self.store.transaction() as state:
            state["status"] = "sent"
        self.path.unlink()
        with self.assertRaises(StateStoreError):
            self.store.load()

    def adopt(self, supplied):
        output = io.StringIO()
        with redirect_stdout(output):
            result = telegram.main(["--adopt", supplied], path=self.path)
        return result, json.loads(output.getvalue())

    def test_owner_idempotence_and_refusal_to_change_established_owner(self):
        self.assertEqual(self.adopt("111"), (0, {"owner_id": 111}))
        with self.store.transaction() as state:
            state["queue"] = [{"sample": "unanswered"}]
            state["candidates"] = [222]
        self.assertEqual(self.adopt("111"), (0, {"owner_id": 111}))
        code, response = self.adopt("222")
        self.assertEqual(code, 2)
        self.assertEqual(response, {"ok": False, "error": "owner-already-adopted"})
        state = self.store.load()
        self.assertEqual(state["owner_id"], 111)
        self.assertEqual(state["queue"], [{"sample": "unanswered"}])
        self.assertEqual(state["candidates"], [])

    def test_invalid_bootstrap_id_cannot_claim_owner(self):
        for value in ("0", "-1", "not-a-user-id"):
            with self.subTest(value=value):
                code, response = self.adopt(value)
                self.assertEqual(code, 2)
                self.assertEqual(response, {"ok": False, "error": "invalid-owner-id"})
        self.assertIsNone(self.store.load().get("owner_id"))


if __name__ == "__main__":
    unittest.main()
