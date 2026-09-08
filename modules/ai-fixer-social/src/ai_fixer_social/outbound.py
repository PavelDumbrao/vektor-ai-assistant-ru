from __future__ import annotations

import fcntl
import hashlib
import json
import re
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from .db import StateStore, utc_now
from .post_validation import validate_post, visible_text
from .rich_post import prepare_article
from .telegram_content import TelegramRejected, prepare_spec


class OutboundBusy(RuntimeError):
    pass


@contextmanager
def outbound_lock(store: StateStore):
    """One non-blocking send boundary shared by poller, Codex and Hermes."""
    with store.path.with_suffix(".outbound.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise OutboundBusy("another_outbound_action_in_progress") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def get_attempt(store: StateStore, action_id: str) -> dict | None:
    row = store.conn.execute("SELECT * FROM outbound_attempts WHERE action_id=?", (action_id,)).fetchone()
    return dict(row) if row else None


def reserve_attempt(store: StateStore, action_id: str, digest: str) -> None:
    store.conn.execute(
        "INSERT INTO outbound_attempts(action_id,payload_hash,status,created_at) VALUES(?,?,?,?)",
        (action_id, digest, "sending", utc_now().isoformat()),
    )
    store.conn.commit()


def finish_attempt(store: StateStore, action_id: str, status: str, result: dict) -> None:
    store.conn.execute(
        "UPDATE outbound_attempts SET status=?, result_json=? WHERE action_id=?",
        (status, json.dumps(result, ensure_ascii=False), action_id),
    )
    store.conn.commit()


def photo_digest(photo: Path | None) -> str:
    if photo is None:
        return ""
    with photo.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


async def publish_post(settings, store, telegram, *, request_id: str, text: str,
                       photo: Path | None = None, dry_run: bool = False,
                       confirm_hash: str | None = None, article: dict | None = None,
                       telegram_spec: dict | None = None) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{3,119}", request_id):
        return {"ok": False, "error": "invalid_request_id"}
    rich = None
    prepared = None
    if telegram_spec is not None:
        if text or photo is not None or article is not None:
            return {"ok": False, "error": "mixed_post_formats"}
        try:
            prepared = prepare_spec(telegram_spec)
            if not prepared.creates_message:
                raise ValueError("use_tg_manage_for_existing_messages")
        except ValueError as exc:
            return {"ok": False, "stage": "validation", "errors": [str(exc)]}
    if article is not None:
        if text or photo is not None:
            return {"ok": False, "error": "mixed_post_formats"}
        try:
            rich = prepare_article(article)
        except ValueError as exc:
            return {"ok": False, "stage": "validation", "errors": [str(exc)]}
    errors = [] if rich or prepared else validate_post(text, has_photo=photo is not None)
    if photo and (not photo.is_file() or photo.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}):
        errors.append("invalid_photo")
    if errors:
        return {"ok": False, "stage": "validation", "errors": errors}
    digest = prepared.payload_hash if prepared else rich.payload_hash if rich else StateStore.payload_hash(text, photo_digest(photo))
    if dry_run:
        if prepared:
            return {"ok": True, "dry_run": True, **prepared.summary()}
        if rich:
            return {"ok": True, "dry_run": True, **rich.summary()}
        return {"ok": True, "dry_run": True, "visible_chars": len(visible_text(text)),
                "has_photo": photo is not None, "payload_hash": digest}
    if confirm_hash is not None and confirm_hash != digest:
        return {"ok": False, "error": "confirmation_payload_mismatch"}
    try:
        with outbound_lock(store):
            existing = store.get_publication(request_id)
            if existing:
                if existing["payload_hash"] != digest:
                    return {"ok": False, "error": "request_id_payload_mismatch"}
                return {"ok": True, "deduplicated": True, "message_id": existing["message_id"],
                        "public_url": existing["public_url"], "delivery": "api_accepted"}
            action_id = f"post:{request_id}"
            previous = get_attempt(store, action_id)
            if previous:
                if previous["status"] == "rejected":
                    return {"ok": False, "error": "previous_request_rejected", "retry_allowed": False, "readback_required": False}
                return {"ok": False, "error": "previous_attempt_requires_readback", "status": previous["status"]}
            duplicate = store.conn.execute(
                "SELECT message_id,public_url FROM publications WHERE payload_hash=?", (digest,)
            ).fetchone()
            if duplicate:
                return {"ok": True, "deduplicated": True, "message_id": duplicate["message_id"],
                        "public_url": duplicate["public_url"], "delivery": "api_accepted"}
            unresolved = store.conn.execute(
                "SELECT 1 FROM outbound_attempts WHERE action_id LIKE 'post:%' AND status IN ('sending','uncertain')"
            ).fetchone()
            if unresolved:
                return {"ok": False, "error": "unresolved_publication_requires_readback"}
            if store.conn.execute(
                "SELECT 1 FROM editor_drafts WHERE status IN ('queued','sending') "
                "AND ('draft-' || draft_id)!=? AND ABS(scheduled_at-?)<21600 LIMIT 1",
                (request_id, utc_now().timestamp()),
            ).fetchone():
                return {"ok": False, "error": "approved_queue_reserves_slot", "sent": False}
            since = (utc_now() - timedelta(hours=6)).isoformat()
            recent = store.conn.execute(
                "SELECT 1 FROM publications WHERE created_at>=? UNION ALL "
                "SELECT 1 FROM thread_roots WHERE channel_post_id IS NOT NULL AND created_at>=? LIMIT 1",
                (since, since),
            ).fetchone()
            if recent:
                return {"ok": False, "error": "channel_six_hour_cooldown"}
            reserve_attempt(store, action_id, digest)
            try:
                if prepared:
                    batch = await telegram.send_spec(chat_id=settings.telegram_channel_id, spec=prepared)
                    sent = batch["messages"][0]
                elif rich:
                    sent = await telegram.send_rich_post(chat_id=settings.telegram_channel_id, article=rich)
                elif photo is None:
                    sent = await telegram.send_text(chat_id=settings.telegram_channel_id, text=text)
                else:
                    sent = await telegram.send_photo(chat_id=settings.telegram_channel_id, photo=photo, caption=text)
                message_id = int(sent["message_id"])
                for native_message in batch["messages"] if prepared else [sent]:
                    kind = prepared.kind if prepared else "rich" if rich else "photo" if photo else "text"
                    if kind == "album":
                        kind = "video" if "video" in native_message else "photo"
                    store.record_telegram_object(settings.telegram_channel_id, native_message, kind)
                public_url = f"https://t.me/{settings.telegram_channel_username}/{message_id}"
                store.record_publication(request_id=request_id, channel_id=settings.telegram_channel_id,
                                         message_id=message_id, public_url=public_url, payload_hash=digest)
                result = {"ok": True, "deduplicated": False, "message_id": message_id,
                          "public_url": public_url, "delivery": "api_accepted", "readback_required": True}
                if prepared:
                    result.update(kind=prepared.kind, message_ids=batch["message_ids"])
                finish_attempt(store, action_id, "sent", result)
                return result
            except Exception as exc:
                if isinstance(exc, TelegramRejected):
                    result = {"ok": False, "error": "telegram_request_rejected", "telegram_error_code": exc.code,
                              "readback_required": False, "retry_allowed": False}
                    finish_attempt(store, action_id, "rejected", result)
                    return result
                result = {"ok": False, "error": "send_result_uncertain", "error_type": type(exc).__name__,
                          "readback_required": True, "retry_allowed": False}
                finish_attempt(store, action_id, "uncertain", result)
                return result
    except OutboundBusy:
        return {"ok": False, "error": "outbound_busy", "sent": False}


