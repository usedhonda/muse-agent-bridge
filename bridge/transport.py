"""Slack Socket Mode transport.

Opens an `apps.connections.open` URL with the app-level token and speaks
the Socket Mode envelope protocol over WSS. Only outbound connections are
made, which is what the agent VM allows.
"""
from __future__ import annotations

import json
import urllib.request


def open_connection_url(app_token: str) -> str:
    request = urllib.request.Request(
        "https://slack.com/api/apps.connections.open",
        data=b"",
        headers={"Authorization": f"Bearer {app_token}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError(f"apps.connections.open failed: {result.get('error')}")
    return result["url"]


class SocketModeTransport:
    def __init__(self, app_token: str):
        self._app_token = app_token
        self._ws = None

    def connect(self) -> None:
        try:
            import websocket
        except ImportError as exc:  # pragma: no cover - environment issue
            raise RuntimeError(
                "websocket-client is required; see requirements.txt"
            ) from exc
        self._ws = websocket.create_connection(open_connection_url(self._app_token))

    def receive(self) -> dict:
        raw = self._ws.recv()
        return json.loads(raw)

    def send_ack(self, envelope_id: str) -> None:
        self._ws.send(json.dumps({"envelope_id": envelope_id}))

    def close(self) -> None:
        if self._ws is not None:
            self._ws.close()
            self._ws = None
