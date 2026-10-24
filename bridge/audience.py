"""Audience resolution: the access gate, applied at the listener.

An event either resolves to exactly one configured audience or is dropped
silently. Bodies of dropped events are never retained by callers.
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import Audience, BridgeConfig

_IGNORED_SUBTYPES = {"message_changed", "message_deleted"}


@dataclass
class Decision:
    accepted: bool
    reason: str
    audience: Audience | None = None


def resolve_event(cfg: BridgeConfig, event: dict, bot_user_id: str) -> Decision:
    if event.get("subtype") in _IGNORED_SUBTYPES:
        return Decision(False, "bot-or-edit-event")
    if event.get("type") not in ("message", "app_mention"):
        return Decision(False, "unsupported-type")

    user = event.get("user")
    if not user or user == bot_user_id:
        return Decision(False, "no-user")
    channel = event.get("channel", "")
    audience = cfg.audience_for_sender(user, channel)
    if audience is None:
        return Decision(False, "no-audience")

    agents = audience.agent_channels.get(channel, set())
    if ((event.get("bot_id") or event.get("subtype") == "bot_message")
            and user not in agents):
        return Decision(False, "unapproved-bot")
    channel_type = event.get("channel_type") or (
        "im" if channel.startswith("D") else "channel"
    )
    if channel_type == "mpim":
        return Decision(False, "group-dm-disabled")
    if channel_type == "im":
        if user not in audience.user_ids:
            return Decision(False, "agent-dm-disabled")
        if not audience.dm_enabled:
            return Decision(False, "dm-disabled")
        return Decision(True, "dm", audience)

    if channel not in audience.channel_allowlist:
        return Decision(False, "channel-not-allowlisted")
    if (
        audience.mentions_only_in_channels and not agents
        and event.get("type") == "message"
        and f"<@{bot_user_id}>" not in event.get("text", "")
    ):
        return Decision(False, "no-mention")
    return Decision(True, "channel", audience)
