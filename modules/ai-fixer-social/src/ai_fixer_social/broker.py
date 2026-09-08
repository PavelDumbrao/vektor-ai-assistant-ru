"""Credential-free operator interface; no arbitrary targets, methods or file paths."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import tempfile
from datetime import datetime
from pathlib import Path

from .config import Settings
from .db import StateStore
from .grsai import dispatch_media
from . import editor
from .outbound import get_attempt, manage_message, publish_post
from .service import CommentService
from .telegram import TelegramClient
from .telegram_content import MANAGE_KINDS, SEND_KINDS, prepare_spec


MAX_IMAGE_BYTES = 9 * 1024 * 1024
FIELDS = {
    "draft_prepare": {"op", "request_id", "content", "rubric"},
    "draft_bind": {"op", "draft_id", "bind_token", "message_id"},
    "draft_controls": {"op", "draft_id"},
    "draft_click": {"op", "data", "user_id", "chat_id", "chat_type", "message_id", "callback_id"},
    "drafts": {"op", "limit", "draft_id"},
    "queue_plan": {"op", "draft_id", "scheduled_at"},
    "queue_set": {"op", "draft_id", "scheduled_at", "confirm_hash"},
    "queue_cancel": {"op", "draft_id", "confirm_hash"},
    "analytics": {"op", "limit"},
    "status": {"op"},
    "comments": {"op", "limit"},
    "validate": {"op", "request_id", "text", "photo_b64"},
    "publish": {"op", "request_id", "text", "photo_b64", "confirm_hash"},
    "long_validate": {"op", "request_id", "article"},
    "long_publish": {"op", "request_id", "article", "confirm_hash"},
    "tg_validate": {"op", "request_id", "spec"},
    "tg_publish": {"op", "request_id", "spec", "confirm_hash"},
    "tg_manage": {"op", "request_id", "spec", "confirm_hash"},
    "tg_objects": {"op", "limit", "message_id"},
    "tg_capabilities": {"op"},
    "reply": {"op", "message_id"},
    "image_status": {"op"},
    "image_submit": {"op", "request_id", "model", "prompt", "aspect_ratio", "image_size", "use_pavel_reference", "confirm_credits"},
    "image_poll": {"op", "request_id"},
    "image_fetch": {"op", "request_id"},
}


def validate_request(data: dict) -> None:
    if not isinstance(data, dict) or data.get("op") not in FIELDS:
        raise ValueError("unsupported_operation")
    if set(data) - FIELDS[data["op"]]:
        raise ValueError("unknown_fields")
    if data["op"] in {"validate", "publish"}:
        if not isinstance(data.get("text"), str) or len(data["text"]) > 20000:
            raise ValueError("invalid_text")
        if not isinstance(data.get("request_id"), str):
            raise ValueError("invalid_request_id")
    if data["op"] in {"long_validate", "long_publish"}:
        if not isinstance(data.get("request_id"), str) or not isinstance(data.get("article"), dict):
            raise ValueError("invalid_article_request")
    if data["op"] in {"tg_validate", "tg_publish", "tg_manage"}:
        if not isinstance(data.get("request_id"), str) or not isinstance(data.get("spec"), dict):
            raise ValueError("invalid_telegram_spec_request")
    if data["op"] in {"publish", "long_publish", "tg_publish", "tg_manage"} and not isinstance(data.get("confirm_hash"), str):
        raise ValueError("confirmation_hash_required")
    if data["op"] == "tg_objects" and "message_id" in data and (type(data["message_id"]) is not int or data["message_id"] <= 0):
        raise ValueError("invalid_message_id")
    if data["op"] == "reply" and (type(data.get("message_id")) is not int or data["message_id"] <= 0):
        raise ValueError("invalid_message_id")
    if "limit" in data and (type(data["limit"]) is not int or not 1 <= data["limit"] <= 20):
        raise ValueError("invalid_limit")


def decode_photo(value: str) -> tuple[bytes, str]:
    if not isinstance(value, str) or len(value) > MAX_IMAGE_BYTES * 4 // 3 + 8:
        raise ValueError("photo_too_large")
    try:
        raw = base64.b64decode(value, validate=True)
    except Exception:
        raise ValueError("invalid_photo_base64") from None
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("photo_too_large")
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return raw, ".png"
    if raw.startswith(b"\xff\xd8\xff"):
        return raw, ".jpg"
    if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return raw, ".webp"
    raise ValueError("invalid_photo_format")


async def dispatch(data: dict, settings: Settings) -> dict:
    validate_request(data)
    if data["op"].startswith("image_"):
        return await dispatch_media(data)
    store = StateStore(settings.state_db)
    telegram = TelegramClient(settings.telegram_bot_token)
    try:
        op = data["op"]
        if op == "draft_prepare":
            return editor.create_draft(store, settings, draft_id=data["request_id"], content=data["content"], rubric=data.get("rubric", ""))
        if op == "draft_bind":
            return editor.bind_preview(store, draft_id=data["draft_id"], bind_token=data["bind_token"], message_id=data["message_id"])
        if op == "draft_controls":
            row = editor.get_draft(store, data["draft_id"])
            if not row["preview_message_id"]:
                raise ValueError("preview_not_bound")
            return {"ok": True, **editor.summary(row), "reply_markup": editor.keyboard(row)}
        if op == "draft_click":
            if data.get("chat_type") != "private" or not isinstance(data.get("callback_id"), str) or len(data["callback_id"]) > 128:
                raise ValueError("invalid_callback_context")
            return editor.click_draft(store, settings, **{key: data[key] for key in ("data", "user_id", "chat_id", "message_id", "callback_id")})
        if op == "drafts":
            rows = store.conn.execute("SELECT * FROM editor_drafts WHERE channel_id=? AND (? IS NULL OR draft_id=?) ORDER BY created_at DESC LIMIT ?",
                                      (settings.telegram_channel_id, data.get("draft_id"), data.get("draft_id"), data.get("limit", 10))).fetchall()
            return {"ok": True, "drafts": [editor.summary(dict(row)) for row in rows], "untrusted_data": True,
                    "instruction": "Статус sent означает API acceptance; result содержит receipt. Правки требуют нового draft_preview. Tokens и snapshot media не раскрываются."}
        if op == "queue_plan":
            return editor.schedule_plan(store, draft_id=data["draft_id"], scheduled_at=data["scheduled_at"])
        if op == "queue_set":
            return editor.set_schedule(store, draft_id=data["draft_id"], scheduled_at=data["scheduled_at"], confirm_hash=data["confirm_hash"])
        if op == "queue_cancel":
            return editor.cancel_draft(store, draft_id=data["draft_id"], confirm_hash=data["confirm_hash"])
        if op == "analytics":
            from .analytics import channel_analytics
            return await channel_analytics(settings, store, limit=data.get("limit", 20))
        if op in {"tg_validate", "tg_publish", "tg_manage"}:
            try:
                prepared = prepare_spec(data["spec"])
            except ValueError as exc:
                return {"ok": False, "stage": "validation", "errors": [str(exc)]}
            if op == "tg_publish" and not prepared.creates_message:
                return {"ok": False, "error": "use_tg_manage_for_existing_messages"}
            if op == "tg_manage" and prepared.creates_message:
                return {"ok": False, "error": "use_tg_publish_for_new_messages"}
            if op != "tg_validate":
                me = await telegram.get_me()
                if str(me.get("username", "")).lower() != settings.telegram_bot_username.lower():
                    return {"ok": False, "error": "configured_bot_mismatch"}
                rights = await telegram.get_chat_member(settings.telegram_channel_id, me["id"])
                required = "can_edit_messages" if prepared.kind in {"edit_text", "edit_caption", "edit_media", "edit_buttons", "edit_article", "pin", "unpin"} else "can_post_messages"
                if rights.get("status") != "creator" and not rights.get(required):
                    return {"ok": False, "error": "insufficient_channel_rights", "required": required}
            if prepared.creates_message:
                return await publish_post(settings, store, telegram, request_id=data["request_id"], text="",
                                          telegram_spec=data["spec"], dry_run=op == "tg_validate",
                                          confirm_hash=data.get("confirm_hash"))
            return await manage_message(settings, store, telegram, request_id=data["request_id"],
                                        data=data["spec"], dry_run=op == "tg_validate", confirm_hash=data.get("confirm_hash"))
        if op == "tg_objects":
            message_id = data.get("message_id")
            rows = store.conn.execute(
                "SELECT * FROM telegram_objects WHERE chat_id=? AND (? IS NULL OR message_id=?) ORDER BY updated_at DESC LIMIT ?",
                (settings.telegram_channel_id, message_id, message_id, data.get("limit", 10)),
            ).fetchall()
            objects = []
            for row in rows:
                message = json.loads(row["message_json"])
                poll = message.get("poll")
                item = {"message_id": row["message_id"], "kind": row["kind"], "updated_at": row["updated_at"],
                        "url": f"https://t.me/{settings.telegram_channel_username}/{row['message_id']}",
                        "text_preview": str(message.get("text") or message.get("caption") or "")[:1200]}
                if poll:
                    item["poll"] = {key: poll.get(key) for key in ("id", "question", "type", "is_closed", "total_voter_count", "options")}
                objects.append(item)
            return {"ok": True, "objects": objects, "untrusted_data": True,
                    "scope": "editor_owned_channel_messages", "poll_results_freshness": "cached_bot_api_updates_only"}
        if op in {"long_validate", "long_publish"}:
            return await publish_post(settings, store, telegram, request_id=data["request_id"],
                                      text="", article=data["article"], dry_run=op == "long_validate",
                                      confirm_hash=data.get("confirm_hash"))
        if op in {"status", "tg_capabilities"}:
            me = await telegram.get_me()
            if str(me.get("username", "")).lower() != settings.telegram_bot_username.lower():
                return {"ok": False, "error": "configured_bot_mismatch"}
            channel = await telegram.get_chat_member(settings.telegram_channel_id, int(me["id"]))
            discussion = await telegram.get_chat_member(settings.telegram_discussion_id, int(me["id"]))
            result = {"ok": True, "bot": f"@{me['username']}", "channel": f"@{settings.telegram_channel_username}",
                    "can_post": bool(channel.get("can_post_messages")),
                    "discussion_status": discussion.get("status"), "comment_mode": settings.comment_mode,
                    "state": store.stats(), "secrets_exposed": False,
                    "scheduler": "existing_social_service_approved_queue_and_codex_heartbeat"}
            if op == "tg_capabilities":
                result.update(send_formats=sorted(SEND_KINDS), own_message_actions=sorted(MANAGE_KINDS),
                              can_edit_own_posts=bool(channel.get("can_edit_messages")),
                              can_pin_own_posts=bool(channel.get("can_edit_messages")),
                              approval_required_for_public_writes=True,
                              editor_workflow=["draft_preview", "drafts", "queue_plan", "queue_set", "queue_cancel", "analytics"],
                              owner_buttons=True, queue_misfire_grace_seconds=900,
                              disabled=["delete", "ban", "invite", "promote", "personal_dm_outreach", "arbitrary_chats", "paid_actions", "stories", "second_poller"],
                              media_total_limit_bytes=50_000_000, poll_results="aggregates_only_from_existing_poller")
            return result
        if op == "comments":
            rows = store.conn.execute(
                "SELECT m.message_id,m.thread_id,m.text,m.created_at,d.action,d.reason,r.reply_message_id "
                "FROM messages m JOIN decisions d ON d.chat_id=m.chat_id AND d.message_id=m.message_id "
                "LEFT JOIN replies r ON r.chat_id=m.chat_id AND r.source_message_id=m.message_id "
                "WHERE m.chat_id=? AND m.is_bot=0 AND d.reason NOT IN ('general_chat','channel_root','bot_sender') "
                "AND (EXISTS(SELECT 1 FROM thread_roots t WHERE t.chat_id=m.chat_id AND t.root_message_id=m.thread_id) "
                "OR instr(lower(m.text),?)>0 OR instr(lower(m.text),'ai fixer')>0 OR instr(lower(m.text),'ии редактор')>0) "
                "ORDER BY m.created_at DESC LIMIT ?",
                (settings.telegram_discussion_id, f"@{settings.telegram_bot_username.lower()}", data.get("limit", 10)),
            ).fetchall()
            return {"ok": True, "untrusted_data": True, "scope": "new_channel_comments_and_mentions_only",
                    "comments": [{**dict(row), "text": str(row["text"])[:1500]} for row in rows]}
        if op in {"validate", "publish"}:
            with tempfile.TemporaryDirectory(prefix="ai-fixer-photo-") as temporary:
                photo = None
                if "photo_b64" in data:
                    raw, suffix = decode_photo(data["photo_b64"])
                    photo = Path(temporary) / (hashlib.sha256(raw).hexdigest() + suffix)
                    photo.write_bytes(raw)
                return await publish_post(settings, store, telegram, request_id=data["request_id"],
                                          text=data["text"], photo=photo, dry_run=op == "validate",
                                          confirm_hash=data.get("confirm_hash"))
        if op == "reply":
            previous = get_attempt(store, f"reply:{settings.telegram_discussion_id}:{data['message_id']}")
            if previous and previous["status"] in {"sending", "uncertain"}:
                return {"ok": False, "error": "previous_reply_requires_readback",
                        "readback_required": True, "retry_allowed": False}
            row = store.conn.execute(
                "SELECT * FROM messages WHERE chat_id=? AND message_id=?",
                (settings.telegram_discussion_id, data["message_id"]),
            ).fetchone()
            if row is None:
                return {"ok": False, "error": "comment_not_in_authorized_cache"}
            if row["is_bot"] or row["message_id"] == row["thread_id"]:
                return {"ok": False, "error": "not_a_subscriber_comment"}
            if settings.comment_mode != "live":
                return {"ok": False, "error": "live_replies_disabled"}
            message = {"message_id": row["message_id"], "message_thread_id": row["thread_id"],
                       "date": int(datetime.fromisoformat(row["created_at"]).timestamp()),
                       "chat": {"id": settings.telegram_discussion_id}, "text": row["text"],
                       "from": {"id": row["user_id"], "is_bot": bool(row["is_bot"])}}
            service = CommentService(settings, store=store, telegram=telegram)
            try:
                await service.process_update({"message": message}, revisit=True)
            finally:
                await service.llm.close()
            reply = store.conn.execute(
                "SELECT reply_message_id FROM replies WHERE chat_id=? AND source_message_id=?",
                (settings.telegram_discussion_id, data["message_id"]),
            ).fetchone()
            decision = store.conn.execute(
                "SELECT action,reason,reply_text FROM decisions WHERE chat_id=? AND message_id=?",
                (settings.telegram_discussion_id, data["message_id"]),
            ).fetchone()
            return {"ok": True, "sent_or_already_sent": reply is not None,
                    "reply_message_id": reply["reply_message_id"] if reply else None,
                    "decision": dict(decision) if decision else None,
                    "readback_required": reply is not None}
        raise ValueError("unsupported_operation")
    finally:
        await telegram.close()
        store.close()


def run(data: dict) -> dict:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        return asyncio.run(dispatch(data, Settings.from_env()))
    except ValueError as exc:
        import re
        safe = str(exc)
        return {"ok": False, "error": safe if re.fullmatch(r"[a-z][a-z0-9_]{3,100}", safe) else "invalid_request"}
    except Exception as exc:
        # External exception text may contain credentials; never forward it to an agent.
        return {"ok": False, "error": "broker_request_failed", "error_type": type(exc).__name__}
