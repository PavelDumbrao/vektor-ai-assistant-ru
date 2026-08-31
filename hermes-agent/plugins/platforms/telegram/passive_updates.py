"""Capability-free DTOs and durable inbox for Telegram passive routing.

This module deliberately has no Bot API client and no send/edit/delete
capability.  The Telegram adapter converts PTB objects into plain JSON data,
fsyncs that data here, and only then notifies standalone collector plugins.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import stat
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

try:  # POSIX-only durability boundary; passive Business capture fails closed elsewhere.
    import fcntl
except ImportError:  # pragma: no cover - exercised only on non-POSIX hosts
    fcntl = None  # type: ignore[assignment]


SCHEMA_VERSION = 2
DEFAULT_RELATIVE_SPOOL = Path("passive-secretary") / "inbox"
MAX_SPOOL_EVENT_BYTES = 512_000
MAX_PASSIVE_GROUP_RECORDS = 256

logger = logging.getLogger(__name__)

_BUSINESS_REPLY_NON_ACTION_RIGHTS = frozenset({"can_read_messages"})


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _ensure_durable_private_directory(path: Path) -> None:
    """Create a private directory chain and durably commit every new entry."""
    missing: list[Path] = []
    cursor = path
    while not cursor.exists():
        if cursor.is_symlink():
            raise RuntimeError("Refusing symlinked Telegram passive directory")
        missing.append(cursor)
        parent = cursor.parent
        if parent == cursor:
            raise RuntimeError("Telegram passive directory has no existing ancestor")
        cursor = parent
    if cursor.is_symlink() or not cursor.is_dir():
        raise RuntimeError("Telegram passive directory ancestor is unsafe")

    for directory in reversed(missing):
        if directory.is_symlink():
            raise RuntimeError("Refusing symlinked Telegram passive directory")
        directory.mkdir(mode=0o700)
        os.chmod(directory, 0o700)
        _fsync_directory(directory)
        _fsync_directory(directory.parent)

    if path.is_symlink() or not path.is_dir():
        raise RuntimeError("Telegram passive directory is unsafe")
    os.chmod(path, 0o700)
    _fsync_directory(path)


def _utc_iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        value = datetime.fromtimestamp(value, tz=timezone.utc)
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _str_or_none(value: Any, *, max_chars: int = 4096) -> str | None:
    if not isinstance(value, str):
        return None
    value = value[:max_chars]
    return value if value else None


def _identity(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    result = {
        "id": _int_or_none(getattr(value, "id", None)),
        "is_bot": bool(getattr(value, "is_bot", False)),
        "first_name": _str_or_none(getattr(value, "first_name", None), max_chars=256),
        "last_name": _str_or_none(getattr(value, "last_name", None), max_chars=256),
        "username": _str_or_none(getattr(value, "username", None), max_chars=256),
    }
    return {key: item for key, item in result.items() if item is not None}


def _chat(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    result = {
        "id": _int_or_none(getattr(value, "id", None)),
        "type": _str_or_none(getattr(value, "type", None), max_chars=32),
        "title": _str_or_none(getattr(value, "title", None), max_chars=512),
        "first_name": _str_or_none(getattr(value, "first_name", None), max_chars=256),
        "last_name": _str_or_none(getattr(value, "last_name", None), max_chars=256),
        "username": _str_or_none(getattr(value, "username", None), max_chars=256),
        "is_forum": bool(getattr(value, "is_forum", False)),
    }
    return {key: item for key, item in result.items() if item is not None}


def _file_metadata(kind: str, value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    result = {
        "kind": kind,
        "file_id": _str_or_none(getattr(value, "file_id", None), max_chars=1024),
        "file_unique_id": _str_or_none(
            getattr(value, "file_unique_id", None), max_chars=1024
        ),
        "file_name": _str_or_none(getattr(value, "file_name", None), max_chars=1024),
        "mime_type": _str_or_none(getattr(value, "mime_type", None), max_chars=256),
        "file_size": _int_or_none(getattr(value, "file_size", None)),
        "duration": _int_or_none(getattr(value, "duration", None)),
        "width": _int_or_none(getattr(value, "width", None)),
        "height": _int_or_none(getattr(value, "height", None)),
    }
    return {key: item for key, item in result.items() if item is not None}


def _message_media(message: Any) -> list[dict[str, Any]]:
    media: list[dict[str, Any]] = []
    photos = getattr(message, "photo", None) or []
    if photos:
        item = _file_metadata("photo", photos[-1])
        if item:
            media.append(item)
    for kind in ("video", "audio", "voice", "document", "sticker", "animation", "video_note"):
        item = _file_metadata(kind, getattr(message, kind, None))
        if item:
            media.append(item)
    return media


def _message(value: Any) -> dict[str, Any]:
    reply = getattr(value, "reply_to_message", None)
    result = {
        "business_connection_id": _str_or_none(
            getattr(value, "business_connection_id", None), max_chars=512
        ),
        "message_id": _int_or_none(getattr(value, "message_id", None)),
        "date_utc": _utc_iso(getattr(value, "date", None)),
        "edit_date_utc": _utc_iso(getattr(value, "edit_date", None)),
        "chat": _chat(getattr(value, "chat", None)),
        "from": _identity(getattr(value, "from_user", None)),
        "sender_chat": _chat(getattr(value, "sender_chat", None)),
        "sender_business_bot": _identity(getattr(value, "sender_business_bot", None)),
        "text": _str_or_none(getattr(value, "text", None), max_chars=100_000),
        "caption": _str_or_none(getattr(value, "caption", None), max_chars=10_000),
        "reply_to_message_id": _int_or_none(getattr(reply, "message_id", None)),
        "message_thread_id": _int_or_none(getattr(value, "message_thread_id", None)),
        "media_group_id": _str_or_none(getattr(value, "media_group_id", None), max_chars=512),
        "media": _message_media(value),
        "has_protected_content": bool(
            getattr(value, "has_protected_content", False)
        ),
    }
    return {key: item for key, item in result.items() if item is not None}


def classify_business_connection_rights_mapping(
    value: Any,
) -> tuple[dict[str, bool], bool, bool, bool]:
    """Classify one JSON-like Business rights mapping fail-closed.

    Telegram Desktop currently couples ``can_reply`` with
    ``can_read_messages``.  Reading is not an action performed by this module,
    so either boolean value is accepted only while ``can_reply`` is true and
    every other reported or future ``can_*`` action right remains false.
    """
    if not isinstance(value, Mapping):
        return {}, False, False, False
    rights: dict[str, bool] = {}
    for key, item in value.items():
        if (
            not isinstance(key, str)
            or not key.startswith("can_")
            or type(item) is not bool
        ):
            return {}, False, False, False
        rights[key] = item
    receive_only = all(enabled is False for enabled in rights.values())
    reply_only = rights.get("can_reply") is True and all(
        enabled is False
        for name, enabled in rights.items()
        if name != "can_reply" and name not in _BUSINESS_REPLY_NON_ACTION_RIGHTS
    )
    return rights, True, receive_only, reply_only


def business_connection_rights_state(
    value: Any,
) -> tuple[dict[str, bool], bool, bool, bool]:
    """Serialize rights while preserving whether the source was trustworthy.

    An empty mapping is receive-only only when it came from a real rights object
    whose ``to_dict`` call succeeded. Missing or unreadable rights must remain
    distinguishable from that valid empty mapping for downstream retention.
    ``reply_only`` accepts Telegram Desktop's minimal 2/5 profile where
    ``can_reply`` and the coupled non-action ``can_read_messages`` are true.
    Every other reported or future Telegram action right must remain false.
    """
    if value is None:
        return {}, False, False, False
    raw: Any = None
    to_dict = getattr(value, "to_dict", None)
    if not callable(to_dict):
        return {}, False, False, False
    try:
        raw = to_dict()
    except Exception:
        return {}, False, False, False
    return classify_business_connection_rights_mapping(raw)


def _business_connection_payload(
    value: Any,
    *,
    business_reply_enabled: bool,
) -> dict[str, Any]:
    """Serialize one Business connection without retaining PTB capability."""
    if type(business_reply_enabled) is not bool:
        raise ValueError("business_reply_enabled must be a literal boolean")
    rights, rights_valid, receive_only, reply_only = (
        business_connection_rights_state(getattr(value, "rights", None))
    )
    capture_authorized = rights_valid and (
        receive_only or (business_reply_enabled and reply_only)
    )
    result = {
        "id": _str_or_none(getattr(value, "id", None), max_chars=512),
        "user": _identity(getattr(value, "user", None)),
        "user_chat_id": _int_or_none(getattr(value, "user_chat_id", None)),
        "date_utc": _utc_iso(getattr(value, "date", None)),
        "rights": rights,
        "rights_valid": rights_valid,
        "receive_only": receive_only,
        "reply_only": reply_only,
        "capture_authorized": capture_authorized,
        "is_enabled": bool(getattr(value, "is_enabled", False)),
    }
    return {key: item for key, item in result.items() if item is not None}


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


def _normalize_connection_snapshot_mapping(
    value: Mapping[str, Any],
    *,
    expected_connection_id: str,
    tenant_owner_id: int,
    business_reply_enabled: bool,
) -> dict[str, Any]:
    """Copy and verify the exact capability-free recovery snapshot schema."""
    if type(business_reply_enabled) is not bool:
        raise ValueError("business_reply_enabled must be a literal boolean")
    if not isinstance(value, Mapping) or set(value) != _CONNECTION_SNAPSHOT_KEYS:
        raise ValueError("Invalid recovered Telegram Business connection snapshot")

    connection_id = value.get("id")
    if (
        not isinstance(connection_id, str)
        or not connection_id
        or connection_id != connection_id.strip()
        or len(connection_id) > 512
        or connection_id != expected_connection_id
        or any(ord(char) < 0x20 for char in connection_id)
    ):
        raise ValueError("Recovered Telegram Business connection id mismatch")

    raw_user = value.get("user")
    if (
        not isinstance(raw_user, Mapping)
        or not set(raw_user).issubset(_CONNECTION_SNAPSHOT_USER_KEYS)
        or not {"id", "is_bot"}.issubset(raw_user)
    ):
        raise ValueError("Invalid recovered Telegram Business owner identity")
    user_id = _int_or_none(raw_user.get("id"))
    if user_id != tenant_owner_id or user_id is None or user_id <= 0:
        raise ValueError("Recovered Telegram Business owner mismatch")
    if type(raw_user.get("is_bot")) is not bool:
        raise ValueError("Invalid recovered Telegram Business owner identity")
    user: dict[str, Any] = {
        "id": user_id,
        "is_bot": raw_user["is_bot"],
    }
    for key in ("first_name", "last_name", "username"):
        if key not in raw_user:
            continue
        normalized = _str_or_none(raw_user.get(key), max_chars=256)
        if normalized is None or normalized != raw_user[key]:
            raise ValueError("Invalid recovered Telegram Business owner identity")
        user[key] = normalized

    user_chat_id = _int_or_none(value.get("user_chat_id"))
    if user_chat_id is None or user_chat_id <= 0 or user_chat_id != value.get(
        "user_chat_id"
    ):
        raise ValueError("Invalid recovered Telegram Business user chat id")

    date_utc = value.get("date_utc")
    if not isinstance(date_utc, str) or len(date_utc) > 64:
        raise ValueError("Invalid recovered Telegram Business connection date")
    try:
        parsed_date = datetime.fromisoformat(date_utc.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            "Invalid recovered Telegram Business connection date"
        ) from exc
    if _utc_iso(parsed_date) != date_utc:
        raise ValueError("Non-canonical recovered Telegram Business connection date")

    rights, rights_valid, receive_only, reply_only = (
        classify_business_connection_rights_mapping(value.get("rights"))
    )
    if not rights_valid:
        raise ValueError("Invalid recovered Telegram Business rights")
    capture_authorized = receive_only or (business_reply_enabled and reply_only)
    expected_flags = {
        "rights_valid": True,
        "receive_only": receive_only,
        "reply_only": reply_only,
        "capture_authorized": capture_authorized,
        "is_enabled": True,
    }
    if any(type(value.get(name)) is not bool for name in expected_flags):
        raise ValueError("Invalid recovered Telegram Business snapshot flags")
    if any(
        value.get(name) is not expected
        for name, expected in expected_flags.items()
    ):
        raise ValueError("Untrusted recovered Telegram Business snapshot flags")
    if not capture_authorized:
        raise ValueError("Recovered Telegram Business rights are not authorized")

    return {
        "id": connection_id,
        "user": user,
        "user_chat_id": user_chat_id,
        "date_utc": date_utc,
        "rights": rights,
        **expected_flags,
    }


def normalize_recovered_business_connection_snapshot(
    value: Any,
    *,
    expected_connection_id: str,
    tenant_owner_id: int,
    business_reply_enabled: bool,
) -> dict[str, Any]:
    """Return a strict JSON-only snapshot for an authoritative API recovery."""
    serialized = _business_connection_payload(
        value,
        business_reply_enabled=business_reply_enabled,
    )
    return _normalize_connection_snapshot_mapping(
        serialized,
        expected_connection_id=expected_connection_id,
        tenant_owner_id=tenant_owner_id,
        business_reply_enabled=business_reply_enabled,
    )


def business_update_kind(update: Any) -> str:
    if getattr(update, "business_connection", None) is not None:
        return "business_connection"
    if getattr(update, "business_message", None) is not None:
        return "business_message"
    if getattr(update, "edited_business_message", None) is not None:
        return "edited_business_message"
    if getattr(update, "deleted_business_messages", None) is not None:
        return "deleted_business_messages"
    raise ValueError("not a Telegram Business update")


def group_passive_update_kind(update: Any) -> str:
    """Return the supported ordinary group update kind.

    Telegram does not expose a generic deleted-message update for normal bot
    group chats, so this contract intentionally covers only new messages and
    edits.  Both are represented by ordinary PTB ``Message`` objects.
    """
    message = getattr(update, "message", None)
    if message is not None:
        return "group_message"
    message = getattr(update, "edited_message", None)
    if message is not None:
        return "edited_group_message"
    raise ValueError("not a supported Telegram group update")


def build_group_passive_update_dto(
    update: Any,
    *,
    tenant_owner_id: int,
    received_at: datetime | None = None,
) -> dict[str, Any]:
    """Convert one ordinary group message/edit into a capability-free DTO."""
    kind = group_passive_update_kind(update)
    update_id = _int_or_none(getattr(update, "update_id", None))
    owner_id = _int_or_none(tenant_owner_id)
    if update_id is None or update_id < 0:
        raise ValueError("Telegram update_id must be a non-negative integer")
    if owner_id is None or owner_id <= 0:
        raise ValueError("tenant_owner_id must be a positive integer")
    message = getattr(update, "message" if kind == "group_message" else "edited_message")
    payload = _message(message)
    chat = payload.get("chat")
    chat_id = chat.get("id") if isinstance(chat, Mapping) else None
    chat_type = chat.get("type") if isinstance(chat, Mapping) else None
    if (
        isinstance(chat_id, bool)
        or not isinstance(chat_id, int)
        or chat_id >= 0
        or chat_type not in {"group", "supergroup"}
    ):
        raise ValueError("Telegram passive group update has invalid chat scope")
    # This marker is storage identity only and is never accepted by the
    # Business outbound JOIN, which requires a real business_connections row.
    payload["business_connection_id"] = "group-passive-v1"
    payload["group_passive"] = True

    canonical_payload = {
        "schema_version": SCHEMA_VERSION,
        "transport": "telegram",
        "tenant_owner_id": owner_id,
        "update_id": update_id,
        "kind": kind,
        "payload": payload,
    }
    encoded = json.dumps(
        canonical_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    envelope = dict(canonical_payload)
    envelope["payload_sha256"] = hashlib.sha256(encoded).hexdigest()
    envelope["received_at_utc"] = _utc_iso(received_at or datetime.now(timezone.utc))
    return envelope


def build_business_update_dto(
    update: Any,
    *,
    tenant_owner_id: int,
    business_reply_enabled: bool = False,
    connection_snapshot: Mapping[str, Any] | None = None,
    received_at: datetime | None = None,
) -> dict[str, Any]:
    """Convert a PTB Business update to a capability-free JSON object."""
    if type(business_reply_enabled) is not bool:
        raise ValueError("business_reply_enabled must be a literal boolean")
    kind = business_update_kind(update)
    update_id = _int_or_none(getattr(update, "update_id", None))
    if update_id is None or update_id < 0:
        raise ValueError("Telegram update_id must be a non-negative integer")
    owner_id = _int_or_none(tenant_owner_id)
    if owner_id is None or owner_id <= 0:
        raise ValueError("tenant_owner_id must be a positive integer")

    payload: dict[str, Any]
    if kind == "business_connection":
        if connection_snapshot is not None:
            raise ValueError(
                "Telegram Business lifecycle DTO cannot contain a recovery snapshot"
            )
        payload = _business_connection_payload(
            update.business_connection,
            business_reply_enabled=business_reply_enabled,
        )
    elif kind in {"business_message", "edited_business_message"}:
        payload = _message(getattr(update, kind))
    else:
        deleted = update.deleted_business_messages
        payload = {
            "business_connection_id": _str_or_none(
                getattr(deleted, "business_connection_id", None), max_chars=512
            ),
            "chat": _chat(getattr(deleted, "chat", None)),
            "message_ids": [
                message_id
                for message_id in (
                    _int_or_none(item) for item in (getattr(deleted, "message_ids", None) or [])
                )
                if message_id is not None
            ],
        }
        payload = {key: item for key, item in payload.items() if item is not None}

    if connection_snapshot is not None:
        expected_connection_id = payload.get("business_connection_id")
        if not isinstance(expected_connection_id, str):
            raise ValueError(
                "Telegram Business recovery snapshot requires a message connection id"
            )
        payload["connection_snapshot"] = _normalize_connection_snapshot_mapping(
            connection_snapshot,
            expected_connection_id=expected_connection_id,
            tenant_owner_id=owner_id,
            business_reply_enabled=business_reply_enabled,
        )

    canonical_payload = {
        "schema_version": SCHEMA_VERSION,
        "transport": "telegram",
        "tenant_owner_id": owner_id,
        "update_id": update_id,
        "kind": kind,
        "payload": payload,
    }
    encoded = json.dumps(
        canonical_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    envelope = dict(canonical_payload)
    envelope["payload_sha256"] = hashlib.sha256(encoded).hexdigest()
    envelope["received_at_utc"] = _utc_iso(received_at or datetime.now(timezone.utc))
    return envelope


class DurableUpdateSpool:
    """Atomic, fsync-backed JSON inbox scoped beneath one HERMES_HOME."""

    def __init__(self, root: Path, *, hermes_home: Path):
        home = hermes_home.resolve()
        candidate = root if root.is_absolute() else home / root
        if candidate.is_symlink():
            raise RuntimeError("Refusing symlinked Telegram passive spool")
        resolved = candidate.resolve()
        try:
            relative = resolved.relative_to(home)
        except ValueError as exc:
            raise RuntimeError("Telegram passive spool must stay inside HERMES_HOME") from exc

        current = home
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise RuntimeError("Refusing symlinked Telegram passive spool path")
        _ensure_durable_private_directory(resolved)
        self.root = resolved
        self._lock = threading.RLock()

    @classmethod
    def for_hermes_home(
        cls,
        hermes_home: Path,
        configured_path: str | None = None,
        *,
        namespace: str | None = None,
    ) -> "DurableUpdateSpool":
        raw = (configured_path or "").strip()
        root = Path(raw) if raw else DEFAULT_RELATIVE_SPOOL
        if namespace:
            if not namespace.replace("-", "").isalnum():
                raise ValueError("Invalid Telegram passive spool namespace")
            root = root / namespace
        return cls(root, hermes_home=hermes_home)

    @staticmethod
    def _payload_digest(event: Mapping[str, Any]) -> str:
        canonical_payload = {
            "schema_version": event.get("schema_version"),
            "transport": event.get("transport"),
            "tenant_owner_id": event.get("tenant_owner_id"),
            "update_id": event.get("update_id"),
            "kind": event.get("kind"),
            "payload": event.get("payload"),
        }
        encoded = json.dumps(
            canonical_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _validate_event(self, event: Mapping[str, Any]) -> tuple[int, str]:
        update_id = _int_or_none(event.get("update_id"))
        digest = event.get("payload_sha256")
        if update_id is None or update_id < 0 or not isinstance(digest, str):
            raise ValueError("Invalid Telegram passive event envelope")
        if event.get("transport") != "telegram":
            raise ValueError("Invalid Telegram passive event transport")
        if self._payload_digest(event) != digest:
            raise ValueError("Telegram passive event checksum mismatch")
        return update_id, digest

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        _fsync_directory(path)

    @staticmethod
    def _read_json_file(path: Path) -> dict[str, Any]:
        if path.is_symlink():
            raise RuntimeError("Refusing symlinked Telegram passive event")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            file_stat = os.fstat(fd)
            mode = file_stat.st_mode
            if not stat.S_ISREG(mode):
                raise RuntimeError("Telegram passive event is not a regular file")
            if file_stat.st_size > MAX_SPOOL_EVENT_BYTES:
                raise RuntimeError("Telegram passive event exceeds the size limit")
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                fd = -1
                value = json.load(handle)
        finally:
            if fd >= 0:
                os.close(fd)
        if not isinstance(value, dict):
            raise ValueError("Telegram passive event must be a JSON object")
        return value

    def put(self, event: Mapping[str, Any]) -> tuple[Path, bool]:
        update_id, digest = self._validate_event(event)

        data = json.dumps(
            dict(event),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        with self._lock:
            target = self.root / f"update-{update_id}.json"
            if target.is_symlink():
                raise RuntimeError("Refusing symlinked Telegram passive event")
            if target.exists():
                existing = self._read_json_file(target)
                _existing_id, existing_digest = self._validate_event(existing)
                if existing_digest != digest:
                    raise RuntimeError("Telegram update_id collision in passive spool")
                return target, False

            fd, temp_name = tempfile.mkstemp(prefix=".pending-", dir=self.root)
            temp = Path(temp_name)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    fd = -1
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    # Hard-link creation is atomic and refuses to overwrite an
                    # event another process may have committed concurrently.
                    os.link(temp, target)
                except FileExistsError:
                    existing = self._read_json_file(target)
                    _existing_id, existing_digest = self._validate_event(existing)
                    if existing_digest != digest:
                        raise RuntimeError(
                            "Telegram update_id collision in passive spool"
                        )
                    return target, False
                self._fsync_directory(self.root)
                return target, True
            finally:
                if fd >= 0:
                    os.close(fd)
                if temp.exists():
                    temp.unlink()
                    # The hard-linked target was committed above; persist the
                    # removal of its temporary sibling as well so a crash
                    # cannot resurrect an ignored .pending-* PII copy.
                    self._fsync_directory(self.root)

    def pending(self, *, limit: int = 1000) -> list[dict[str, Any]]:
        """Return valid pending events without letting one poison file block replay."""
        if limit <= 0:
            return []
        with self._lock:
            paths_with_ids: list[tuple[int, Path]] = []
            for path in self.root.glob("update-*.json"):
                raw_id = path.stem.removeprefix("update-")
                if not raw_id.isdigit():
                    logger.critical(
                        "Telegram passive spool contains an invalid filename; "
                        "the file is retained for operator recovery"
                    )
                    continue
                paths_with_ids.append((int(raw_id), path))
            events: list[dict[str, Any]] = []
            for _path_id, path in sorted(paths_with_ids):
                if len(events) >= limit:
                    break
                try:
                    event = self._read_json_file(path)
                    update_id, _digest = self._validate_event(event)
                    if path.name != f"update-{update_id}.json":
                        raise RuntimeError(
                            "Telegram passive spool filename mismatch"
                        )
                except Exception:
                    logger.critical(
                        "Telegram passive spool contains an unreadable event; "
                        "the file is retained and later events will continue",
                        exc_info=True,
                    )
                    continue
                events.append(event)
            return events

    def acknowledge(self, event: Mapping[str, Any]) -> bool:
        """Delete a committed event only when id and checksum still match."""
        update_id, digest = self._validate_event(event)
        with self._lock:
            target = self.root / f"update-{update_id}.json"
            if not target.exists():
                return False
            existing = self._read_json_file(target)
            _existing_id, existing_digest = self._validate_event(existing)
            if existing_digest != digest:
                raise RuntimeError("Refusing to acknowledge a different event")
            target.unlink()
            self._fsync_directory(self.root)
            return True


class BusinessConnectionRegistry:
    """Durable allowlisted Business connection-to-owner mapping."""

    def __init__(self, path: Path, *, hermes_home: Path):
        home = hermes_home.resolve()
        candidate = path if path.is_absolute() else home / path
        resolved_parent = candidate.parent.resolve()
        try:
            resolved_parent.relative_to(home)
        except ValueError as exc:
            raise RuntimeError(
                "Telegram Business registry must stay inside HERMES_HOME"
            ) from exc
        if candidate.is_symlink() or candidate.parent.is_symlink():
            raise RuntimeError("Refusing symlinked Telegram Business registry")
        _ensure_durable_private_directory(resolved_parent)
        self.path = resolved_parent / candidate.name
        self._process_lock_path = resolved_parent / f".{candidate.name}.lock"
        self._lock = threading.RLock()
        # A lifecycle update may fail after Telegram has already delivered it.
        # Keep that connection denied in this process until an equal/newer
        # authoritative state is durably committed.  The value is the highest
        # update_id whose safe state may be missing from disk.
        self._untrusted_after: dict[str, int] = {}
        self._all_untrusted = False

    @contextmanager
    def _process_lock(self, *, exclusive: bool) -> Iterator[None]:
        """Serialize registry snapshots across overlapping gateway processes."""
        if fcntl is None:
            raise RuntimeError(
                "Telegram Business registry requires POSIX file locking"
            )
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        created = False
        try:
            fd = os.open(self._process_lock_path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
        except FileExistsError:
            fd = os.open(self._process_lock_path, flags)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) & 0o077
            ):
                raise RuntimeError("Telegram Business registry lock is unsafe")
            if created:
                os.fsync(fd)
                DurableUpdateSpool._fsync_directory(self.path.parent)
            operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            fcntl.flock(fd, operation)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        raw = DurableUpdateSpool._read_json_file(self.path)
        connections = raw.get("connections")
        if not isinstance(connections, dict):
            raise ValueError("Invalid Telegram Business registry")
        result: dict[str, dict[str, Any]] = {}
        for connection_id, raw_record in connections.items():
            if not isinstance(connection_id, str) or not connection_id:
                continue
            # Backward-compatible read of the original owner-only registry.
            if not isinstance(raw_record, dict):
                owner_id = _int_or_none(raw_record)
                if owner_id and owner_id > 0:
                    result[connection_id] = {
                        "owner_id": owner_id,
                        # Legacy registries did not record rights or ordering.
                        # Fail closed and force a Bot API recovery before any
                        # message payload can be accepted.
                        "enabled": False,
                        "last_update_id": -1,
                    }
                continue
            owner_id = _int_or_none(raw_record.get("owner_id"))
            last_update_id = _int_or_none(raw_record.get("last_update_id"))
            enabled = raw_record.get("enabled")
            if (
                owner_id
                and owner_id > 0
                and last_update_id is not None
                and last_update_id >= 0
                and isinstance(enabled, bool)
            ):
                result[connection_id] = {
                    "owner_id": owner_id,
                    "enabled": enabled,
                    "last_update_id": last_update_id,
                }
        return result

    def _write_locked(self, connections: Mapping[str, Mapping[str, Any]]) -> None:
        """Atomically persist a registry snapshot while the process lock is held."""
        payload = {
            "schema_version": SCHEMA_VERSION,
            "connections": connections,
        }
        fd, temp_name = tempfile.mkstemp(prefix=".connections-", dir=self.path.parent)
        temp = Path(temp_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = -1
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
            DurableUpdateSpool._fsync_directory(self.path.parent)
        finally:
            if fd >= 0:
                os.close(fd)
            if temp.exists():
                temp.unlink()
                DurableUpdateSpool._fsync_directory(self.path.parent)

    def invalidate_enabled(self) -> int:
        """Force live rights recovery after every passive gateway start.

        ``blocked`` mode still advances Telegram's update offset while ignoring
        Business lifecycle events.  A connection that used to be receive-only
        may therefore have gained action rights while capture was blocked.  Do
        not trust that stale capability snapshot after activation/restart.
        """
        with self._lock:
            with self._process_lock(exclusive=True):
                connections = self._load()
                changed = 0
                for connection_id, record in list(connections.items()):
                    if record.get("enabled") is True:
                        connections[connection_id] = {**record, "enabled": False}
                        changed += 1
                if changed:
                    self._write_locked(connections)
                self._untrusted_after.clear()
                self._all_untrusted = False
                return changed

    def mark_existing_untrusted(self, connection_id: str, update_id: int) -> bool:
        """Deny a known mapping before parsing a lifecycle event's new owner/rights."""
        if not connection_id or update_id < 0:
            return False
        with self._lock:
            try:
                with self._process_lock(exclusive=False):
                    known = connection_id in self._load()
            except Exception:
                # If registry identity cannot be established, no cached mapping
                # in this process is safe to use until a successful startup
                # invalidation rebuilds the boundary.
                self._all_untrusted = True
                raise
            if known:
                self._untrusted_after[connection_id] = max(
                    self._untrusted_after.get(connection_id, -1), update_id
                )
            return known

    def recover_current(
        self,
        connection_id: str,
        owner_id: int,
        *,
        enabled: bool,
        observed_update_id: int,
    ) -> bool:
        """Commit a live getBusinessConnection result without lowering order floor."""
        if not connection_id or owner_id <= 0 or observed_update_id < 0:
            raise ValueError("Invalid Telegram Business connection identity")
        with self._lock:
            poison_floor = self._untrusted_after.get(connection_id, -1)
            if observed_update_id < poison_floor:
                return False
            self._untrusted_after[connection_id] = max(
                poison_floor, observed_update_id
            )
            with self._process_lock(exclusive=True):
                connections = self._load()
                current = connections.get(connection_id)
                floor = int(current["last_update_id"]) if current else -1
                if current and (
                    int(current["owner_id"]) != owner_id
                    or floor > observed_update_id
                ):
                    # A newer lifecycle update won the race while the async
                    # getBusinessConnection call was in flight.  Its disabled
                    # or rights-rejected state is authoritative; never revive
                    # it from an older API response.
                    return False
                connections[connection_id] = {
                    "owner_id": owner_id,
                    "enabled": bool(enabled),
                    "last_update_id": max(floor, observed_update_id),
                }
                self._write_locked(connections)
                self._untrusted_after.pop(connection_id, None)
                return bool(enabled)

    def owner_for(self, connection_id: str) -> int | None:
        with self._lock:
            if self._all_untrusted or connection_id in self._untrusted_after:
                return None
            with self._process_lock(exclusive=False):
                record = self._load().get(connection_id)
                if not record or record.get("enabled") is not True:
                    return None
                return int(record["owner_id"])

    def update(
        self,
        connection_id: str,
        owner_id: int,
        *,
        enabled: bool,
        update_id: int,
    ) -> bool:
        if not connection_id or owner_id <= 0 or update_id < 0:
            raise ValueError("Invalid Telegram Business connection identity")
        with self._lock:
            poison_floor = self._untrusted_after.get(connection_id, -1)
            if update_id < poison_floor:
                return False
            self._untrusted_after[connection_id] = max(poison_floor, update_id)
            with self._process_lock(exclusive=True):
                connections = self._load()
                current = connections.get(connection_id)
                if current and int(current["last_update_id"]) >= update_id:
                    if int(current["last_update_id"]) >= self._untrusted_after.get(
                        connection_id, -1
                    ):
                        self._untrusted_after.pop(connection_id, None)
                    return False
                connections[connection_id] = {
                    "owner_id": owner_id,
                    "enabled": bool(enabled),
                    "last_update_id": update_id,
                }
                self._write_locked(connections)
                self._untrusted_after.pop(connection_id, None)
                return True


