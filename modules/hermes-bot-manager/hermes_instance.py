#!/usr/bin/env python3
"""Versioned desired-state contract for one Hermes Forge instance."""
from __future__ import annotations

import json
import re
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
OWNER_RE = re.compile(r"^h[1-9][0-9]{4,18}$")
BOT_USERNAME_RE = re.compile(r"^[a-z0-9_]{5,32}$")
RELEASE_RE = re.compile(r"^hermes-[A-Za-z0-9_.-]{1,80}$")
PACKAGE_RE = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
UPDATE_CHANNELS = frozenset({"canary", "preview", "stable", "pinned"})
DESIRED_STATES = frozenset({"running", "paused"})


def owner_for_telegram_id(user_id: int) -> str:
    value = int(user_id)
    if value <= 0:
        raise ValueError("owner_telegram_id_invalid")
    owner = f"h{value}"
    if not OWNER_RE.fullmatch(owner):
        raise ValueError("derived_owner_invalid")
    return owner


def normalize_bot_username(value: str) -> str:
    username = str(value or "").strip().lstrip("@").lower()
    if not BOT_USERNAME_RE.fullmatch(username) or not username.endswith("bot"):
        raise ValueError("bot_username_invalid")
    return username


def _positive_int(value: Any, label: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}_invalid") from exc
    if result <= 0:
        raise ValueError(f"{label}_invalid")
    return result


@dataclass(frozen=True)
class HermesInstance:
    instance_id: str
    owner_linux: str
    owner_telegram_id: int
    bot_id: int
    bot_username: str
    bot_name: str
    release_id: str
    package_id: str = "personal-hermes"
    package_version: str = "1.0.0"
    desired_state: str = "running"
    update_channel: str = "stable"

    def validate(self) -> "HermesInstance":
        expected_owner = owner_for_telegram_id(self.owner_telegram_id)
        if self.owner_linux != expected_owner:
            raise ValueError("owner_linux_mismatch")
        if self.instance_id != f"hermes-{self.owner_telegram_id}":
            raise ValueError("instance_id_mismatch")
        _positive_int(self.bot_id, "bot_id")
        if normalize_bot_username(self.bot_username) != self.bot_username:
            raise ValueError("bot_username_not_canonical")
        if not self.bot_name or len(self.bot_name) > 64:
            raise ValueError("bot_name_invalid")
        if not RELEASE_RE.fullmatch(self.release_id):
            raise ValueError("release_id_invalid")
        if not PACKAGE_RE.fullmatch(self.package_id):
            raise ValueError("package_id_invalid")
        if not re.fullmatch(r"[0-9A-Za-z_.+-]{1,40}", self.package_version):
            raise ValueError("package_version_invalid")
        if self.desired_state not in DESIRED_STATES:
            raise ValueError("desired_state_invalid")
        if self.update_channel not in UPDATE_CHANNELS:
            raise ValueError("update_channel_invalid")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION, **asdict(self)}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "HermesInstance":
        if int(payload.get("schema_version", 0)) != SCHEMA_VERSION:
            raise ValueError("schema_version_unsupported")
        fields = {
            key: payload[key]
            for key in cls.__dataclass_fields__
            if key in payload
        }
        fields["owner_telegram_id"] = _positive_int(
            fields.get("owner_telegram_id"), "owner_telegram_id"
        )
        fields["bot_id"] = _positive_int(fields.get("bot_id"), "bot_id")
        return cls(**fields).validate()

    def write_atomic(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        raw = json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o600)
            temporary.replace(path)
        finally:
            if temporary.exists():
                temporary.unlink()
