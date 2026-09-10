from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter
from plugins.platforms.telegram.local_file_ingress import (
    LocalTelegramPathError,
    strip_trusted_bot_file_url,
)


def _adapter():
    config = PlatformConfig(enabled=True, extra={
        "base_url": "http://127.0.0.1:8082/bot",
        "local_mode": True,
        "max_file_bytes": 1024 * 1024 * 1024,
    })
    config.token = "unit-test-identity"
    return TelegramAdapter(config)


def test_trusted_ptb_file_url_is_stripped():
    base = "http://127.0.0.1:8082/file/bot-demo"
    assert strip_trusted_bot_file_url(
        f"{base}/photos/file.jpg", trusted_base_file_url=base
    ) == "photos/file.jpg"


def test_untrusted_ptb_file_url_is_rejected():
    with pytest.raises(LocalTelegramPathError, match="not trusted"):
        strip_trusted_bot_file_url(
            "http://127.0.0.1:9999/file/bot-demo/photos/file.jpg",
            trusted_base_file_url="http://127.0.0.1:8082/file/bot-demo",
        )


def test_relative_path_passes_through_normalizer():
    assert strip_trusted_bot_file_url(
        "voice/file.oga",
        trusted_base_file_url="http://127.0.0.1:8082/file/bot-demo",
    ) == "voice/file.oga"


@pytest.mark.asyncio
async def test_adapter_strips_ptb_prefix_before_private_mount(tmp_path, monkeypatch):
    payload = b"ptb local media"
    mount = tmp_path / "mount"
    (mount / "documents").mkdir(parents=True)
    (mount / "documents" / "file.txt").write_bytes(payload)
    monkeypatch.setenv("HERMES_TELEGRAM_LOCAL_MOUNT", str(mount))
    monkeypatch.setenv("HERMES_TELEGRAM_TENANT_SHA256", "a" * 64)
    monkeypatch.setenv("HERMES_TELEGRAM_LOCAL_SERVER_ROOT", "/var/lib/telegram-bot-api")
    import gateway.platforms.base as base_module
    monkeypatch.setattr(base_module, "DOCUMENT_CACHE_DIR", tmp_path / "cache")
    trusted = "http://127.0.0.1:8082/file/bot-demo"
    no_http = AsyncMock(side_effect=AssertionError("HTTP download must not run"))
    file_obj = SimpleNamespace(
        file_size=len(payload),
        file_path=f"{trusted}/documents/file.txt",
        get_bot=lambda: SimpleNamespace(base_file_url=trusted),
        download_to_drive=no_http,
    )
    source = SimpleNamespace(
        file_size=len(payload),
        get_file=AsyncMock(return_value=file_obj),
    )
    cached = await _adapter()._download_telegram_media_file(
        source,
        filename="notes.txt",
        mime_type="text/plain",
        default_kind="document",
    )
    assert Path(cached.path).read_bytes() == payload
    no_http.assert_not_awaited()
