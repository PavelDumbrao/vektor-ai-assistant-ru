import hashlib
import os
import tracemalloc
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter
from plugins.platforms.telegram.local_file_ingress import (
    LocalTelegramPathError,
    copy_from_private_mount,
    tenant_relative_path,
)

SERVER_ROOT = "/var/lib/telegram-bot-api"
TENANT = "1234567890:opaque-tenant-directory-name"
TENANT_HASH = hashlib.sha256(TENANT.encode()).hexdigest()


def test_tenant_relative_path_accepts_only_matching_tenant():
    path = f"{SERVER_ROOT}/{TENANT}/videos/clip.mp4"
    assert tenant_relative_path(
        path, expected_tenant_sha256=TENANT_HASH, server_root=SERVER_ROOT
    ) == PurePosixPath("videos/clip.mp4")


def test_hash_mismatch_is_rejected():
    tenant_b = "9999999999:tenant-b"
    candidate = f"{SERVER_ROOT}/{tenant_b}/videos/clip.mp4"
    with pytest.raises(LocalTelegramPathError):
        tenant_relative_path(
            candidate,
            expected_tenant_sha256=TENANT_HASH,
            server_root=SERVER_ROOT,
        )


def test_non_server_path_is_rejected():
    with pytest.raises(LocalTelegramPathError):
        tenant_relative_path(
            "/unrelated/location/file.bin",
            expected_tenant_sha256=TENANT_HASH,
            server_root=SERVER_ROOT,
        )


def test_parent_component_is_rejected():
    candidate = f"{SERVER_ROOT}/{TENANT}/videos/../secret.bin"
    with pytest.raises(LocalTelegramPathError):
        tenant_relative_path(
            candidate,
            expected_tenant_sha256=TENANT_HASH,
            server_root=SERVER_ROOT,
        )


def _make_mount(tmp_path: Path, payload: bytes = b"video"):
    mount = tmp_path / "mount"
    (mount / "videos").mkdir(parents=True)
    source = mount / "videos" / "clip.mp4"
    source.write_bytes(payload)
    return mount, source


def test_private_mount_copy_rejects_symlink_file(tmp_path):
    mount, source = _make_mount(tmp_path)
    real = source.with_name("real.mp4")
    source.rename(real)
    source.symlink_to(real.name)
    destination = tmp_path / "staging.bin"
    destination.touch()
    with pytest.raises(OSError):
        copy_from_private_mount(
            mount,
            PurePosixPath("videos/clip.mp4"),
            destination,
            max_bytes=1024,
        )


def test_private_mount_copy_rejects_symlink_directory(tmp_path):
    mount, _source = _make_mount(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "clip.mp4").write_bytes(b"wrong")
    for child in (mount / "videos").iterdir():
        child.unlink()
    (mount / "videos").rmdir()
    (mount / "videos").symlink_to(outside, target_is_directory=True)
    destination = tmp_path / "staging.bin"
    destination.touch()
    with pytest.raises(OSError):
        copy_from_private_mount(
            mount,
            PurePosixPath("videos/clip.mp4"),
            destination,
            max_bytes=1024,
        )


def test_private_mount_copy_enforces_hard_size_limit(tmp_path):
    mount, _source = _make_mount(tmp_path, b"x" * 1024)
    destination = tmp_path / "staging.bin"
    destination.touch()
    with pytest.raises(ValueError):
        copy_from_private_mount(
            mount,
            PurePosixPath("videos/clip.mp4"),
            destination,
            max_bytes=512,
        )


def test_101mb_copy_keeps_python_heap_bounded(tmp_path):
    size = 101 * 1024 * 1024
    mount = tmp_path / "mount"
    (mount / "videos").mkdir(parents=True)
    source = mount / "videos" / "large.mp4"
    with source.open("wb") as fh:
        fh.truncate(size)
    destination = tmp_path / "staging.bin"
    destination.touch()

    tracemalloc.start()
    try:
        copied = copy_from_private_mount(
            mount,
            PurePosixPath("videos/large.mp4"),
            destination,
            max_bytes=200 * 1024 * 1024,
            expected_bytes=size,
        )
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert copied == size
    assert destination.stat().st_size == size
    assert peak < 8 * 1024 * 1024


def _adapter(*, local: bool) -> TelegramAdapter:
    extra = {}
    if local:
        extra = {
            "base_url": "http://127.0.0.1:8082/bot",
            "local_mode": True,
            "max_file_bytes": 1024 * 1024 * 1024,
        }
    config = PlatformConfig(enabled=True, extra=extra)
    config.token = "unit-test-identity"
    return TelegramAdapter(config)


