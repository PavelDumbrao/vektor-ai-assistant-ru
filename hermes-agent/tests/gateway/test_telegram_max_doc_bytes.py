"""Tests for Telegram document-size cap.

The public Telegram Bot API path remains capped at 20MB. Large-file ingress
is enabled only when a custom Bot API is paired with PTB local_mode so files
are copied from disk instead of buffered into gateway RAM.
"""

import sys
from unittest.mock import MagicMock

from gateway.config import PlatformConfig


def _ensure_telegram_mock():
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return

    telegram_mod = MagicMock()
    telegram_mod.ext.ContextTypes.DEFAULT_TYPE = type(None)
    telegram_mod.constants.ParseMode.MARKDOWN_V2 = "MarkdownV2"
    telegram_mod.constants.ChatType.GROUP = "group"
    telegram_mod.constants.ChatType.SUPERGROUP = "supergroup"
    telegram_mod.constants.ChatType.CHANNEL = "channel"
    telegram_mod.constants.ChatType.PRIVATE = "private"

    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, telegram_mod)


_ensure_telegram_mock()

from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402


def _adapter(extra):
    cfg = PlatformConfig(enabled=True, extra=extra)
    cfg.token = "test-credential-placeholder"
    return TelegramAdapter(cfg)


def test_custom_http_base_without_local_mode_stays_at_20mb():
    assert _adapter({"base_url": "http://localhost:8081/bot"})._max_doc_bytes == 20 * 1024 * 1024


def test_local_mode_defaults_to_1gb():
    adapter = _adapter({"base_url": "http://localhost:8081/bot", "local_mode": True})
    assert adapter._max_doc_bytes == 1024 * 1024 * 1024


def test_local_mode_explicit_limit_is_clamped_at_2gb():
    adapter = _adapter({
        "base_url": "http://localhost:8081/bot",
        "local_mode": True,
        "max_file_bytes": 3 * 1024 * 1024 * 1024,
    })
    assert adapter._max_doc_bytes == 2 * 1024 * 1024 * 1024
