"""Minimal Slack Web API client (bot token, stdlib HTTP)."""
from __future__ import annotations

import json
import urllib.request

_API_BASE = "https://slack.com/api/"


class SlackWebClient:
    def __init__(self, bot_token: str):
        self._bot_token = bot_token

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
        if not result.get("ok"):
            raise RuntimeError(f"auth.test failed: {result.get('error')}")
        return result["user_id"]

    def post_message(self, channel: str, thread_ts: str, text: str) -> str:
        result = self._post("chat.postMessage", {
            "channel": channel, "thread_ts": thread_ts, "text": text,
            "reply_broadcast": False, "unfurl_links": False, "unfurl_media": False,
        })
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
