"""One photo-caption preview to the configured owner; no arbitrary recipients."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from html.parser import HTMLParser
from pathlib import Path

from .rich_post import prepare_article
from .telegram_content import TelegramRejected, execute_spec, prepare_spec


class CaptionText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def plain_caption(text):
    parser = CaptionText()
    parser.feed(text)
    parser.close()
    return "".join(parser.parts)


def send_owner_photo(owner_id, text, raw):
    """Use the profile's Bot API transport, not the disabled cross-chat tool."""
    from gateway.config import Platform, load_gateway_config
    from telegram import Bot

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    config = load_gateway_config().platforms[Platform.TELEGRAM]
    if not config.enabled or not config.token or not config.home_channel:
        raise ValueError("telegram_profile_unavailable")
    if str(config.home_channel.chat_id) != owner_id:
        raise ValueError("telegram_owner_mismatch")

    async def send():
        async with Bot(token=config.token) as bot:
            if bot.username.lower() != "vektor_assist_bot":
                raise ValueError("preview_bot_mismatch")
            message = await bot.send_photo(
                chat_id=int(owner_id), photo=raw, caption=text, parse_mode="HTML",
                show_caption_above_media=False,
                connect_timeout=10, read_timeout=30, write_timeout=30, pool_timeout=10,
            )
            if (not message.photo or str(message.chat.id) != owner_id
                    or message.chat.type != "private" or message.caption != plain_caption(text)):
                raise ValueError("preview_response_mismatch")
            return {"message_id": message.message_id, "has_photo": True,
                    "caption_chars": len(message.caption)}

    return asyncio.run(send())


def send_owner_rich_post(owner_id, article):
    from gateway.config import Platform, load_gateway_config
    from telegram import Bot
    import httpx

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    config = load_gateway_config().platforms[Platform.TELEGRAM]
    if (not config.enabled or not config.token or not config.home_channel
            or str(config.home_channel.chat_id) != owner_id):
        raise ValueError("telegram_owner_mismatch")

    async def send():
        async with Bot(token=config.token) as bot:
            if bot.username.lower() != "vektor_assist_bot":
                raise ValueError("preview_bot_mismatch")
        async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10)) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{config.token}/sendRichMessage",
                data={"chat_id": owner_id, "rich_message": json.dumps(article.wire(), ensure_ascii=False)},
                files=article.files(),
            )
        response.raise_for_status()
        payload = response.json()
        result = payload.get("result", {})
        if (not payload.get("ok") or not result.get("rich_message")
                or str(result.get("chat", {}).get("id")) != owner_id):
            raise ValueError("rich_preview_response_mismatch")
        return {"message_id": result["message_id"], "has_rich_message": True, **article.summary()}

    return asyncio.run(send())


def deliver_preview(*, owner_id, request_id, text, raw, ledger_path: Path, sender=None):
    """Reserve before sending, then persist the receipt. Never retry ambiguity."""
    owner_id = str(owner_id)
    caption = plain_caption(text)
    if not caption.strip() or len(caption.encode("utf-16-le")) // 2 > 1024:
        raise ValueError("preview_caption_too_long_or_empty")
    if not isinstance(raw, bytes) or not raw or len(raw) > 9 * 1024 * 1024:
        raise ValueError("invalid_preview_photo")
    digest = hashlib.sha256(owner_id.encode() + b"\0" + text.encode() + b"\0" + raw).hexdigest()
    return deliver_once(owner_id=owner_id, request_id=request_id, digest=digest, ledger_path=ledger_path,
                        sender=lambda: (sender or send_owner_photo)(owner_id, text, raw),
                        required_field="has_photo", delivery="owner_dm_photo_caption")


def deliver_rich_preview(*, owner_id, request_id, article, ledger_path: Path, sender=None):
    owner_id = str(owner_id)
    prepared = prepare_article(article)
    digest = hashlib.sha256(f"rich:{owner_id}:{prepared.payload_hash}".encode()).hexdigest()
    return deliver_once(owner_id=owner_id, request_id=request_id, digest=digest, ledger_path=ledger_path,
                        sender=lambda: (sender or send_owner_rich_post)(owner_id, prepared),
                        required_field="has_rich_message", delivery="owner_dm_rich_message")