@pytest.mark.asyncio
async def test_adapter_local_ingress_never_buffers_or_http_downloads(tmp_path, monkeypatch):
    payload = b"disk-backed-video"
    mount, _source_path = _make_mount(tmp_path, payload)
    monkeypatch.setenv("HERMES_TELEGRAM_LOCAL_MOUNT", str(mount))
    monkeypatch.setenv("HERMES_TELEGRAM_TENANT_SHA256", TENANT_HASH)
    monkeypatch.setenv("HERMES_TELEGRAM_LOCAL_SERVER_ROOT", SERVER_ROOT)
    monkeypatch.delenv("TERMINAL_ENV", raising=False)
    import gateway.platforms.base as base
    monkeypatch.setattr(base, "VIDEO_CACHE_DIR", tmp_path / "video-cache")
    file_obj = SimpleNamespace(
        file_size=len(payload),
        file_path=f"{SERVER_ROOT}/{TENANT}/videos/clip.mp4",
        download_to_drive=AsyncMock(side_effect=AssertionError("HTTP path must not run")),
        download_as_bytearray=AsyncMock(side_effect=AssertionError("RAM path must not run")),
    )
    source = SimpleNamespace(
        file_size=len(payload),
        get_file=AsyncMock(return_value=file_obj),
    )
    cached = await _adapter(local=True)._download_telegram_media_file(
        source,
        filename="clip.mp4",
        mime_type="video/mp4",
        default_kind="video",
    )
    cached_path = Path(cached.path)
    assert cached_path.read_bytes() == payload
    assert (cached_path.stat().st_mode & 0o777) == 0o600
    file_obj.download_to_drive.assert_not_awaited()
    file_obj.download_as_bytearray.assert_not_awaited()


@pytest.mark.asyncio
async def test_public_fallback_uses_drive_download_not_bytearray(tmp_path, monkeypatch):
    payload = b"public-video"
    monkeypatch.delenv("HERMES_TELEGRAM_LOCAL_MOUNT", raising=False)
    monkeypatch.delenv("HERMES_TELEGRAM_TENANT_SHA256", raising=False)
    monkeypatch.delenv("TERMINAL_ENV", raising=False)
    import gateway.platforms.base as base
    monkeypatch.setattr(base, "VIDEO_CACHE_DIR", tmp_path / "public-video-cache")

    async def _drive(custom_path=None, **_kwargs):
        path = Path(custom_path)
        path.write_bytes(payload)
        return path

    file_obj = SimpleNamespace(
        file_size=len(payload),
        file_path="videos/clip.mp4",
        download_to_drive=AsyncMock(side_effect=_drive),
        download_as_bytearray=AsyncMock(side_effect=AssertionError("RAM path must not run")),
    )
    source = SimpleNamespace(file_size=len(payload), get_file=AsyncMock(return_value=file_obj))
    cached = await _adapter(local=False)._download_telegram_media_file(
        source,
        filename="clip.mp4",
        mime_type="video/mp4",
        default_kind="video",
    )
    assert Path(cached.path).read_bytes() == payload
    file_obj.download_to_drive.assert_awaited_once()
    file_obj.download_as_bytearray.assert_not_awaited()



def test_private_relative_path_requires_explicit_private_mount_opt_in():
    relative = "voice/file_5.oga"
    with pytest.raises(LocalTelegramPathError, match="must be absolute"):
        tenant_relative_path(
            relative, expected_tenant_sha256=TENANT_HASH, server_root=SERVER_ROOT
        )
    assert tenant_relative_path(
        relative,
        expected_tenant_sha256=TENANT_HASH,
        server_root=SERVER_ROOT,
        allow_private_mount_relative=True,
    ) == PurePosixPath("voice/file_5.oga")


@pytest.mark.asyncio
async def test_adapter_local_ingress_accepts_private_relative_file_path(tmp_path, monkeypatch):
    payload = b"relative local document"
    mount = tmp_path / "mount"
    (mount / "documents").mkdir(parents=True)
    (mount / "documents" / "file_4.txt").write_bytes(payload)
    monkeypatch.setenv("HERMES_TELEGRAM_LOCAL_MOUNT", str(mount))
    monkeypatch.setenv("HERMES_TELEGRAM_TENANT_SHA256", TENANT_HASH)
    monkeypatch.setenv("HERMES_TELEGRAM_LOCAL_SERVER_ROOT", SERVER_ROOT)
    monkeypatch.delenv("TERMINAL_ENV", raising=False)
    import gateway.platforms.base as base
    monkeypatch.setattr(base, "DOCUMENT_CACHE_DIR", tmp_path / "doc-cache")
    file_obj = SimpleNamespace(
        file_size=len(payload), file_path="documents/file_4.txt",
        download_to_drive=AsyncMock(side_effect=AssertionError("HTTP path must not run")),
        download_as_bytearray=AsyncMock(side_effect=AssertionError("RAM path must not run")),
    )
    source = SimpleNamespace(file_size=len(payload), get_file=AsyncMock(return_value=file_obj))
    cached = await _adapter(local=True)._download_telegram_media_file(
        source, filename="notes.txt", mime_type="text/plain", default_kind="document"
    )
    assert Path(cached.path).read_bytes() == payload
    assert cached.media_type == "text/plain"
    file_obj.download_to_drive.assert_not_awaited()
    file_obj.download_as_bytearray.assert_not_awaited()

def test_staging_directory_ignores_host_tmpdir(tmp_path, monkeypatch):
    hermes_home = tmp_path / "profile" / ".hermes"
    hermes_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "blocked-tmp"))
    monkeypatch.delenv("HERMES_TELEGRAM_STAGING_DIR", raising=False)

    staging = _adapter(local=True)._telegram_staging_directory()
    assert staging == hermes_home / "cache" / "telegram-ingress"
    assert (staging.stat().st_mode & 0o777) == 0o700


def test_staging_directory_cannot_escape_hermes_home(tmp_path, monkeypatch):
    hermes_home = tmp_path / "profile" / ".hermes"
    hermes_home.mkdir(parents=True)
    outside = tmp_path / "outside"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_TELEGRAM_STAGING_DIR", str(outside))

    with pytest.raises(LocalTelegramPathError, match="must stay inside HERMES_HOME"):
        _adapter(local=True)._telegram_staging_directory()
