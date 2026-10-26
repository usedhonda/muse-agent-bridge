"""Slack Socket Mode transport.

Opens an `apps.connections.open` URL with the app-level token and speaks
the Socket Mode envelope protocol over WSS. Only outbound connections are
made, which is what the agent VM allows.

When HTTPS_PROXY/https_proxy is set (the Muse runtime egress), the WSS
connection is made through that HTTP CONNECT proxy; Slack's WSS host is
not necessarily reachable directly from the VM.
"""
from __future__ import annotations

import json
import os
import urllib.request
from urllib.parse import urlparse


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


def _socket_call(call, *args, **kwargs):
    import websocket
    try:
        return call(*args, **kwargs)
    except websocket.WebSocketException:
        # Exception details can contain transport URLs/headers; never echo them.
        raise ConnectionError("Socket Mode connection interrupted") from None


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
        options: dict = {}
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        if proxy:
            parsed = urlparse(proxy)
            if parsed.hostname:
                options["http_proxy_host"] = parsed.hostname
            if parsed.port:
                options["http_proxy_port"] = parsed.port
            if parsed.username:
                options["http_proxy_auth"] = (parsed.username, parsed.password or "")
            options["proxy_type"] = "http"
        self._ws = _socket_call(websocket.create_connection,
            open_connection_url(self._app_token), **options
        )

    def receive(self) -> dict:
        raw = _socket_call(self._ws.recv)
        if not raw:
            raise ConnectionError("Socket Mode connection closed")
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            raise ConnectionError("invalid Socket Mode envelope") from None

    def send_ack(self, envelope_id: str) -> None:
        _socket_call(self._ws.send, json.dumps({"envelope_id": envelope_id}))

    def close(self) -> None:
        if self._ws is not None:
            try:
                _socket_call(self._ws.close)
            finally:
                self._ws = None
