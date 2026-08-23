"""Durable, bounded media enrichment for passive Telegram Business updates.

The private job spool is the only boundary that retains Telegram ``file_id``
values.  Result envelopes are capability-free: they never contain a bot
client, token, download identifier, URL, or local filesystem path.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import math
import os
import stat
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from plugins.platforms.telegram.passive_updates import (
    SCHEMA_VERSION,
    _ensure_durable_private_directory,
    _fsync_directory,
)


logger = logging.getLogger(__name__)


MEDIA_JOB_VERSION = 1
DEFAULT_PROCESSOR_VERSION = "whisper-asr-webservice:1.9.1/small-int8"
OPENROUTER_WHISPER_TURBO_MODEL = "openai/whisper-large-v3-turbo"
OPENROUTER_PROCESSOR_VERSION = "openrouter:openai/whisper-large-v3-turbo:v1"
OPENROUTER_STT_ENDPOINT = "https://openrouter.ai/api/v1/audio/transcriptions"
DEFAULT_RELATIVE_MEDIA_SPOOL = Path("passive-secretary") / "media"
MAX_MEDIA_FILE_BYTES = 20 * 1024 * 1024
MAX_MEDIA_DURATION_SECONDS = 1_800
MAX_TRANSCRIPT_CHARS = 75_000
MAX_ASR_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_MEDIA_SPOOL_ITEM_BYTES = 512_000
MAX_PENDING_MEDIA_ITEMS = 1_000
MEDIA_RESULT_KIND = "business_media_result"

_MEDIA_KINDS = frozenset({"voice", "video_note"})
_RESULT_STATUSES = frozenset(
    {
        "transcribed",
        "unsupported",
        "too_large",
        "download_failed_permanent",
        "asr_failed_permanent",
    }
)
_JOB_KEYS = frozenset(
    {
        "job_version",
        "job_id",
        "bot_id",
        "tenant_owner_id",
        "source_update_id",
        "business_connection_id",
        "chat_id",
        "message_id",
        "media_index",
        "media_kind",
        "file_id",
        "file_unique_id",
        "declared_file_size",
        "declared_duration",
        "declared_mime_type",
        "declared_file_name",
        "processor_version",
        "preflight_status",
        "enqueued_at_utc",
        "job_sha256",
    }
)
_RESULT_PAYLOAD_KEYS = frozenset(
    {
        "job_id",
        "business_connection_id",
        "chat_id",
        "message_id",
        "media_index",
        "media_kind",
        "file_unique_id",
        "status",
        "transcript",
        "language",
        "content_sha256",
        "actual_bytes",
        "duration_ms",
        "processor",
    }
)
_RESULT_EVENT_KEYS = frozenset(
    {
        "schema_version",
        "transport",
        "tenant_owner_id",
        "update_id",
        "kind",
        "payload",
        "payload_sha256",
        "received_at_utc",
    }
)
_PROCESSOR_KEYS = frozenset(
    {"service", "version", "engine", "model", "quantization"}
)


def _utc_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_digest(value: Mapping[str, Any], *, omit: str) -> str:
    payload = {key: item for key, item in value.items() if key != omit}
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _core_event_digest(event: Mapping[str, Any]) -> str:
    canonical = {
        "schema_version": event.get("schema_version"),
        "transport": event.get("transport"),
        "tenant_owner_id": event.get("tenant_owner_id"),
        "update_id": event.get("update_id"),
        "kind": event.get("kind"),
        "payload": event.get("payload"),
    }
    return hashlib.sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _bounded_string(value: Any, *, maximum: int, allow_empty: bool = False) -> str | None:
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
        return None
    if not allow_empty and not value:
        return None
    return value


def _positive_int(value: Any, *, allow_zero: bool = False) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0 or (value == 0 and not allow_zero):
        return None
    return value


def _validate_core_media_source(event: Mapping[str, Any]) -> tuple[int, int, Mapping[str, Any]]:
    if (
        event.get("schema_version") != SCHEMA_VERSION
        or event.get("transport") != "telegram"
        or event.get("kind") not in {
            "business_message", "edited_business_message",
            "group_message", "edited_group_message",
        }
        or event.get("payload_sha256") != _core_event_digest(event)
    ):
        raise ValueError("Invalid passive media source envelope")
    owner_id = _positive_int(event.get("tenant_owner_id"))
    update_id = _positive_int(event.get("update_id"), allow_zero=True)
    payload = event.get("payload")
    if owner_id is None or update_id is None or not isinstance(payload, Mapping):
        raise ValueError("Invalid passive media source identity")
    return owner_id, update_id, payload


def build_media_jobs(
    event: Mapping[str, Any],
    *,
    bot_id: str,
    processor_version: str = DEFAULT_PROCESSOR_VERSION,
) -> list[dict[str, Any]]:
    """Project an authenticated message DTO into private durable media jobs."""
    owner_id, update_id, payload = _validate_core_media_source(event)
    normalized_bot_id = _bounded_string(bot_id, maximum=32)
    normalized_processor = _bounded_string(processor_version, maximum=128)
    connection_id = _bounded_string(
        payload.get("business_connection_id"), maximum=512
    )
    chat = payload.get("chat")
    raw_chat_id = chat.get("id") if isinstance(chat, Mapping) else None
    chat_id = (
        raw_chat_id
        if isinstance(raw_chat_id, int) and not isinstance(raw_chat_id, bool) and raw_chat_id != 0
        else None
    )
    message_id = _positive_int(payload.get("message_id"), allow_zero=True)
    if (
        normalized_bot_id is None
        or not normalized_bot_id.isdigit()
        or normalized_processor is None
        or connection_id is None
        or chat_id is None
        or message_id is None
    ):
        raise ValueError("Invalid passive media message identity")

    media = payload.get("media")
    if payload.get("has_protected_content") is True:
        return []
    if not isinstance(media, list):
        return []
    raw_received_at = _bounded_string(event.get("received_at_utc"), maximum=64)
    try:
        parsed_received_at = datetime.fromisoformat(
            str(raw_received_at).replace("Z", "+00:00")
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid passive media source receipt time") from exc
    queued_at = _utc_iso(parsed_received_at)
    if queued_at != raw_received_at:
        raise ValueError("Invalid passive media source receipt time")
    jobs: list[dict[str, Any]] = []
    for index, raw_item in enumerate(media[:8]):
        if not isinstance(raw_item, Mapping):
            continue
        kind = raw_item.get("kind")
        if kind not in _MEDIA_KINDS:
            continue
        file_id = _bounded_string(raw_item.get("file_id"), maximum=1024)
        unique_id = _bounded_string(raw_item.get("file_unique_id"), maximum=1024)
        if file_id is None or unique_id is None:
            continue
        size = _positive_int(raw_item.get("file_size"))
        duration = raw_item.get("duration")
        if duration is not None:
            duration = _positive_int(duration)
        preflight_status: str | None = None
        if size is not None and size > MAX_MEDIA_FILE_BYTES:
            preflight_status = "too_large"
        elif isinstance(duration, int) and duration > MAX_MEDIA_DURATION_SECONDS:
            preflight_status = "too_large"

        identity = "\0".join(
            (
                normalized_bot_id,
                str(owner_id),
                str(update_id),
                connection_id,
                str(chat_id),
                str(message_id),
                str(index),
                unique_id,
                normalized_processor,
            )
        )
        job: dict[str, Any] = {
            "job_version": MEDIA_JOB_VERSION,
            "job_id": hashlib.sha256(identity.encode("utf-8", "strict")).hexdigest(),
            "bot_id": normalized_bot_id,
            "tenant_owner_id": owner_id,
            "source_update_id": update_id,
            "business_connection_id": connection_id,
            "chat_id": chat_id,
            "message_id": message_id,
            "media_index": index,
            "media_kind": kind,
            "file_id": file_id,
            "file_unique_id": unique_id,
            "declared_file_size": size,
            "declared_duration": duration,
            "declared_mime_type": _bounded_string(
                raw_item.get("mime_type"), maximum=256, allow_empty=True
            ),
            "declared_file_name": _bounded_string(
                raw_item.get("file_name"), maximum=1024, allow_empty=True
            ),
            "processor_version": normalized_processor,
            "preflight_status": preflight_status,
            "enqueued_at_utc": queued_at,
        }
        job["job_sha256"] = _canonical_digest(job, omit="job_sha256")
        jobs.append(job)
    return jobs


def validate_media_job(value: Mapping[str, Any]) -> tuple[str, str]:
    if not isinstance(value, Mapping) or set(value) != _JOB_KEYS:
        raise ValueError("Invalid passive media job")
    job_id = value.get("job_id")
    digest = value.get("job_sha256")
    if (
        not isinstance(job_id, str)
        or len(job_id) != 64
        or any(char not in "0123456789abcdef" for char in job_id)
        or not isinstance(digest, str)
        or digest != _canonical_digest(value, omit="job_sha256")
    ):
        raise ValueError("Invalid passive media job checksum")
    if value.get("job_version") != MEDIA_JOB_VERSION:
        raise ValueError("Unsupported passive media job version")
    if (
        _bounded_string(value.get("bot_id"), maximum=32) is None
        or not str(value.get("bot_id")).isdigit()
        or _positive_int(value.get("tenant_owner_id")) is None
        or _positive_int(value.get("source_update_id"), allow_zero=True) is None
        or _bounded_string(value.get("business_connection_id"), maximum=512) is None
        or not isinstance(value.get("chat_id"), int)
        or isinstance(value.get("chat_id"), bool)
        or value.get("chat_id") == 0
        or _positive_int(value.get("message_id"), allow_zero=True) is None
        or _positive_int(value.get("media_index"), allow_zero=True) is None
        or int(value.get("media_index")) > 7
        or _bounded_string(value.get("file_unique_id"), maximum=1024) is None
        or _bounded_string(value.get("processor_version"), maximum=128) is None
        or _bounded_string(value.get("enqueued_at_utc"), maximum=64) is None
    ):
        raise ValueError("Invalid passive media job identity")
    if _bounded_string(value.get("file_id"), maximum=1024) is None:
        raise ValueError("Invalid passive media download identifier")
    if value.get("media_kind") not in _MEDIA_KINDS:
        raise ValueError("Invalid passive media kind")
    if value.get("preflight_status") not in {None, "too_large", "unsupported"}:
        raise ValueError("Invalid passive media preflight status")
    for name in ("declared_file_size", "declared_duration"):
        raw_number = value.get(name)
        if raw_number is not None and _positive_int(raw_number) is None:
            raise ValueError("Invalid passive media declared measurement")
    for name, maximum in (("declared_mime_type", 256), ("declared_file_name", 1024)):
        raw_text = value.get(name)
        if raw_text is not None and _bounded_string(
            raw_text, maximum=maximum, allow_empty=True
        ) is None:
            raise ValueError("Invalid passive media declared label")
    return job_id, digest


def build_media_result_event(
    job: Mapping[str, Any],
    *,
    status: str,
    processor: Mapping[str, str],
    transcript: str | None = None,
    language: str | None = None,
    content_sha256: str | None = None,
    actual_bytes: int | None = None,
    duration_ms: int | None = None,
    received_at: datetime | None = None,
) -> dict[str, Any]:
    """Create a digested result envelope without Telegram download capability."""
    validate_media_job(job)
    if status not in _RESULT_STATUSES:
        raise ValueError("Invalid passive media result status")
    if set(processor) != _PROCESSOR_KEYS or any(
        _bounded_string(value, maximum=128) is None for value in processor.values()
    ):
        raise ValueError("Invalid passive media processor identity")
    if transcript is not None and (
        not isinstance(transcript, str)
        or len(transcript) > MAX_TRANSCRIPT_CHARS
        or "\x00" in transcript
    ):
        raise ValueError("Invalid passive media transcript")
    if status == "transcribed" and transcript is None:
        raise ValueError("Transcribed passive media requires transcript text")
    if status != "transcribed" and (transcript is not None or language is not None):
        raise ValueError("Failed passive media result cannot carry transcript text")
    if language is not None and _bounded_string(language, maximum=32) is None:
        raise ValueError("Invalid passive media language")
    if content_sha256 is not None and (
        not isinstance(content_sha256, str)
        or len(content_sha256) != 64
        or any(char not in "0123456789abcdef" for char in content_sha256)
    ):
        raise ValueError("Invalid passive media content checksum")
    for value in (actual_bytes, duration_ms):
        if value is not None and _positive_int(value, allow_zero=True) is None:
            raise ValueError("Invalid passive media measurement")

    payload = {
        "job_id": job["job_id"],
        "business_connection_id": job["business_connection_id"],
        "chat_id": job["chat_id"],
        "message_id": job["message_id"],
        "media_index": job["media_index"],
        "media_kind": job["media_kind"],
        "file_unique_id": job["file_unique_id"],
        "status": status,
        "transcript": transcript,
        "language": language,
        "content_sha256": content_sha256,
        "actual_bytes": actual_bytes,
        "duration_ms": duration_ms,
        "processor": dict(processor),
    }
    event: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "transport": "telegram",
        "tenant_owner_id": job["tenant_owner_id"],
        "update_id": job["source_update_id"],
        "kind": MEDIA_RESULT_KIND,
        "payload": payload,
    }
    event["payload_sha256"] = _core_event_digest(event)
    event["received_at_utc"] = _utc_iso(received_at)
    validate_media_result_event(event)
    return event


def validate_media_result_event(value: Mapping[str, Any]) -> tuple[str, str]:
    if (
        not isinstance(value, Mapping)
        or set(value) != _RESULT_EVENT_KEYS
        or value.get("schema_version") != SCHEMA_VERSION
        or value.get("transport") != "telegram"
        or value.get("kind") != MEDIA_RESULT_KIND
        or value.get("payload_sha256") != _core_event_digest(value)
    ):
        raise ValueError("Invalid passive media result envelope")
    payload = value.get("payload")
    if not isinstance(payload, Mapping) or set(payload) != _RESULT_PAYLOAD_KEYS:
        raise ValueError("Invalid passive media result payload")
    job_id = payload.get("job_id")
    digest = value.get("payload_sha256")
    if (
        not isinstance(job_id, str)
        or len(job_id) != 64
        or any(char not in "0123456789abcdef" for char in job_id)
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(char not in "0123456789abcdef" for char in digest)
    ):
        raise ValueError("Invalid passive media result identity")
    forbidden = {"file_id", "file_path", "path", "token", "url"}
    if forbidden.intersection(payload):
        raise ValueError("Passive media result contains a forbidden capability")
    if (
        _positive_int(value.get("tenant_owner_id")) is None
        or _positive_int(value.get("update_id"), allow_zero=True) is None
        or _bounded_string(payload.get("business_connection_id"), maximum=512)
        is None
        or not isinstance(payload.get("chat_id"), int)
        or isinstance(payload.get("chat_id"), bool)
        or payload.get("chat_id") == 0
        or _positive_int(payload.get("message_id"), allow_zero=True) is None
        or _positive_int(payload.get("media_index"), allow_zero=True) is None
        or int(payload.get("media_index")) > 7
        or payload.get("media_kind") not in _MEDIA_KINDS
        or _bounded_string(payload.get("file_unique_id"), maximum=1024) is None
        or payload.get("status") not in _RESULT_STATUSES
    ):
        raise ValueError("Invalid passive media result scope")
    transcript = payload.get("transcript")
    language = payload.get("language")
    if payload.get("status") == "transcribed":
        if (
            not isinstance(transcript, str)
            or len(transcript) > MAX_TRANSCRIPT_CHARS
            or "\x00" in transcript
        ):
            raise ValueError("Invalid passive media result transcript")
        if language is not None and _bounded_string(language, maximum=32) is None:
            raise ValueError("Invalid passive media result language")
    elif transcript is not None or language is not None:
        raise ValueError("Failed passive media result carries transcript data")
    content_digest = payload.get("content_sha256")
    if content_digest is not None and (
        not isinstance(content_digest, str)
        or len(content_digest) != 64
        or any(char not in "0123456789abcdef" for char in content_digest)
    ):
        raise ValueError("Invalid passive media result content checksum")
    for name in ("actual_bytes", "duration_ms"):
        measurement = payload.get(name)
        if measurement is not None and _positive_int(
            measurement, allow_zero=True
        ) is None:
            raise ValueError("Invalid passive media result measurement")
    processor = payload.get("processor")
    if (
        not isinstance(processor, Mapping)
        or set(processor) != _PROCESSOR_KEYS
        or any(
            _bounded_string(item, maximum=128) is None
            for item in processor.values()
        )
    ):
        raise ValueError("Invalid passive media result processor")
    raw_received_at = _bounded_string(value.get("received_at_utc"), maximum=64)
    try:
        parsed_received_at = datetime.fromisoformat(
            str(raw_received_at).replace("Z", "+00:00")
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid passive media result receipt time") from exc
    if _utc_iso(parsed_received_at) != raw_received_at:
        raise ValueError("Invalid passive media result receipt time")
    return job_id, digest


class DurableMediaSpool:
    """Private fsync-backed job/result queues scoped below one HERMES_HOME."""

    def __init__(self, root: Path, *, hermes_home: Path):
        home = hermes_home.resolve()
        candidate = root if root.is_absolute() else home / root
        if candidate.is_symlink():
            raise RuntimeError("Refusing symlinked Telegram media spool")
        resolved = candidate.resolve()
        try:
            resolved.relative_to(home)
        except ValueError as exc:
            raise RuntimeError("Telegram media spool must stay inside HERMES_HOME") from exc
        for path in (resolved, resolved / "jobs", resolved / "results", resolved / "tmp"):
            _ensure_durable_private_directory(path)
        self.root = resolved
        self.jobs = resolved / "jobs"
        self.results = resolved / "results"
        self.tmp = resolved / "tmp"
        self._lock = threading.RLock()

    @classmethod
    def for_hermes_home(
        cls,
        hermes_home: Path,
        *,
        bot_id: str,
        configured_path: str | None = None,
    ) -> "DurableMediaSpool":
        if not isinstance(bot_id, str) or not bot_id.isdigit():
            raise ValueError("Telegram media spool requires a numeric bot id")
        raw = str(configured_path or "").strip()
        root = Path(raw) if raw else DEFAULT_RELATIVE_MEDIA_SPOOL
        return cls(root / f"telegram-bot-{bot_id}", hermes_home=hermes_home)

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        if path.is_symlink():
            raise RuntimeError("Refusing symlinked Telegram media item")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_size > MAX_MEDIA_SPOOL_ITEM_BYTES
                or stat.S_IMODE(info.st_mode) & 0o077
            ):
                raise RuntimeError("Unsafe Telegram media spool item")
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                fd = -1
                value = json.load(handle)
        finally:
            if fd >= 0:
                os.close(fd)
        if not isinstance(value, dict):
            raise ValueError("Telegram media spool item must be an object")
        return value

    def _put(
        self,
        directory: Path,
        prefix: str,
        identity: str,
        digest: str,
        value: Mapping[str, Any],
        validator,
    ) -> tuple[Path, bool]:
        data = json.dumps(
            dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8") + b"\n"
        if len(data) > MAX_MEDIA_SPOOL_ITEM_BYTES:
            raise ValueError("Telegram media spool item exceeds size limit")
        with self._lock:
            target = directory / f"{prefix}-{identity}.json"
            if target.exists():
                existing = self._read(target)
                existing_id, existing_digest = validator(existing)
                if existing_id != identity or existing_digest != digest:
                    raise RuntimeError("Telegram media spool identity collision")
                return target, False
            if sum(1 for _ in directory.glob(f"{prefix}-*.json")) >= MAX_PENDING_MEDIA_ITEMS:
                raise RuntimeError("Telegram media spool quota exceeded")
            fd, temporary_name = tempfile.mkstemp(prefix=".pending-", dir=directory)
            temporary = Path(temporary_name)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    fd = -1
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.link(temporary, target)
                except FileExistsError:
                    existing = self._read(target)
                    existing_id, existing_digest = validator(existing)
                    if existing_id != identity or existing_digest != digest:
                        raise RuntimeError("Telegram media spool identity collision")
                    return target, False
                _fsync_directory(directory)
                return target, True
            finally:
                if fd >= 0:
                    os.close(fd)
                if temporary.exists():
                    temporary.unlink()
                    _fsync_directory(directory)

    def put_job(self, job: Mapping[str, Any]) -> tuple[Path, bool]:
        identity, digest = validate_media_job(job)
        return self._put(
            self.jobs, "job", identity, digest, job, validate_media_job
        )

    def put_result(self, event: Mapping[str, Any]) -> tuple[Path, bool]:
        identity, digest = validate_media_result_event(event)
        return self._put(
            self.results,
            "result",
            identity,
            digest,
            event,
            validate_media_result_event,
        )

    def stage_event(
        self,
        event: Mapping[str, Any],
        *,
        bot_id: str,
        processor_version: str = DEFAULT_PROCESSOR_VERSION,
    ) -> int:
        jobs = build_media_jobs(
            event, bot_id=bot_id, processor_version=processor_version
        )
        for job in jobs:
            self.put_job(job)
        return len(jobs)

    def _pending(self, directory: Path, prefix: str, validator, limit: int) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        result: list[dict[str, Any]] = []
        with self._lock:
            for path in sorted(directory.glob(f"{prefix}-*.json")):
                if len(result) >= limit:
                    break
                try:
                    value = self._read(path)
                    identity, _digest = validator(value)
                    if path.name != f"{prefix}-{identity}.json":
                        raise RuntimeError("Telegram media spool filename mismatch")
                    result.append(value)
                except Exception as exc:
                    # Retain the unsafe item for operator inspection, but do
                    # not let it poison later jobs/results or log its contents.
                    logger.error(
                        "Telegram media spool retained an invalid %s item (%s)",
                        prefix,
                        type(exc).__name__,
                    )
        return result

    def pending_jobs(self, *, limit: int = 25) -> list[dict[str, Any]]:
        return self._pending(self.jobs, "job", validate_media_job, limit)

    def pending_results(self, *, limit: int = 25) -> list[dict[str, Any]]:
        return self._pending(
            self.results, "result", validate_media_result_event, limit
        )

    def _acknowledge(
        self,
        directory: Path,
        prefix: str,
        value: Mapping[str, Any],
        validator,
    ) -> bool:
        identity, digest = validator(value)
        with self._lock:
            target = directory / f"{prefix}-{identity}.json"
            if not target.exists():
                return False
            existing_id, existing_digest = validator(self._read(target))
            if existing_id != identity or existing_digest != digest:
                raise RuntimeError("Refusing to acknowledge different Telegram media item")
            target.unlink()
            _fsync_directory(directory)
            return True

    def acknowledge_job(self, job: Mapping[str, Any]) -> bool:
        return self._acknowledge(
            self.jobs, "job", job, validate_media_job
        )

    def acknowledge_result(self, event: Mapping[str, Any]) -> bool:
        return self._acknowledge(
            self.results, "result", event, validate_media_result_event
        )

    def reconcile_completed_jobs(self, *, limit: int = 25) -> int:
        """ACK jobs whose result is already durable after a crash window."""
        reconciled = 0
        for job in self.pending_jobs(limit=max(1, min(int(limit), 250))):
            result_path = self.results / f"result-{job['job_id']}.json"
            if not result_path.exists():
                continue
            try:
                result = self._read(result_path)
                result_id, _digest = validate_media_result_event(result)
                if result_id != job["job_id"]:
                    raise RuntimeError("Telegram media result identity mismatch")
            except Exception as exc:
                logger.error(
                    "Telegram media spool retained an invalid result item (%s)",
                    type(exc).__name__,
                )
                continue
            if self.acknowledge_job(job):
                reconciled += 1
        return reconciled

    def create_private_temp(self, job_id: str) -> Path:
        if not isinstance(job_id, str) or len(job_id) != 64:
            raise ValueError("Invalid Telegram media temp identity")
        fd, name = tempfile.mkstemp(prefix=f"media-{job_id[:16]}-", dir=self.tmp)
        os.fchmod(fd, 0o600)
        os.close(fd)
        _fsync_directory(self.tmp)
        return Path(name)

    def cleanup_private_temps(self, *, older_than_seconds: float = 3_600.0) -> int:
        """Remove only stale crash leftovers created by this spool."""
        cutoff = datetime.now(timezone.utc).timestamp() - max(
            60.0, float(older_than_seconds)
        )
        removed = 0
        with self._lock:
            for path in self.tmp.glob("media-*"):
                try:
                    info = path.lstat()
                except FileNotFoundError:
                    continue
                if (
                    path.is_symlink()
                    or not stat.S_ISREG(info.st_mode)
                    or info.st_mtime >= cutoff
                ):
                    continue
                path.unlink()
                removed += 1
            if removed:
                _fsync_directory(self.tmp)
        return removed

    def discard_private_temp(self, path: Path) -> bool:
        """Delete one exact temp created under this spool, never an arbitrary path."""
        candidate = Path(path)
        if candidate.parent != self.tmp or not candidate.name.startswith("media-"):
            raise ValueError("Invalid Telegram media temp path")
        with self._lock:
            try:
                info = candidate.lstat()
            except FileNotFoundError:
                return False
            if candidate.is_symlink() or not stat.S_ISREG(info.st_mode):
                raise RuntimeError("Unsafe Telegram media temp item")
            candidate.unlink()
            _fsync_directory(self.tmp)
            return True


class MediaPermanentError(RuntimeError):
    """A public-safe permanent media processing category."""

    def __init__(self, status: str):
        if status not in _RESULT_STATUSES - {"transcribed"}:
            raise ValueError("invalid permanent media status")
        super().__init__(status)
        self.status = status


class MediaTransientError(RuntimeError):
    """A retryable media failure without transport detail or secret text."""


@dataclass(frozen=True)
class MediaTranscription:
    transcript: str
    language: str | None
    content_sha256: str
    actual_bytes: int
    duration_ms: int


class LocalWhisperASR:
    """Strict loopback-only client for the existing local Whisper service."""

    def __init__(
        self,
        *,
        endpoint: str = "http://127.0.0.1:9000/asr",
        timeout_seconds: float = 600.0,
    ) -> None:
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path != "/asr"
        ):
            raise ValueError("Passive media ASR endpoint must be an exact loopback /asr URL")
        self.endpoint = endpoint
        self.timeout_seconds = max(1.0, min(float(timeout_seconds), 600.0))

    @staticmethod
    def _probe(path: Path) -> int:
        try:
            completed = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration:stream=codec_type",
                    "-of",
                    "json",
                    str(path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MediaPermanentError("unsupported") from exc
        if completed.returncode != 0 or len(completed.stdout) > 256_000:
            raise MediaPermanentError("unsupported")
        try:
            payload = json.loads(completed.stdout.decode("utf-8", "strict"))
            streams = payload.get("streams")
            duration = float((payload.get("format") or {}).get("duration"))
        except (AttributeError, TypeError, ValueError, UnicodeDecodeError) as exc:
            raise MediaPermanentError("unsupported") from exc
        if not isinstance(streams, list) or not any(
            isinstance(stream, dict) and stream.get("codec_type") == "audio"
            for stream in streams
        ):
            raise MediaPermanentError("unsupported")
        if (
            not math.isfinite(duration)
            or duration <= 0
            or duration > MAX_MEDIA_DURATION_SECONDS
        ):
            raise MediaPermanentError("too_large")
        return int(round(duration * 1000))

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _extract_video_note_audio(path: Path) -> Path:
        fd, raw_name = tempfile.mkstemp(
            prefix=f"{path.name}-audio-",
            suffix=".wav",
            dir=path.parent,
        )
        os.fchmod(fd, 0o600)
        os.close(fd)
        output = Path(raw_name)
        _fsync_directory(path.parent)
        try:
            completed = subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-y",
                    "-i",
                    str(path),
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    str(output),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
                check=False,
            )
            info = output.lstat()
            if (
                completed.returncode != 0
                or output.is_symlink()
                or not stat.S_ISREG(info.st_mode)
                or not 44 < info.st_size <= 64 * 1024 * 1024
            ):
                raise MediaPermanentError("unsupported")
            return output
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MediaPermanentError("unsupported") from exc
        except Exception:
            try:
                output.unlink()
                _fsync_directory(output.parent)
            except OSError:
                pass
            raise

    @staticmethod
    def _discard_extracted_audio(path: Path) -> None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        if path.is_symlink() or not stat.S_ISREG(info.st_mode):
            raise RuntimeError("Unsafe extracted passive media path")
        path.unlink()
        _fsync_directory(path.parent)

    async def _request_transcript(
        self, path: Path, *, audio_format: str
    ) -> dict[str, Any]:
        try:
            import httpx

            timeout = httpx.Timeout(
                self.timeout_seconds,
                connect=2.0,
                write=30.0,
                pool=2.0,
            )
            raw: dict[str, Any] | None = None
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                # Quiet/noisy openings in Telegram video notes can make VAD
                # return an empty transcript. Retry that exact file once with
                # VAD disabled before declaring that speech was not recognized.
                for vad_filter in ("true", "false"):
                    with path.open("rb") as handle:
                        async with client.stream(
                            "POST",
                            self.endpoint,
                            params={
                                "encode": "true",
                                "task": "transcribe",
                                "vad_filter": vad_filter,
                                "word_timestamps": "false",
                                "output": "json",
                            },
                            files={
                                "audio_file": (
                                    f"media.{audio_format}",
                                    handle,
                                    "audio/wav"
                                    if audio_format == "wav"
                                    else "audio/ogg",
                                )
                            },
                        ) as response:
                            if response.status_code >= 500 or response.status_code == 429:
                                raise MediaTransientError("asr_temporarily_unavailable")
                            if response.status_code >= 400:
                                raise MediaPermanentError("asr_failed_permanent")
                            response_body = bytearray()
                            async for chunk in response.aiter_bytes():
                                response_body.extend(chunk)
                                if len(response_body) > MAX_ASR_RESPONSE_BYTES:
                                    raise MediaPermanentError("asr_failed_permanent")
                    candidate = json.loads(response_body.decode("utf-8", "strict"))
                    if not isinstance(candidate, dict) or not isinstance(
                        candidate.get("text"), str
                    ):
                        raise MediaPermanentError("asr_failed_permanent")
                    raw = candidate
                    if candidate["text"].strip():
                        break
            if raw is None:
                raise MediaPermanentError("asr_failed_permanent")
            return raw
        except (MediaPermanentError, MediaTransientError):
            raise
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise MediaPermanentError("asr_failed_permanent") from exc
        except Exception as exc:
            raise MediaTransientError("asr_request_failed") from exc

    async def transcribe(
        self, path: Path, *, media_kind: str | None = None
    ) -> MediaTranscription:
        try:
            info = path.stat()
        except OSError as exc:
            raise MediaTransientError("media_temp_unavailable") from exc
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_MEDIA_FILE_BYTES:
            raise MediaPermanentError("too_large")
        duration_ms, content_sha256 = await asyncio.gather(
            asyncio.to_thread(self._probe, path),
            asyncio.to_thread(self._sha256, path),
        )
        extracted_audio: Path | None = None
        try:
            request_path = path
            if media_kind == "video_note":
                extracted_audio = await asyncio.to_thread(
                    self._extract_video_note_audio, path
                )
                request_path = extracted_audio
            raw = await self._request_transcript(
                request_path,
                audio_format="wav" if media_kind == "video_note" else "ogg",
            )
        finally:
            if extracted_audio is not None:
                await asyncio.to_thread(
                    self._discard_extracted_audio, extracted_audio
                )
        transcript = raw["text"].replace("\x00", "").strip()
        if not transcript:
            transcript = "[речь не распознана]"
        if len(transcript) > MAX_TRANSCRIPT_CHARS:
            transcript = transcript[:MAX_TRANSCRIPT_CHARS]
        language = raw.get("language")
        if not isinstance(language, str) or not language or len(language) > 32:
            language = None
        return MediaTranscription(
            transcript=transcript,
            language=language,
            content_sha256=content_sha256,
            actual_bytes=info.st_size,
            duration_ms=duration_ms,
        )


class OpenRouterWhisperASR(LocalWhisperASR):
    """Bounded OpenRouter STT client fixed to Whisper Large V3 Turbo."""

    def __init__(
        self,
        *,
        api_key: str,
        endpoint: str = OPENROUTER_STT_ENDPOINT,
        timeout_seconds: float = 90.0,
    ) -> None:
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "openrouter.ai"
            or parsed.port not in {None, 443}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path != "/api/v1/audio/transcriptions"
        ):
            raise ValueError("Passive media OpenRouter endpoint is not trusted")
        if (
            not isinstance(api_key, str)
            or not 20 <= len(api_key) <= 512
            or api_key != api_key.strip()
            or any(ord(character) < 33 or ord(character) == 127 for character in api_key)
        ):
            raise ValueError("Passive media OpenRouter API key is missing or invalid")
        self.endpoint = endpoint
        self._api_key = api_key
        self.timeout_seconds = max(10.0, min(float(timeout_seconds), 180.0))

    async def _request_transcript(
        self, path: Path, *, audio_format: str
    ) -> dict[str, Any]:
        if audio_format not in {"ogg", "wav"}:
            raise MediaPermanentError("unsupported")
        try:
            import httpx

            audio = await asyncio.to_thread(path.read_bytes)
            if not 0 < len(audio) <= MAX_MEDIA_FILE_BYTES:
                raise MediaPermanentError("too_large")
            payload = {
                "model": OPENROUTER_WHISPER_TURBO_MODEL,
                "input_audio": {
                    "data": base64.b64encode(audio).decode("ascii"),
                    "format": audio_format,
                },
                "temperature": 0,
            }
            timeout = httpx.Timeout(
                self.timeout_seconds,
                connect=5.0,
                write=min(self.timeout_seconds, 60.0),
                pool=5.0,
            )
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                async with client.stream(
                    "POST",
                    self.endpoint,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                ) as response:
                    if response.status_code == 413:
                        raise MediaPermanentError("too_large")
                    if response.status_code in {400, 415, 422}:
                        raise MediaPermanentError("asr_failed_permanent")
                    if response.status_code >= 400:
                        # Credentials, balance, rate limits, provider outages, and
                        # model availability can all be repaired. Retain the job
                        # instead of permanently acknowledging private audio.
                        raise MediaTransientError("openrouter_temporarily_unavailable")
                    response_body = bytearray()
                    async for chunk in response.aiter_bytes():
                        response_body.extend(chunk)
                        if len(response_body) > MAX_ASR_RESPONSE_BYTES:
                            raise MediaPermanentError("asr_failed_permanent")
            raw = json.loads(response_body.decode("utf-8", "strict"))
            if not isinstance(raw, dict) or not isinstance(raw.get("text"), str):
                raise MediaPermanentError("asr_failed_permanent")
            return raw
        except (MediaPermanentError, MediaTransientError):
            raise
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise MediaPermanentError("asr_failed_permanent") from exc
        except Exception as exc:
            raise MediaTransientError("openrouter_request_failed") from exc
