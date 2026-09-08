from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigurationError(RuntimeError):
    pass


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigurationError(f"Missing required environment variable: {name}")
    return value


def _int(name: str, default: int | None = None) -> int:
    raw = os.getenv(name)
    if raw is None and default is not None:
        return default
    try:
        return int(raw or "")
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false")


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_channel_id: int
    telegram_discussion_id: int
    telegram_channel_username: str
    telegram_bot_username: str
    pavel_user_id: int
    llm_base_url: str
    llm_api_key: str
    llm_comment_model: str
    state_db: Path
    editorial_policy: Path
    comment_mode: str
    bootstrap_skip_pending: bool
    max_reply_chars: int
    min_reply_confidence: float
    max_replies_per_hour: int
    max_replies_per_thread_hour: int
    max_replies_per_day: int
    user_cooldown_minutes: int
    max_comment_age_seconds: int
    log_level: str

    @classmethod
    def from_env(cls) -> "Settings":
        mode = os.getenv("COMMENT_MODE", "shadow").strip().lower()
        if mode not in {"shadow", "live", "off"}:
            raise ConfigurationError("COMMENT_MODE must be shadow, live, or off")

        channel_id = _int("TELEGRAM_CHANNEL_ID")
        discussion_id = _int("TELEGRAM_DISCUSSION_ID")
        if channel_id >= 0 or discussion_id >= 0:
            raise ConfigurationError("Telegram channel and discussion IDs must be negative")

        username = _required("TELEGRAM_BOT_USERNAME").lstrip("@")
        channel_username = os.getenv("TELEGRAM_CHANNEL_USERNAME", "ProAiCommunity").strip().lstrip("@")
        if not username or not channel_username:
            raise ConfigurationError("Telegram usernames must not be empty")

        settings = cls(
            telegram_bot_token=_required("TELEGRAM_BOT_TOKEN"),
            telegram_channel_id=channel_id,
            telegram_discussion_id=discussion_id,
            telegram_channel_username=channel_username,
            telegram_bot_username=username,
            pavel_user_id=_int("PAVEL_USER_ID"),
            llm_base_url=_required("LLM_BASE_URL").rstrip("/"),
            llm_api_key=_required("LLM_API_KEY"),
            llm_comment_model=os.getenv("LLM_COMMENT_MODEL", "gpt-5.4-mini").strip(),
            state_db=Path(_required("STATE_DB")),
            editorial_policy=Path(_required("EDITORIAL_POLICY")),
            comment_mode=mode,
            bootstrap_skip_pending=_bool("BOOTSTRAP_SKIP_PENDING", True),
            max_reply_chars=_int("MAX_REPLY_CHARS", 600),
            min_reply_confidence=_float("MIN_REPLY_CONFIDENCE", 0.88),
            max_replies_per_hour=_int("MAX_REPLIES_PER_HOUR", 10),
            max_replies_per_thread_hour=_int("MAX_REPLIES_PER_THREAD_HOUR", 2),
            max_replies_per_day=_int("MAX_REPLIES_PER_DAY", 40),
            user_cooldown_minutes=_int("USER_COOLDOWN_MINUTES", 60),
            max_comment_age_seconds=_int("MAX_COMMENT_AGE_SECONDS", 1800),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )
        if not 0.0 <= settings.min_reply_confidence <= 1.0:
            raise ConfigurationError("MIN_REPLY_CONFIDENCE must be between 0 and 1")
        if settings.max_reply_chars < 80 or settings.max_reply_chars > 1000:
            raise ConfigurationError("MAX_REPLY_CHARS must be between 80 and 1000")
        return settings