class PassiveGroupRegistry:
    """Durable owner consent for passive Telegram group capture.

    Callback payloads contain only a random nonce.  Raw group identifiers and
    approval state stay in this private, fsync-backed registry.  Every
    add/re-add transition replaces an earlier approval with a fresh pending
    decision, so stale consent cannot silently survive a new membership.
    """

    def __init__(self, path: Path, *, hermes_home: Path):
        home = hermes_home.resolve()
        candidate = path if path.is_absolute() else home / path
        resolved_parent = candidate.parent.resolve()
        try:
            resolved_parent.relative_to(home)
        except ValueError as exc:
            raise RuntimeError(
                "Telegram passive group registry must stay inside HERMES_HOME"
            ) from exc
        if candidate.is_symlink() or candidate.parent.is_symlink():
            raise RuntimeError("Refusing symlinked Telegram passive group registry")
        _ensure_durable_private_directory(resolved_parent)
        self.path = resolved_parent / candidate.name
        self._process_lock_path = resolved_parent / f".{candidate.name}.lock"
        self._lock = threading.RLock()

    @contextmanager
    def _process_lock(self, *, exclusive: bool) -> Iterator[None]:
        if fcntl is None:
            raise RuntimeError(
                "Telegram passive group registry requires POSIX file locking"
            )
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        created = False
        try:
            fd = os.open(self._process_lock_path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
        except FileExistsError:
            fd = os.open(self._process_lock_path, flags)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) & 0o077
            ):
                raise RuntimeError("Telegram passive group registry lock is unsafe")
            if created:
                os.fsync(fd)
                DurableUpdateSpool._fsync_directory(self.path.parent)
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        raw = DurableUpdateSpool._read_json_file(self.path)
        groups = raw.get("groups")
        if not isinstance(groups, dict):
            raise ValueError("Invalid Telegram passive group registry")
        result: dict[str, dict[str, Any]] = {}
        for raw_chat_id, record in groups.items():
            try:
                chat_id = int(raw_chat_id)
            except (TypeError, ValueError, OverflowError):
                continue
            if chat_id >= 0 or not isinstance(record, dict):
                continue
            owner_id = _int_or_none(record.get("owner_id"))
            state = record.get("state")
            if not owner_id or owner_id <= 0 or state not in {
                "pending", "approved", "denied"
            }:
                continue
            normalized = {
                "owner_id": owner_id,
                "state": state,
                "title": _str_or_none(record.get("title"), max_chars=256) or "Группа Telegram",
            }
            username = _str_or_none(record.get("username"), max_chars=256)
            if username:
                normalized["username"] = username
            if state == "pending":
                nonce_sha256 = record.get("nonce_sha256")
                expires_at = record.get("expires_at")
                if (
                    not isinstance(nonce_sha256, str)
                    or len(nonce_sha256) != 64
                    or isinstance(expires_at, bool)
                    or not isinstance(expires_at, (int, float))
                ):
                    continue
                normalized["nonce_sha256"] = nonce_sha256
                normalized["expires_at"] = float(expires_at)
            result[str(chat_id)] = normalized
        return result

    def _write_locked(self, groups: Mapping[str, Mapping[str, Any]]) -> None:
        payload = {"schema_version": SCHEMA_VERSION, "groups": groups}
        fd, temp_name = tempfile.mkstemp(prefix=".groups-", dir=self.path.parent)
        temp = Path(temp_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = -1
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
            DurableUpdateSpool._fsync_directory(self.path.parent)
        finally:
            if fd >= 0:
                os.close(fd)
            if temp.exists():
                temp.unlink()
                DurableUpdateSpool._fsync_directory(self.path.parent)

    @staticmethod
    def _nonce_digest(nonce: str) -> str:
        return hashlib.sha256(nonce.encode("utf-8")).hexdigest()

    @staticmethod
    def _prune_expired(groups: dict[str, dict[str, Any]], now: float) -> None:
        for chat_id, record in list(groups.items()):
            if record.get("state") == "pending" and float(record.get("expires_at", 0)) <= now:
                groups[chat_id] = {
                    key: value
                    for key, value in record.items()
                    if key not in {"nonce_sha256", "expires_at"}
                }
                groups[chat_id]["state"] = "denied"

    def begin(
        self,
        *,
        chat_id: int,
        owner_id: int,
        title: str,
        username: str | None = None,
        ttl_seconds: int = 600,
        now: float | None = None,
    ) -> str:
        if chat_id >= 0 or owner_id <= 0:
            raise ValueError("Invalid Telegram passive group identity")
        if not isinstance(title, str) or not title.strip():
            title = "Группа Telegram"
        if not 60 <= int(ttl_seconds) <= 3600:
            raise ValueError("Invalid Telegram passive group consent TTL")
        current_time = time.time() if now is None else float(now)
        nonce = secrets.token_urlsafe(18)
        record: dict[str, Any] = {
            "owner_id": owner_id,
            "state": "pending",
            "title": title.strip()[:256],
            "nonce_sha256": self._nonce_digest(nonce),
            "expires_at": current_time + int(ttl_seconds),
        }
        if isinstance(username, str) and username.strip():
            record["username"] = username.strip().lstrip("@")[:256]
        with self._lock:
            with self._process_lock(exclusive=True):
                groups = self._load()
                self._prune_expired(groups, current_time)
                if str(chat_id) not in groups and len(groups) >= MAX_PASSIVE_GROUP_RECORDS:
                    raise RuntimeError("Telegram passive group registry is full")
                groups[str(chat_id)] = record
                self._write_locked(groups)
        return nonce

    def resolve(
        self,
        *,
        nonce: str,
        owner_id: int,
        approve: bool,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        if not isinstance(nonce, str) or not nonce or owner_id <= 0:
            return None
        current_time = time.time() if now is None else float(now)
        candidate_digest = self._nonce_digest(nonce)
        with self._lock:
            with self._process_lock(exclusive=True):
                groups = self._load()
                before = dict(groups)
                self._prune_expired(groups, current_time)
                matched_chat_id: str | None = None
                matched: dict[str, Any] | None = None
                for chat_id, record in groups.items():
                    if (
                        record.get("state") == "pending"
                        and int(record.get("owner_id", 0)) == owner_id
                        and secrets.compare_digest(
                            str(record.get("nonce_sha256", "")), candidate_digest
                        )
                    ):
                        matched_chat_id = chat_id
                        matched = record
                        break
                if matched_chat_id is None or matched is None:
                    # Persist expiry pruning even when the supplied nonce is
                    # invalid, without revealing whether a group existed.
                    if groups != before:
                        self._write_locked(groups)
                    return None
                result = {**matched, "chat_id": int(matched_chat_id)}
                if approve:
                    groups[matched_chat_id] = {
                        key: value
                        for key, value in matched.items()
                        if key not in {"nonce_sha256", "expires_at"}
                    }
                    groups[matched_chat_id]["state"] = "approved"
                else:
                    groups[matched_chat_id] = {
                        key: value
                        for key, value in matched.items()
                        if key not in {"nonce_sha256", "expires_at"}
                    }
                    groups[matched_chat_id]["state"] = "denied"
                self._write_locked(groups)
                return result

    def approved_ids(self, *, owner_id: int) -> set[int]:
        with self._lock:
            with self._process_lock(exclusive=True):
                groups = self._load()
                before = len(groups)
                self._prune_expired(groups, time.time())
                if len(groups) != before:
                    self._write_locked(groups)
                return {
                    int(chat_id)
                    for chat_id, record in groups.items()
                    if record.get("state") == "approved"
                    and int(record.get("owner_id", 0)) == owner_id
                }

    def blocked_ids(self, *, owner_id: int) -> set[int]:
        """Return pending/denied ids that must shadow legacy static allowlists."""
        with self._lock:
            with self._process_lock(exclusive=True):
                groups = self._load()
                before = len(groups)
                self._prune_expired(groups, time.time())
                if len(groups) != before:
                    self._write_locked(groups)
                return {
                    int(chat_id)
                    for chat_id, record in groups.items()
                    if record.get("state") in {"pending", "denied"}
                    and int(record.get("owner_id", 0)) == owner_id
                }

    def revoke(self, *, chat_id: int, owner_id: int) -> bool:
        if chat_id >= 0 or owner_id <= 0:
            return False
        with self._lock:
            with self._process_lock(exclusive=True):
                groups = self._load()
                record = groups.get(str(chat_id))
                if record and int(record.get("owner_id", 0)) != owner_id:
                    return False
                groups[str(chat_id)] = ({
                    key: value
                    for key, value in (record or {}).items()
                    if key not in {"nonce_sha256", "expires_at"}
                } or {"owner_id": owner_id, "title": "Группа Telegram"})
                groups[str(chat_id)]["state"] = "denied"
                self._write_locked(groups)
                return True