def resolve_management(settings, store, data):
    prepared = prepare_spec(data)
    if prepared.creates_message:
        raise ValueError("use_tg_publish_for_new_messages")
    obj = store.telegram_object(settings.telegram_channel_id, prepared.params["message_id"])
    if obj is None:
        raise ValueError("message_not_owned_or_not_recorded_by_editor")
    kind = obj["kind"]
    if prepared.kind == "stop_poll" and kind not in {"poll", "quiz"}:
        raise ValueError("target_is_not_our_poll")
    if prepared.kind == "edit_text" and kind != "text":
        raise ValueError("edit_text_requires_text_post")
    if prepared.kind == "edit_article" and kind != "rich":
        raise ValueError("edit_article_requires_rich_post")
    if prepared.kind in {"edit_caption", "edit_media"} and kind not in {"photo", "video", "animation", "audio", "voice", "document"}:
        raise ValueError("target_has_no_editable_media")
    old = obj["message"]
    if (prepared.kind.startswith("edit_") or prepared.kind == "stop_poll") and "buttons" not in prepared.provided and old.get("reply_markup"):
        prepared.params["reply_markup"] = old["reply_markup"]
    if prepared.kind == "edit_media" and "caption" not in prepared.provided:
        media = prepared.params["media"]
        media["caption"] = old.get("caption", "")
        if old.get("caption_entities"):
            media["caption_entities"] = old["caption_entities"]
    return prepared, obj