def send_owner_telegram(owner_id, spec):
    from gateway.config import Platform, load_gateway_config
    from telegram import Bot
    import httpx

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    config = load_gateway_config().platforms[Platform.TELEGRAM]
    if (not config.enabled or not config.token or not config.home_channel
            or str(config.home_channel.chat_id) != owner_id):
        raise ValueError("telegram_owner_mismatch")
    if not spec.creates_message:
        raise ValueError("private_preview_supports_new_messages_only")

    async def send():
        async with Bot(token=config.token) as bot:
            if bot.username.lower() != "vektor_assist_bot":
                raise ValueError("preview_bot_mismatch")
        async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=10)) as client:
            delivery = await execute_spec(client, f"https://api.telegram.org/bot{config.token}", int(owner_id), spec)
        first = delivery["messages"][0]
        result = {"message_id": first["message_id"], "message_ids": delivery["message_ids"],
                  "has_content": True, "kind": spec.kind, "payload_hash": spec.payload_hash}
        if first.get("poll"):
            result["poll_id"] = first["poll"]["id"]
        return result

    return asyncio.run(send())


def deliver_telegram_preview(*, owner_id, request_id, spec, ledger_path: Path, sender=None):
    owner_id = str(owner_id)
    prepared = prepare_spec(spec)
    if not prepared.creates_message:
        raise ValueError("private_preview_supports_new_messages_only")
    digest = hashlib.sha256(f"telegram:{owner_id}:{prepared.payload_hash}".encode()).hexdigest()
    return deliver_once(owner_id=owner_id, request_id=request_id, digest=digest, ledger_path=ledger_path,
                        sender=lambda: (sender or send_owner_telegram)(owner_id, prepared),
                        required_field="has_content", delivery="owner_dm_telegram_content")


def deliver_once(*, owner_id, request_id, digest, ledger_path, sender, required_field, delivery):
    if not owner_id.isdecimal() or int(owner_id) <= 0:
        raise ValueError("invalid_owner")
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{3,119}", request_id):
        raise ValueError("invalid_request_id")
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(ledger_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    os.close(descriptor)
    ledger_path.chmod(0o600)
    conn = sqlite3.connect(ledger_path, timeout=2)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS previews (request_id TEXT PRIMARY KEY, "
                     "payload_hash TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL, result_json TEXT)")
        conn.execute("BEGIN IMMEDIATE")
        previous = conn.execute("SELECT * FROM previews WHERE request_id=?", (request_id,)).fetchone()
        if previous and previous["payload_hash"] != digest:
            return {"ok": False, "error": "preview_request_id_payload_mismatch"}
        previous = previous or conn.execute(
            "SELECT * FROM previews WHERE payload_hash=? AND status IN ('sent','sending','uncertain') ORDER BY created_at DESC LIMIT 1", (digest,)
        ).fetchone()
        if previous and previous["status"] == "sent":
            return {**json.loads(previous["result_json"]), "deduplicated": True}
        if previous and previous["status"] == "rejected":
            return {"ok": False, "error": "previous_request_rejected", "retry_allowed": False, "readback_required": False}
        if conn.execute("SELECT 1 FROM previews WHERE status IN ('sending','uncertain') LIMIT 1").fetchone():
            return {"ok": False, "error": "previous_preview_requires_readback",
                    "readback_required": True, "retry_allowed": False}
        recent = conn.execute("SELECT COUNT(*) FROM previews WHERE created_at>?", (time.time() - 3600,)).fetchone()[0]
        if recent >= 6:
            return {"ok": False, "error": "preview_hourly_limit", "sent": False}
        conn.execute("INSERT INTO previews VALUES (?,?,?, ?,NULL)", (request_id, digest, "sending", time.time()))
        conn.commit()
        try:
            receipt = sender()
            if not receipt.get("message_id") or receipt.get(required_field) is not True:
                raise ValueError("invalid_preview_receipt")
            result = {"ok": True, **receipt, "request_id": request_id,
                      "delivery": delivery, "deduplicated": False,
                      "published": False, "readback_required": True,
                      "instruction": "Превью уже доставлено одним сообщением. Не дублируй текст или файл в финальном ответе. Публикация требует отдельного одобрения Павла."}
            status = "sent"
        except Exception as exc:
            result = {"ok": False, "error": "preview_result_uncertain", "error_type": type(exc).__name__,
                      "request_id": request_id, "readback_required": True, "retry_allowed": False}
            if type(getattr(exc, "code", None)) is int:
                result["telegram_error_code"] = exc.code
            status = "uncertain"
            if isinstance(exc, TelegramRejected):
                result["error"] = "telegram_request_rejected"
                result["readback_required"] = False
                status = "rejected"
        conn.execute("UPDATE previews SET status=?,result_json=? WHERE request_id=?",
                     (status, json.dumps(result, ensure_ascii=False), request_id))
        conn.commit()
        return result
    finally:
        conn.close()
