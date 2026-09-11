import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter
from plugins.platforms.telegram.passive_media import _canonical_digest

SERVER_ROOT = "/var/lib/telegram-bot-api"
TENANT = "1234567890:tenant-private"
TENANT_HASH = hashlib.sha256(TENANT.encode()).hexdigest()


class DummySpool:
    def __init__(self, root: Path):
        self.root = root
        self.results = []
        self.acked = []

    def create_private_temp(self, job_id):
        path = self.root / f"{job_id}.tmp"
        path.touch(mode=0o600)
        return path

    def put_result(self, result):
        self.results.append(result)

    def acknowledge_job(self, job):
        self.acked.append(job["job_id"])

    def discard_private_temp(self, path):
        path.unlink(missing_ok=True)


class DummyASR:
    async def transcribe(self, path, *, media_kind):
        data = Path(path).read_bytes()
        return SimpleNamespace(
            transcript="voice ok",
            language="ru",
            content_sha256=hashlib.sha256(data).hexdigest(),
            actual_bytes=len(data),
            duration_ms=1000,
        )


def make_job(processor_version, size):
    job = {
        "job_version": 1,
        "job_id": "a" * 64,
        "bot_id": "1234567890",
        "tenant_owner_id": 42,
        "source_update_id": 7,
        "business_connection_id": "group-passive-v1",
        "chat_id": -1001,
        "message_id": 9,
        "media_index": 0,
        "media_kind": "voice",
        "file_id": "opaque-file-id",
        "file_unique_id": "unique-voice",
        "declared_file_size": size,
        "declared_duration": 1,
        "declared_mime_type": "audio/ogg",
        "declared_file_name": None,
        "processor_version": processor_version,
        "preflight_status": None,
        "enqueued_at_utc": "2026-09-11T17:00:00Z",
    }
    job["job_sha256"] = _canonical_digest(job, omit="job_sha256")
    return job


@pytest.mark.asyncio
async def test_passive_voice_uses_private_local_mount(tmp_path, monkeypatch):
    payload = b"passive voice bytes"
    mount = tmp_path / "mount"
    (mount / "voice").mkdir(parents=True)
    (mount / "voice" / "file_5.oga").write_bytes(payload)
    monkeypatch.setenv("HERMES_TELEGRAM_LOCAL_MOUNT", str(mount))
    monkeypatch.setenv("HERMES_TELEGRAM_TENANT_SHA256", TENANT_HASH)
    monkeypatch.setenv("HERMES_TELEGRAM_LOCAL_SERVER_ROOT", SERVER_ROOT)

    config = PlatformConfig(enabled=True, extra={"local_mode": True})
    config.token = "1234567890:test"
    adapter = TelegramAdapter(config)
    spool = DummySpool(tmp_path)
    processor_version = adapter._passive_media_processor_version()
    job = make_job(processor_version, len(payload))

    file_obj = SimpleNamespace(
        file_size=len(payload),
        file_path="voice/file_5.oga",
        get_bot=lambda: SimpleNamespace(base_file_url="http://127.0.0.1:8082/file/bot"),
        download_to_drive=AsyncMock(side_effect=AssertionError("HTTP path must not run")),
    )
    adapter._bot = SimpleNamespace(get_file=AsyncMock(return_value=file_obj))
    monkeypatch.setattr(adapter, "_get_passive_media_spool", lambda: spool)
    monkeypatch.setattr(adapter, "_get_passive_media_asr", lambda: DummyASR())

    ok = await adapter._process_passive_media_job(job)

    assert ok is True
    assert spool.acked == [job["job_id"]]
    assert spool.results[0]["payload"]["status"] == "transcribed"
    assert spool.results[0]["payload"]["transcript"] == "voice ok"
    file_obj.download_to_drive.assert_not_awaited()