async def manage_message(settings, store, telegram, *, request_id, data, confirm_hash=None, dry_run=False):
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{3,119}", request_id):
        return {"ok": False, "error": "invalid_request_id"}
    try:
        prepared, obj = resolve_management(settings, store, data)
    except ValueError as exc:
        return {"ok": False, "stage": "validation", "errors": [str(exc)]}
    if dry_run:
        return {"ok": True, "dry_run": True, **prepared.summary(),
                "target_url": f"https://t.me/{settings.telegram_channel_username}/{obj['message_id']}"}
    if confirm_hash != prepared.payload_hash:
        return {"ok": False, "error": "confirmation_payload_mismatch"}
    action_id = f"manage:{request_id}"
    try:
        with outbound_lock(store):
            # Resolve again under the lock: an intervening edit must invalidate approval.
            prepared, obj = resolve_management(settings, store, data)
            if prepared.payload_hash != confirm_hash:
                return {"ok": False, "error": "confirmation_payload_mismatch"}
            previous = get_attempt(store, action_id)
            if previous:
                if previous["payload_hash"] != prepared.payload_hash:
                    return {"ok": False, "error": "request_id_payload_mismatch"}
                if previous["status"] == "sent":
                    return {**json.loads(previous["result_json"]), "deduplicated": True}
                if previous["status"] == "rejected":
                    return {"ok": False, "error": "previous_request_rejected", "retry_allowed": False, "readback_required": False}
                return {"ok": False, "error": "previous_management_requires_readback", "retry_allowed": False}
            unresolved = store.conn.execute("SELECT 1 FROM outbound_attempts WHERE action_id LIKE 'manage:%' AND status IN ('sending','uncertain')").fetchone()
            if unresolved:
                return {"ok": False, "error": "unresolved_management_requires_readback", "retry_allowed": False}
            since = (utc_now() - timedelta(hours=1)).isoformat()
            if store.conn.execute("SELECT COUNT(*) FROM outbound_attempts WHERE action_id LIKE 'manage:%' AND created_at>=?", (since,)).fetchone()[0] >= 20:
                return {"ok": False, "error": "management_hourly_limit"}
            reserve_attempt(store, action_id, prepared.payload_hash)
            store.remember_revision(action_id, obj)
            try:
                if prepared.kind == "stop_poll" and obj["message"].get("poll", {}).get("is_closed") is True:
                    delivery = {"messages": [], "message_ids": [obj["message_id"]], "no_change": True}
                else:
                    delivery = await telegram.send_spec(chat_id=settings.telegram_channel_id, spec=prepared)
                for native_message in delivery.get("messages", []):
                    kind = data["media_type"] if prepared.kind == "edit_media" else obj["kind"]
                    store.record_telegram_object(settings.telegram_channel_id, native_message, kind)
                if delivery.get("poll"):
                    if delivery["poll"].get("id") != obj["message"].get("poll", {}).get("id"):
                        raise RuntimeError("poll_id_response_mismatch")
                    store.update_poll(delivery["poll"])
                result = {"ok": True, "deduplicated": False, "kind": prepared.kind,
                          "message_id": obj["message_id"], "readback_required": True,
                          "no_change": delivery.get("no_change", False)}
                finish_attempt(store, action_id, "sent", result)
                return result
            except Exception as exc:
                if isinstance(exc, TelegramRejected):
                    result = {"ok": False, "error": "telegram_request_rejected", "telegram_error_code": exc.code,
                              "readback_required": False, "retry_allowed": False}
                    finish_attempt(store, action_id, "rejected", result)
                    return result
                result = {"ok": False, "error": "management_result_uncertain", "error_type": type(exc).__name__,
                          "retry_allowed": False, "readback_required": True}
                finish_attempt(store, action_id, "uncertain", result)
                return result
    except OutboundBusy:
        return {"ok": False, "error": "outbound_busy", "sent": False}
