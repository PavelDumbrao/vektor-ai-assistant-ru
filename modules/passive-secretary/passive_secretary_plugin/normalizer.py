"""Validate and enrich capability-free Telegram passive DTOs from Hermes core."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any

from telegram_business_rights import classify_business_connection_rights_mapping

from .settings import Settings


SUPPORTED_KINDS = {
    "business_connection",
    "business_message",
    "edited_business_message",
    "deleted_business_messages",
    "group_message",
    "edited_group_message",
    "business_media_result",
}

_MEDIA_RESULT_PAYLOAD_KEYS = frozenset(
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
_MEDIA_RESULT_EVENT_KEYS = frozenset(
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
_MEDIA_PROCESSOR_KEYS = frozenset(
    {"service", "version", "engine", "model", "quantization"}
)
_MEDIA_RESULT_STATUSES = frozenset(
    {
        "transcribed",
        "unsupported",
        "too_large",
        "download_failed_permanent",
        "asr_failed_permanent",
    }
)
_MEDIA_KINDS = frozenset({"voice", "video_note"})
_HEX = frozenset("0123456789abcdef")

_CONNECTION_SNAPSHOT_KEYS = frozenset(
    {
        "id",
        "user",
        "user_chat_id",
        "date_utc",
        "rights",
        "rights_valid",
        "receive_only",
        "reply_only",
        "capture_authorized",
        "is_enabled",
    }
)
_CONNECTION_SNAPSHOT_USER_KEYS = frozenset(
    {"id", "is_bot", "first_name", "last_name", "username"}
)


def _utc_iso(value: Any, *, fallback: datetime) -> str:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            parsed = fallback
    elif isinstance(value, datetime):
        parsed = value
    else:
        parsed = fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_label(value: Any, *, max_chars: int = 160) -> str:
    text = " ".join(str(value or "").split())
    return text[:max_chars]


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


class PassiveEventNormalizer:
    """Reject mutable/untrusted envelopes and add server-owned scope identifiers."""

    def __init__(self, settings: Settings, reference_key: bytes):
        self.settings = settings
        self.reference_key = reference_key

    def _opaque_ref(self, owner_id: str, namespace: str, raw: Any) -> str:
        digest = hmac.new(
            self.reference_key,
            (
                f"{self.settings.tenant_id}\0{owner_id}\0{self.settings.source_id}\0"
                f"{namespace}\0{raw}"
            ).encode("utf-8", "strict"),
            hashlib.sha256,
        ).hexdigest()[:20]
        return f"{namespace}:{digest}"

    @staticmethod
    def _verify_core_digest(event: dict[str, Any]) -> bool:
        canonical = {
            "schema_version": event.get("schema_version"),
            "transport": event.get("transport"),
            "update_id": event.get("update_id"),
            "kind": event.get("kind"),
            "payload": event.get("payload"),
            # tenant_owner_id is inserted by core and covered by its v2 digest.
            "tenant_owner_id": event.get("tenant_owner_id"),
        }
        expected = event.get("payload_sha256")
        if not isinstance(expected, str) or len(expected) != 64:
            return False
        encoded = json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hmac.compare_digest(hashlib.sha256(encoded).hexdigest(), expected)

    @staticmethod
    def _display_label(entity: dict[str, Any]) -> str:
        title = _safe_label(entity.get("title"))
        full_name = _safe_label(
            f"{entity.get('first_name', '')} {entity.get('last_name', '')}"
        )
        base = title or full_name or "Telegram contact"
        username = _safe_label(entity.get("username"), max_chars=64).lstrip("@")
        if username:
            return _safe_label(f"{base} (@{username})")
        return base

    def normalize(
        self,
        event: Any,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        if not isinstance(event, dict):
            return None
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        update_id = event.get("update_id")
        kind = event.get("kind")
        owner_id = str(event.get("tenant_owner_id") or "")
        if (
            event.get("schema_version") != 2
            or event.get("transport") != "telegram"
            or not isinstance(update_id, int)
            or update_id < 0
            or kind not in SUPPORTED_KINDS
            or owner_id not in self.settings.owner_ids
            or not self._verify_core_digest(event)
        ):
            return None
        if kind == "business_media_result" and set(event) != _MEDIA_RESULT_EVENT_KEYS:
            return None
        received_at = _utc_iso(event.get("received_at_utc"), fallback=current)
        raw_key = (
            f"{self.settings.tenant_id}\0{owner_id}\0{self.settings.source_id}\0"
            f"{self.settings.test_run_id}\0{update_id}\0{event['payload_sha256']}"
        )
        result: dict[str, Any] = {
            "event_key": hmac.new(
                self.reference_key,
                raw_key.encode("utf-8", "strict"),
                hashlib.sha256,
            ).hexdigest(),
            "update_id": update_id,
            "kind": kind,
            "tenant_id": self.settings.tenant_id,
            "tenant_owner_id": owner_id,
            "source_id": self.settings.source_id,
            "test_run_id": self.settings.test_run_id,
            "received_at": received_at,
        }
        payload = _dict(event.get("payload"))
        if kind == "business_media_result":
            return self._media_result(result, payload)
        if kind == "business_connection":
            return self._connection(result, payload, owner_id, current)
        if kind in {
            "business_message", "edited_business_message",
            "group_message", "edited_group_message",
        }:
            normalized = self._message(result, payload, owner_id, current)
        else:
            normalized = self._deleted(result, payload, owner_id)
        if normalized is None:
            return None
        return self._attach_connection_snapshot(
            normalized,
            payload,
            owner_id,
            current,
        )

    def _connection(
        self,
        result: dict[str, Any],
        payload: dict[str, Any],
        owner_id: str,
        current: datetime,
    ) -> dict[str, Any] | None:
        connection_id = str(payload.get("id") or "")
        user = _dict(payload.get("user"))
        if not connection_id or str(user.get("id") or "") != owner_id:
            return None
        raw_rights = payload.get("rights")
        # An empty mapping is a valid receive-only BusinessBotRights payload:
        # PTB may omit every false field from ``to_dict()``.  The core DTO
        # carries ``rights_valid`` separately so missing/unreadable rights
        # remain distinguishable from this valid empty mapping.
        _rights, rights_shape_valid, receive_only, reply_only = (
            classify_business_connection_rights_mapping(raw_rights)
        )
        capture_authorized = (
            payload.get("rights_valid") is True
            and payload.get("receive_only") is receive_only
            and payload.get("reply_only") is reply_only
            and payload.get("capture_authorized") is True
            and (receive_only or reply_only)
        )
        result.update(
            {
                "connection_id": connection_id,
                "owner_ref": self._opaque_ref(owner_id, "owner", owner_id),
                "user_chat_id": str(payload.get("user_chat_id") or ""),
                "enabled": payload.get("is_enabled") is True and capture_authorized,
                "telegram_can_reply": reply_only,
                "connection_date": _utc_iso(payload.get("date_utc"), fallback=current),
            }
        )
        return result

    def _attach_connection_snapshot(
        self,
        result: dict[str, Any],
        payload: dict[str, Any],
        owner_id: str,
        current: datetime,
    ) -> dict[str, Any] | None:
        """Normalize one recovered connection without creating another event."""
        if "connection_snapshot" not in payload:
            return result
        raw_snapshot = payload.get("connection_snapshot")
        if (
            not isinstance(raw_snapshot, dict)
            or set(raw_snapshot) != _CONNECTION_SNAPSHOT_KEYS
        ):
            return None

        raw_connection_id = raw_snapshot.get("id")
        if (
            not isinstance(raw_connection_id, str)
            or not raw_connection_id
            or raw_connection_id != raw_connection_id.strip()
            or len(raw_connection_id) > 512
            or raw_connection_id != result.get("connection_id")
            or any(ord(char) < 0x20 for char in raw_connection_id)
        ):
            return None

        raw_user = raw_snapshot.get("user")
        if (
            not isinstance(raw_user, dict)
            or not set(raw_user).issubset(_CONNECTION_SNAPSHOT_USER_KEYS)
            or not {"id", "is_bot"}.issubset(raw_user)
            or isinstance(raw_user.get("id"), bool)
            or str(raw_user.get("id") or "") != owner_id
            or type(raw_user.get("is_bot")) is not bool
        ):
            return None
        for key in ("first_name", "last_name", "username"):
            if key in raw_user and (
                not isinstance(raw_user[key], str)
                or not raw_user[key]
                or len(raw_user[key]) > 256
            ):
                return None

        user_chat_id = raw_snapshot.get("user_chat_id")
        if (
            isinstance(user_chat_id, bool)
            or not isinstance(user_chat_id, int)
            or user_chat_id <= 0
        ):
            return None
        raw_date = raw_snapshot.get("date_utc")
        if (
            not isinstance(raw_date, str)
            or len(raw_date) > 64
            or _utc_iso(raw_date, fallback=current) != raw_date
        ):
            return None

        raw_rights = raw_snapshot.get("rights")
        if not isinstance(raw_rights, dict) or not all(
            isinstance(name, str)
            and name.startswith("can_")
            and type(value) is bool
            for name, value in raw_rights.items()
        ):
            return None
        for flag in (
            "rights_valid",
            "receive_only",
            "reply_only",
            "capture_authorized",
            "is_enabled",
        ):
            if type(raw_snapshot.get(flag)) is not bool:
                return None

        snapshot_base = {
            key: result[key]
            for key in (
                "update_id",
                "tenant_id",
                "tenant_owner_id",
                "source_id",
                "test_run_id",
                "received_at",
            )
        }
        normalized_snapshot = self._connection(
            snapshot_base,
            raw_snapshot,
            owner_id,
            current,
        )
        if (
            normalized_snapshot is None
            or normalized_snapshot.get("connection_id") != result.get("connection_id")
            or normalized_snapshot.get("enabled") is not True
        ):
            return None
        result["connection_snapshot"] = normalized_snapshot
        return result

    def _message(
        self,
        result: dict[str, Any],
        payload: dict[str, Any],
        owner_id: str,
        current: datetime,
    ) -> dict[str, Any] | None:
        connection_id = str(payload.get("business_connection_id") or "")
        chat = _dict(payload.get("chat"))
        sender = _dict(payload.get("from")) or _dict(payload.get("sender_chat"))
        chat_id = str(chat.get("id") or "")
        sender_id = str(sender.get("id") or "")
        message_id = payload.get("message_id")
        is_group = result["kind"] in {"group_message", "edited_group_message"}
        expected_chat_types = {"group", "supergroup"} if is_group else {"private"}
        if (
            not connection_id
            or not chat_id
            or chat.get("type") not in expected_chat_types
            or (is_group and payload.get("group_passive") is not True)
            or (is_group and connection_id != "group-passive-v1")
            or not isinstance(message_id, int)
            or message_id < 0
        ):
            return None
        media = payload.get("media") if isinstance(payload.get("media"), list) else []
        stored_media = self._storage_media(media)
        reply_to_message_id = payload.get("reply_to_message_id")
        if (
            isinstance(reply_to_message_id, bool)
            or not isinstance(reply_to_message_id, int)
            or reply_to_message_id < 0
        ):
            reply_to_message_id = None
        raw_media_group_id = payload.get("media_group_id")
        media_group_id = (
            raw_media_group_id[:512]
            if isinstance(raw_media_group_id, str) and raw_media_group_id
            else None
        )
        content_kind = "text" if payload.get("text") is not None else (
            str(_dict(media[0]).get("kind") or "other") if media else "other"
        )
        result.update(
            {
                "connection_id": connection_id,
                "chat_id": chat_id,
                "source_ref": self._opaque_ref(owner_id, "chat", chat_id),
                "chat_label": self._display_label(chat),
                "message_id": message_id,
                "message_ref": self._opaque_ref(owner_id, "message", f"{chat_id}:{message_id}"),
                "reply_to_message_id": reply_to_message_id,
                "reply_to_message_ref": (
                    self._opaque_ref(
                        owner_id,
                        "message",
                        f"{chat_id}:{reply_to_message_id}",
                    )
                    if reply_to_message_id is not None
                    else None
                ),
                "media_group_id": media_group_id,
                "media_group_ref": (
                    self._opaque_ref(
                        owner_id,
                        "media_group",
                        f"{chat_id}:{media_group_id}",
                    )
                    if media_group_id is not None
                    else None
                ),
                "sender_user_id": sender_id,
                "sender_ref": self._opaque_ref(owner_id, "sender", sender_id or "unknown"),
                "sender_label": self._display_label(sender),
                "direction": "outgoing" if sender_id == owner_id else "incoming",
                "body": str(payload["text"]) if payload.get("text") is not None else None,
                "caption": str(payload["caption"]) if payload.get("caption") is not None else None,
                "content_kind": content_kind,
                # Telegram file_id is a download capability.  Core's private
                # media job spool retains it; PostgreSQL stores metadata only.
                "attachment": {"items": stored_media},
                "sent_at": _utc_iso(payload.get("date_utc"), fallback=current),
                "edited_at": (
                    _utc_iso(payload.get("edit_date_utc"), fallback=current)
                    if result["kind"] in {
                        "edited_business_message", "edited_group_message"
                    }
                    else None
                ),
            }
        )
        return result

    @staticmethod
    def _storage_media(media: list[Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for raw_item in media[:8]:
            if not isinstance(raw_item, dict):
                continue
            kind = raw_item.get("kind")
            unique_id = raw_item.get("file_unique_id")
            if (
                not isinstance(kind, str)
                or not kind
                or len(kind) > 32
                or not isinstance(unique_id, str)
                or not unique_id
                or len(unique_id) > 1024
                or "\x00" in unique_id
            ):
                continue
            item: dict[str, Any] = {
                "kind": kind,
                "file_unique_id": unique_id,
            }
            for name, maximum in (("file_name", 1024), ("mime_type", 256)):
                value = raw_item.get(name)
                if isinstance(value, str) and len(value) <= maximum and "\x00" not in value:
                    item[name] = value
            for name in ("file_size", "duration", "width", "height"):
                value = raw_item.get(name)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    item[name] = value
            result.append(item)
        return result

    @staticmethod
    def _media_result(
        result: dict[str, Any], payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        if set(payload) != _MEDIA_RESULT_PAYLOAD_KEYS:
            return None
        job_id = payload.get("job_id")
        content_sha256 = payload.get("content_sha256")
        connection_id = payload.get("business_connection_id")
        file_unique_id = payload.get("file_unique_id")
        processor = payload.get("processor")
        status = payload.get("status")
        chat_id = payload.get("chat_id")
        message_id = payload.get("message_id")
        media_index = payload.get("media_index")
        if (
            not isinstance(job_id, str)
            or len(job_id) != 64
            or any(character not in _HEX for character in job_id)
            or not isinstance(connection_id, str)
            or not connection_id
            or connection_id != connection_id.strip()
            or len(connection_id) > 512
            or any(ord(character) < 0x20 for character in connection_id)
            or isinstance(chat_id, bool)
            or not isinstance(chat_id, int)
            or chat_id == 0
            or isinstance(message_id, bool)
            or not isinstance(message_id, int)
            or message_id < 0
            or isinstance(media_index, bool)
            or not isinstance(media_index, int)
            or not 0 <= media_index <= 7
            or payload.get("media_kind") not in _MEDIA_KINDS
            or not isinstance(file_unique_id, str)
            or not file_unique_id
            or len(file_unique_id) > 1024
            or "\x00" in file_unique_id
            or status not in _MEDIA_RESULT_STATUSES
            or not isinstance(processor, dict)
            or set(processor) != _MEDIA_PROCESSOR_KEYS
            or any(
                not isinstance(value, str)
                or not value
                or len(value) > 128
                or "\x00" in value
                for value in processor.values()
            )
        ):
            return None
        transcript = payload.get("transcript")
        language = payload.get("language")
        if status == "transcribed":
            if (
                not isinstance(transcript, str)
                or len(transcript) > 75_000
                or "\x00" in transcript
                or (
                    language is not None
                    and (
                        not isinstance(language, str)
                        or not language
                        or len(language) > 32
                        or "\x00" in language
                    )
                )
            ):
                return None
        elif transcript is not None or language is not None:
            return None
        if content_sha256 is not None and (
            not isinstance(content_sha256, str)
            or len(content_sha256) != 64
            or any(character not in _HEX for character in content_sha256)
        ):
            return None
        for name in ("actual_bytes", "duration_ms"):
            value = payload.get(name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                return None
        if isinstance(payload.get("actual_bytes"), int) and payload["actual_bytes"] > 20 * 1024 * 1024:
            return None
        if isinstance(payload.get("duration_ms"), int) and payload["duration_ms"] > 1_800_000:
            return None
        result.update(
            {
                "job_id": job_id,
                "connection_id": connection_id,
                "chat_id": str(chat_id),
                "message_id": message_id,
                "media_index": media_index,
                "media_kind": payload["media_kind"],
                "file_unique_id": file_unique_id,
                "status": status,
                "transcript": transcript,
                "language": language,
                "content_sha256": content_sha256,
                "actual_bytes": payload.get("actual_bytes"),
                "duration_ms": payload.get("duration_ms"),
                "processor_service": processor["service"],
                "processor_version": processor["version"],
                "processor_engine": processor["engine"],
                "processor_model": processor["model"],
                "processor_quantization": processor["quantization"],
            }
        )
        return result

    def _deleted(
        self,
        result: dict[str, Any],
        payload: dict[str, Any],
        owner_id: str,
    ) -> dict[str, Any] | None:
        connection_id = str(payload.get("business_connection_id") or "")
        chat = _dict(payload.get("chat"))
        chat_id = str(chat.get("id") or "")
        raw_ids = payload.get("message_ids")
        message_ids = (
            [value for value in raw_ids if isinstance(value, int) and value >= 0]
            if isinstance(raw_ids, list)
            else []
        )
        if not connection_id or not chat_id or not message_ids:
            return None
        result.update(
            {
                "connection_id": connection_id,
                "chat_id": chat_id,
                "source_ref": self._opaque_ref(owner_id, "chat", chat_id),
                "chat_label": self._display_label(chat),
                "message_ids": message_ids,
                "message_refs": [
                    self._opaque_ref(owner_id, "message", f"{chat_id}:{message_id}")
                    for message_id in message_ids
                ],
            }
        )
        return result
