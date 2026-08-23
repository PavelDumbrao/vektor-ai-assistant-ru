"""Strict, secret-free settings for the passive secretary plugin."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,127}$")
TELEGRAM_ID_MAX = 2**63 - 1


@dataclass(frozen=True)
class Settings:
    tenant_id: str
    source_id: str
    test_run_id: str
    owner_telegram_user_ids: tuple[str, ...]
    postgres_dsn_env: str
    source_ref_key_env: str
    retention_days: int
    capture_enabled: bool = False
    timezone: str = "Europe/Moscow"
    auto_context_enabled: bool = False
    outbound_replies_enabled: bool = False
    outbound_reply_max_chars: int = 2_000
    outbound_intent_ttl_seconds: int = 300
    auto_context_hours: int = 24
    auto_context_max_messages: int = 40
    auto_context_max_chars: int = 10_000
    per_message_max_chars: int = 600
    worker_batch_size: int = 25
    worker_poll_seconds: float = 1.0
    retry_max_seconds: int = 300
    query_timeout_seconds: int = 3
    session_authorization_ttl_seconds: int = 3_600

    def __post_init__(self) -> None:
        for label, value in (("tenant_id", self.tenant_id), ("source_id", self.source_id)):
            if not ID_RE.fullmatch(value):
                raise ValueError(f"{label} has an invalid format")
        if self.test_run_id and not ID_RE.fullmatch(self.test_run_id):
            raise ValueError("test_run_id has an invalid format")
        if not self.owner_telegram_user_ids:
            raise ValueError("owner_telegram_user_ids must not be empty")
        if any(
            not value.isdigit() or not 0 < int(value) <= TELEGRAM_ID_MAX
            for value in self.owner_telegram_user_ids
        ):
            raise ValueError("owner_telegram_user_ids must contain positive numeric ids")
        if len(set(self.owner_telegram_user_ids)) != len(self.owner_telegram_user_ids):
            raise ValueError("owner_telegram_user_ids contains duplicates")
        for label, value in (
            ("postgres_dsn_env", self.postgres_dsn_env),
            ("source_ref_key_env", self.source_ref_key_env),
        ):
            if not ENV_RE.fullmatch(value):
                raise ValueError(f"{label} must be an environment-variable name")
        if not 1 <= self.retention_days <= 3650:
            raise ValueError("retention_days must be between 1 and 3650")
        if self.timezone != "Europe/Moscow":
            raise ValueError("timezone must be Europe/Moscow for this deployment profile")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone: {self.timezone}") from exc
        if not 1 <= self.auto_context_hours <= 168:
            raise ValueError("auto_context_hours must be between 1 and 168")
        if not 1 <= self.outbound_reply_max_chars <= 4_000:
            raise ValueError("outbound_reply_max_chars must be between 1 and 4000")
        if not 60 <= self.outbound_intent_ttl_seconds <= 900:
            raise ValueError("outbound_intent_ttl_seconds must be between 60 and 900")
        if not 1 <= self.auto_context_max_messages <= 200:
            raise ValueError("auto_context_max_messages must be between 1 and 200")
        if not 1_000 <= self.auto_context_max_chars <= 40_000:
            raise ValueError("auto_context_max_chars must be between 1000 and 40000")
        if not 100 <= self.per_message_max_chars <= 4_000:
            raise ValueError("per_message_max_chars must be between 100 and 4000")
        if not 1 <= self.worker_batch_size <= 200:
            raise ValueError("worker_batch_size must be between 1 and 200")
        if not 0.2 <= self.worker_poll_seconds <= 60:
            raise ValueError("worker_poll_seconds must be between 0.2 and 60")
        if not 5 <= self.retry_max_seconds <= 3600:
            raise ValueError("retry_max_seconds must be between 5 and 3600")
        if not 1 <= self.query_timeout_seconds <= 30:
            raise ValueError("query_timeout_seconds must be between 1 and 30")
        if not 60 <= self.session_authorization_ttl_seconds <= 86_400:
            raise ValueError("session_authorization_ttl_seconds must be between 60 and 86400")

    @property
    def owner_ids(self) -> frozenset[str]:
        return frozenset(self.owner_telegram_user_ids)

    def postgres_configured(self) -> bool:
        return bool(os.environ.get(self.postgres_dsn_env, "").strip())

    def source_ref_key(self) -> bytes:
        value = os.environ.get(self.source_ref_key_env, "")
        if len(value) < 32:
            raise RuntimeError(
                f"{self.source_ref_key_env} must contain at least 32 characters"
            )
        return value.encode("utf-8", "strict")


def _strict_bool(raw: Any, label: str) -> bool:
    if not isinstance(raw, bool):
        raise ValueError(f"{label} must be a boolean")
    return raw


def load_settings(path: str | Path) -> Settings:
    settings_path = Path(path)
    if settings_path.is_symlink() or not settings_path.is_file():
        raise RuntimeError("Passive secretary settings are missing or unsafe")
    raw = json.loads(settings_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Passive secretary settings must be a JSON object")
    owners = raw.get("owner_telegram_user_ids")
    if not isinstance(owners, list):
        raise ValueError("owner_telegram_user_ids must be a list")
    capture_enabled = _strict_bool(raw.get("capture_enabled", False), "capture_enabled")
    auto_context_enabled = _strict_bool(
        raw.get("auto_context_enabled", False), "auto_context_enabled"
    )
    outbound_replies_enabled = _strict_bool(
        raw.get("outbound_replies_enabled", False), "outbound_replies_enabled"
    )
    return Settings(
        tenant_id=str(raw.get("tenant_id") or ""),
        source_id=str(raw.get("source_id") or ""),
        test_run_id=str(raw.get("test_run_id") or ""),
        owner_telegram_user_ids=tuple(str(item) for item in owners),
        postgres_dsn_env=str(raw.get("postgres_dsn_env") or ""),
        source_ref_key_env=str(raw.get("source_ref_key_env") or ""),
        retention_days=int(raw["retention_days"]),
        capture_enabled=capture_enabled,
        timezone=str(raw.get("timezone") or "Europe/Moscow"),
        auto_context_enabled=auto_context_enabled,
        outbound_replies_enabled=outbound_replies_enabled,
        outbound_reply_max_chars=int(raw.get("outbound_reply_max_chars", 2_000)),
        outbound_intent_ttl_seconds=int(raw.get("outbound_intent_ttl_seconds", 300)),
        auto_context_hours=int(raw.get("auto_context_hours", 24)),
        auto_context_max_messages=int(raw.get("auto_context_max_messages", 40)),
        auto_context_max_chars=int(raw.get("auto_context_max_chars", 10_000)),
        per_message_max_chars=int(raw.get("per_message_max_chars", 600)),
        worker_batch_size=int(raw.get("worker_batch_size", 25)),
        worker_poll_seconds=float(raw.get("worker_poll_seconds", 1.0)),
        retry_max_seconds=int(raw.get("retry_max_seconds", 300)),
        query_timeout_seconds=int(raw.get("query_timeout_seconds", 3)),
        session_authorization_ttl_seconds=int(
            raw.get("session_authorization_ttl_seconds", 3_600)
        ),
    )
