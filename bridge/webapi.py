"""Minimal Slack Web API client (bot token, stdlib HTTP)."""
from __future__ import annotations

import json
import urllib.request
import urllib.parse

_API_BASE = "https://slack.com/api/"


class SlackWebClient:
    def __init__(self, bot_token: str):
        if bot_token.startswith("xoxp-"):
            raise ValueError("user tokens cannot be used for bridge replies")
        self._bot_token = bot_token
        self._bot_user_id = None

    def _post(self, method: str, params: dict) -> dict:
        request = urllib.request.Request(
            _API_BASE + method,
            data=json.dumps(params).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._bot_token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))

    def auth_test(self) -> str:
        result = self._post("auth.test", {})
        if not result.get("ok") or not result.get("bot_id") or not result.get("user_id"):
            raise RuntimeError("auth.test did not verify a bot identity")
        self._bot_user_id = result["user_id"]
        return self._bot_user_id

    def _get(self, method: str, params: dict) -> dict:
        request = urllib.request.Request(
            _API_BASE + method + "?" + urllib.parse.urlencode(params),
            headers={"Authorization": f"Bearer {self._bot_token}"},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))

    def private_room_matches(self, channel: str, expected: set[str]) -> bool:
        info = self._get("conversations.info", {"channel": channel})
        room = info.get("channel", {})
        if (not info.get("ok") or not room.get("is_private")
                or room.get("is_im") or room.get("is_mpim") or room.get("is_archived")
                or room.get("is_shared") or room.get("is_ext_shared")
                or room.get("is_org_shared") or room.get("pending_shared")):
            return False
        members = self._get("conversations.members", {"channel": channel, "limit": 200})
        return (members.get("ok") is True
                and not members.get("response_metadata", {}).get("next_cursor")
                and set(members.get("members", [])) == expected)

    def has_thread(self, channel: str, timestamp: str) -> bool:
        result = self._get("conversations.replies", {"channel": channel, "ts": timestamp, "limit": 1})
        if not result.get("ok") or not result.get("messages"):
            raise RuntimeError("could not verify existing thread")
        return bool(result["messages"][0].get("reply_count", 0))

    def post_message(self, channel: str, thread_ts: str | None, text: str) -> str:
        if self._bot_user_id is None:
            self.auth_test()  # fail closed for user/rotated/surrogate credentials
        params = {
            "channel": channel, "text": text,
            "reply_broadcast": False, "unfurl_links": False, "unfurl_media": False,
        }
        if thread_ts is not None:
            params["thread_ts"] = thread_ts
        result = self._post("chat.postMessage", params)
        if not result.get("ok"):
            raise RuntimeError("chat.postMessage failed")
        return result["ts"]

    def reactions_add(self, channel: str, timestamp: str, name: str) -> None:
        result = self._post(
            "reactions.add",
            {"channel": channel, "timestamp": timestamp, "name": name},
        )
        # already_reacted is fine; anything else is worth surfacing in logs.
        if not result.get("ok") and result.get("error") != "already_reacted":
            raise RuntimeError(f"reactions.add failed: {result.get('error')}")
