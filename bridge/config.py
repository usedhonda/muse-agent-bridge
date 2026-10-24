"""Bridge configuration: local YAML only, no secrets.

Config lives in `.local/config.yaml` (gitignored). Tokens are never read
from config; the loader rejects any key that looks like a credential so a
token can never be committed by accident through a copied config file.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

_FORBIDDEN_KEY_PARTS = ("token", "secret", "password")


@dataclass
class Audience:
    id: str
    scope: str  # "owner" | "public" | "shared:<audience-id>"
    user_ids: set[str]
    channel_allowlist: set[str] = field(default_factory=set)
    dm_enabled: bool = False
    mentions_only_in_channels: bool = True
    # Explicit private-room exceptions; agent IDs never become DM principals.
    agent_channels: dict[str, set[str]] = field(default_factory=dict)


@dataclass
class BridgeConfig:
    deployment: str
    ack_reaction: str | None
    audiences: list[Audience]
    # Owner preference (2026-10-24): replies go to the channel top level,
    # not into threads. Deployments opt out with replyInThreads: false;
    # the default preserves the original threaded behavior.
    reply_in_threads: bool = True

    def audience_for_user(self, user_id: str) -> Audience | None:
        for audience in self.audiences:
            if user_id in audience.user_ids:
                return audience
        return None

    def audience_for_sender(self, user_id: str, channel: str) -> Audience | None:
        audience = self.audience_for_user(user_id)
        if audience is not None:
            return audience
        return next((a for a in self.audiences
                     if user_id in a.agent_channels.get(channel, set())), None)


def _check_no_secrets(node, path: str = "") -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            lowered = str(key).lower()
            if any(part in lowered for part in _FORBIDDEN_KEY_PARTS):
                raise ValueError(f"credential-like key not allowed in config: {path}{key}")
            _check_no_secrets(value, f"{path}{key}.")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _check_no_secrets(value, f"{path}{index}.")


def _valid_scope(scope: str, audience_id: str) -> bool:
    return scope in ("owner", "public") or scope == f"shared:{audience_id}"


def load_config(path: str | Path) -> BridgeConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("config root must be a mapping")
    _check_no_secrets(raw)
    audiences: list[Audience] = []
    for entry in raw.get("audiences", []):
        rooms = entry.get("agentChannels", {})
        if not isinstance(rooms, dict) or any(
            not isinstance(users, list) or any(not isinstance(u, str) or not u.strip() for u in users)
            for users in rooms.values()
        ):
            raise ValueError("agentChannels must map channel IDs to agent ID lists")
        audience = Audience(
            id=str(entry["id"]),
            scope=str(entry["scope"]),
            user_ids={str(u) for u in entry.get("userIds", [])},
            channel_allowlist={str(c) for c in entry.get("channelAllowlist", [])},
            dm_enabled=bool(entry.get("dmEnabled", False)),
            mentions_only_in_channels=bool(entry.get("mentionsOnlyInChannels", True)),
            agent_channels={str(c): {str(u) for u in users}
                            for c, users in rooms.items()},
        )
        if audience.agent_channels and (
            audience.id != "owner" or audience.scope != "owner"
            or len(audience.user_ids) != 1
            or not set(audience.agent_channels) <= audience.channel_allowlist
            or any(not agents or agents & audience.user_ids
                   or not channel.startswith(("C", "G"))
                   for channel, agents in audience.agent_channels.items())
        ):
            raise ValueError("agentChannels requires owner-only allowlisted private rooms")
        if not _valid_scope(audience.scope, audience.id):
            raise ValueError(f"invalid scope for audience {audience.id}: {audience.scope}")
        audiences.append(audience)
    if not audiences:
        raise ValueError("config must define at least one audience")
    return BridgeConfig(
        deployment=str(raw.get("deployment", "unspecified")),
        ack_reaction=raw.get("ackReaction", "eyes"),
        audiences=audiences,
        reply_in_threads=bool(raw.get("replyInThreads", True)),
    )

