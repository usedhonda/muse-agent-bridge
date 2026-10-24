"""Offline config credential-key checks, including mappings inside lists."""
import tempfile
import unittest
from pathlib import Path

import yaml

from bridge.config import load_config


class ConfigSecretListTests(unittest.TestCase):
    def config(self):
        return {"deployment": "test", "audiences": [
            {"id": "owner", "scope": "owner", "userIds": ["U_OWNER"],
             "channelAllowlist": ["C_PRIVATE"]}
        ]}

    def load(self, raw):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(yaml.safe_dump(raw), encoding="utf-8")
            return load_config(path)

    def assert_rejected(self, raw, expected_path):
        with self.assertRaises(ValueError) as caught:
            self.load(raw)
        self.assertEqual(str(caught.exception),
                         "credential-like key not allowed in config: " + expected_path)
        self.assertNotIn("dummy-credential-value", str(caught.exception))

    def test_credential_keys_inside_audience_list_are_rejected(self):
        for key in ("apiToken", "clientSECRET", "Password"):
            with self.subTest(key=key):
                raw = self.config()
                raw["audiences"][0][key] = "dummy-credential-value"
                self.assert_rejected(raw, "audiences.0." + key)

    def test_list_inside_audience_mapping_is_checked(self):
        raw = self.config()
        raw["audiences"][0]["options"] = [None, {"apiToken": "dummy-credential-value"}]
        self.assert_rejected(raw, "audiences.0.options.1.apiToken")

    def test_nested_lists_are_checked(self):
        raw = self.config()
        raw["extras"] = [[], [None, [{"password": "dummy-credential-value"}]]]
        self.assert_rejected(raw, "extras.1.1.0.password")

    def test_existing_mapping_checks_and_paths_are_preserved(self):
        for key in ("TOKEN", "clientSecret", "db_password"):
            with self.subTest(key=key):
                raw = self.config()
                raw["options"] = {key: "dummy-credential-value"}
                self.assert_rejected(raw, "options." + key)

    def test_top_level_checks_are_preserved(self):
        raw = self.config()
        raw["token"] = "dummy-credential-value"
        self.assert_rejected(raw, "token")

    def test_safe_lists_and_scalar_values_remain_allowed(self):
        raw = self.config()
        raw["extras"] = [None, True, 42, "password", [], [{"label": "secret"}]]
        config = self.load(raw)
        self.assertEqual(config.deployment, "test")
        self.assertEqual(config.audiences[0].user_ids, {"U_OWNER"})
        self.assertEqual(config.audiences[0].channel_allowlist, {"C_PRIVATE"})


if __name__ == "__main__":
    unittest.main()
