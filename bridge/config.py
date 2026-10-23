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


@dataclass
class BridgeConfig:
    deployment: str
    ack_reaction: str
    audiences: list[Audience]

    def audience_for_user(self, user_id: str) -> Audience | None:
        for audience in self.audiences:
            if user_id in audience.user_ids:
                return audience
        return None


def _check_no_secrets(node, path: str = "") -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            lowered = str(key).lower()
            if any(part in lowered for part in _FORBIDDEN_KEY_PARTS):
                raise ValueError(f"credential-like key not allowed in config: {path}{key}")
            _check_no_secrets(value, f"{path}{key}.")


def _valid_scope(scope: str, audience_id: str) -> bool:
    return scope in ("owner", "public") or scope == f"shared:{audience_id}"


def load_config(path: str | Path) -> BridgeConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("config root must be a mapping")
    _check_no_secrets(raw)
    audiences: list[Audience] = []
    for entry in raw.get("audiences", []):
        audience = Audience(
            id=str(entry["id"]),
            scope=str(entry["scope"]),
            user_ids={str(u) for u in entry.get("userIds", [])},
            channel_allowlist={str(c) for c in entry.get("channelAllowlist", [])},
            dm_enabled=bool(entry.get("dmEnabled", False)),
            mentions_only_in_channels=bool(entry.get("mentionsOnlyInChannels", True)),
        )
        if not _valid_scope(audience.scope, audience.id):
            raise ValueError(f"invalid scope for audience {audience.id}: {audience.scope}")
        audiences.append(audience)
    if not audiences:
        raise ValueError("config must define at least one audience")
    return BridgeConfig(
        deployment=str(raw.get("deployment", "unspecified")),
        ack_reaction=str(raw.get("ackReaction", "eyes")),
        audiences=audiences,
    )
