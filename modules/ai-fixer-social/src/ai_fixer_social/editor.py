"""Immutable owner-reviewed drafts and a durable, at-most-once publication queue."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from .db import utc_now
from .outbound import OutboundBusy, get_attempt, outbound_lock, publish_post
from .rich_post import prepare_article
from .telegram_content import prepare_spec


MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
EDITABLE = {"draft", "confirm_publish", "choose_time", "confirm_queue"}
RESCHEDULABLE = EDITABLE | {"queued", "blocked", "expired"}


def migrate(store):
    store.conn.executescript("""
        CREATE TABLE IF NOT EXISTS editor_drafts (
            draft_id TEXT PRIMARY KEY, channel_id INTEGER NOT NULL,
            payload_hash TEXT NOT NULL, content_json TEXT NOT NULL,
            kind TEXT NOT NULL, title TEXT NOT NULL, rubric TEXT NOT NULL,
            status TEXT NOT NULL, nonce TEXT UNIQUE NOT NULL,
            bind_token TEXT NOT NULL, preview_message_id INTEGER,
            created_at REAL NOT NULL, expires_at REAL NOT NULL,
            scheduled_at REAL, approved_at REAL, approval_source TEXT,
            result_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS editor_events (
            id INTEGER PRIMARY KEY, draft_id TEXT NOT NULL,
            event TEXT NOT NULL, created_at REAL NOT NULL,
            source TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_editor_due ON editor_drafts(status,scheduled_at);
    """)
    store.conn.commit()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))


def prepare_content(content):
    if not isinstance(content, dict) or set(content) not in ({"spec"}, {"article"}):
        raise ValueError("one_content_format_required")
    if len(canonical(content).encode()) > MAX_SNAPSHOT_BYTES:
        raise ValueError("editor_snapshot_exceeds_16_mib")
    if "article" in content:
        prepared = prepare_article(content["article"])
        from .post_validation import visible_text
        return prepared.payload_hash, "rich", visible_text(prepared.html)[:300]
    prepared = prepare_spec(content["spec"])
    if not prepared.creates_message or prepared.kind == "album":
        raise ValueError("editor_requires_single_message_use_tg_preview_for_album")
    # An absolute poll close time would expire while waiting for owner approval.
    if prepared.kind in {"poll", "quiz"} and "close_date" in content["spec"]:
        raise ValueError("queued_poll_use_open_period_not_close_date")
    source = content["spec"]
    return prepared.payload_hash, prepared.kind, str(source.get("text") or source.get("caption") or source.get("question") or prepared.kind)[:300]


def get_draft(store, draft_id):
    row = store.conn.execute("SELECT * FROM editor_drafts WHERE draft_id=?", (draft_id,)).fetchone()
    if row is None:
        raise ValueError("draft_not_found")
    return dict(row)


def summary(row):
    return {key: row[key] for key in (
        "draft_id", "payload_hash", "kind", "title", "rubric", "status",
        "preview_message_id", "created_at", "scheduled_at", "approved_at"
    )} | {"result": json.loads(row["result_json"])}


def event(store, row, name, source=""):
    store.conn.execute("INSERT INTO editor_events(draft_id,event,created_at,source) VALUES(?,?,?,?)",
                       (row["draft_id"], name, utc_now().timestamp(), source))


def keyboard(row):
    def button(text, action):
        return {"text": text, "callback_data": f"af:{row['nonce']}:{action}"}
    phase = row["status"]
    if phase == "draft":
        rows = [[button("🚀 Опубликовать", "publish"), button("🗓 В очередь", "queue")],
                [button("✏️ Правки текста", "edit"), button("🖼 Другая картинка", "image")],
                [button("Отменить черновик", "cancel")]]
    elif phase in {"confirm_publish", "confirm_queue"}:
        when = "сейчас" if phase == "confirm_publish" else format_time(row["scheduled_at"])
        rows = [[button(f"✅ В @ProAiCommunity {when}", "confirm")], [button("Назад", "back")]]
    elif phase == "choose_time":
        rows = [[button("Через час", "hour"), button("Ближайшие 11:30 МСК", "slot")], [button("Назад", "back")]]
    elif phase == "queued":
        rows = [[button("🔄 Статус", "status"), button("Снять из очереди", "cancel")]]
    elif phase in {"sent", "blocked", "expired", "uncertain"}:
        rows = [[button("🔄 Статус", "status")]]
        receipt = json.loads(row["result_json"])
        if phase == "sent" and receipt.get("public_url"):
            rows.append([{"text": "Открыть публикацию", "url": receipt["public_url"]}])
    else:
        rows = []
    return {"inline_keyboard": rows}


def format_time(timestamp):
    return datetime.fromtimestamp(timestamp, ZoneInfo("Europe/Moscow")).strftime("%d.%m %H:%M МСК")


def create_draft(store, settings, *, draft_id, content, rubric=""):
    if not isinstance(draft_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{3,99}", draft_id):
        raise ValueError("invalid_draft_id")
    if not isinstance(rubric, str) or len(rubric) > 80:
        raise ValueError("invalid_rubric")
    digest, kind, title = prepare_content(content)
    now = utc_now().timestamp()
    with store.conn:
        store.conn.execute("BEGIN IMMEDIATE")
        old = store.conn.execute("SELECT * FROM editor_drafts WHERE draft_id=?", (draft_id,)).fetchone()
        if old:
            if old["payload_hash"] != digest or old["rubric"] != rubric:
                raise ValueError("draft_id_content_is_immutable_use_new_version")
            row = dict(old)
        else:
            duplicate = store.conn.execute(
                "SELECT * FROM editor_drafts WHERE channel_id=? AND payload_hash=? AND status NOT IN ('canceled','edit_requested','image_requested') ORDER BY created_at DESC LIMIT 1",
                (settings.telegram_channel_id, digest),
            ).fetchone()
            if duplicate:
                row = dict(duplicate)
            else:
                recent = store.conn.execute("SELECT COUNT(*) FROM editor_drafts WHERE created_at>?", (now - 86400,)).fetchone()[0]
                if recent >= 24:
                    raise ValueError("draft_daily_limit")
                used = store.conn.execute("SELECT COALESCE(SUM(LENGTH(CAST(content_json AS BLOB))),0) FROM editor_drafts").fetchone()[0]
                if used + len(canonical(content).encode()) > 512 * 1024 * 1024:
                    raise ValueError("snapshot_storage_limit_requires_review")
                store.conn.execute(
                    "INSERT INTO editor_drafts(draft_id,channel_id,payload_hash,content_json,kind,title,rubric,status,nonce,bind_token,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (draft_id, settings.telegram_channel_id, digest, canonical(content), kind, title, rubric,
                     "preparing", secrets.token_urlsafe(18), secrets.token_urlsafe(24), now, now + 7 * 86400),
                )
                row = get_draft(store, draft_id)
                event(store, row, "created")
    preview_row = {**row, "status": "draft"} if row["status"] == "preparing" else row
    # This transport-only result MUST NOT be forwarded to the model.
    return {"ok": True, **summary(row), "reply_markup": keyboard(preview_row), "bind_token": row["bind_token"]}


def bind_preview(store, *, draft_id, bind_token, message_id):
    if type(message_id) is not int or message_id <= 0:
        raise ValueError("invalid_preview_id")
    with store.conn:
        store.conn.execute("BEGIN IMMEDIATE")
        row = get_draft(store, draft_id)
        if not secrets.compare_digest(str(bind_token), row["bind_token"]):
            raise ValueError("invalid_preview_binding")
        if row["preview_message_id"] == message_id:
            return {"ok": True, **summary(row), "deduplicated": True}
        if row["status"] != "preparing" or row["preview_message_id"] is not None:
            raise ValueError("preview_already_bound")
        store.conn.execute("UPDATE editor_drafts SET preview_message_id=?,status='draft' WHERE draft_id=?", (message_id, draft_id))
        event(store, row, "preview_bound")
    return {"ok": True, **summary(get_draft(store, draft_id)), "published": False}


def parse_schedule(value, now=None):
    now = utc_now().timestamp() if now is None else now
    if not isinstance(value, str):
        raise ValueError("schedule_requires_iso_time_with_timezone")
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("schedule_timezone_required")
    result = stamp.astimezone(UTC).timestamp()
    if not now + 60 <= result <= now + 30 * 86400:
        raise ValueError("schedule_must_be_1_minute_to_30_days_ahead")
    return result


def plan_hash(row, timestamp):
    return hashlib.sha256(canonical({"draft_id": row["draft_id"], "hash": row["payload_hash"],
                                    "channel": row["channel_id"], "scheduled_at": timestamp}).encode()).hexdigest()


def schedule_plan(store, *, draft_id, scheduled_at):
    row = get_draft(store, draft_id)
    if row["status"] not in RESCHEDULABLE or not row["preview_message_id"]:
        raise ValueError("draft_not_ready_for_scheduling")
    if row["status"] in EDITABLE and row["expires_at"] < utc_now().timestamp():
        raise ValueError("preview_expired_request_fresh_preview")
    stamp = parse_schedule(scheduled_at)
    return {"ok": True, **summary(row), "scheduled_at": stamp, "schedule_label": format_time(stamp),
            "confirm_hash": plan_hash(row, stamp)}


def check_slot(store, row, stamp):
    if store.conn.execute(
        "SELECT 1 FROM editor_drafts WHERE draft_id!=? AND status IN ('queued','sending') AND ABS(scheduled_at-?)<21600 LIMIT 1",
        (row["draft_id"], stamp),
    ).fetchone():
        raise ValueError("another_approved_post_within_six_hours")
    # Old queued sends with an ambiguous result may never be re-authorized by changing time.
    if get_attempt(store, "post:draft-" + row["draft_id"]):
        raise ValueError("draft_already_attempted_readback_required")


def set_schedule(store, *, draft_id, scheduled_at, confirm_hash):
    with store.conn:
        store.conn.execute("BEGIN IMMEDIATE")
        plan = schedule_plan(store, draft_id=draft_id, scheduled_at=scheduled_at)
        if not secrets.compare_digest(str(confirm_hash), plan["confirm_hash"]):
            raise ValueError("confirmation_payload_mismatch")
        row = get_draft(store, draft_id)
        check_slot(store, row, plan["scheduled_at"])
        if row["status"] == "queued" and row["scheduled_at"] == plan["scheduled_at"]:
            return {"ok": True, **summary(row), "deduplicated": True}
        store.conn.execute(
            "UPDATE editor_drafts SET status='queued',scheduled_at=?,approved_at=?,approval_source='hermes_one_shot',nonce=?,result_json='{}' WHERE draft_id=?",
            (plan["scheduled_at"], utc_now().timestamp(), secrets.token_urlsafe(18), draft_id),
        )
        event(store, row, "scheduled", "hermes_one_shot")
    return {"ok": True, **summary(get_draft(store, draft_id)), "published": False}


def cancel_draft(store, *, draft_id, confirm_hash):
    with store.conn:
        store.conn.execute("BEGIN IMMEDIATE")
        row = get_draft(store, draft_id)
        if not secrets.compare_digest(str(confirm_hash), row["payload_hash"]):
            raise ValueError("confirmation_payload_mismatch")
        if row["status"] == "canceled":
            return {"ok": True, **summary(row), "deduplicated": True}
        if row["status"] not in RESCHEDULABLE:
            raise ValueError("draft_cannot_be_canceled_in_current_state")
        store.conn.execute("UPDATE editor_drafts SET status='canceled',nonce=? WHERE draft_id=?", (secrets.token_urlsafe(18), draft_id))
        event(store, row, "canceled", "hermes_one_shot")
    return {"ok": True, **summary(get_draft(store, draft_id)), "published": False}


def click_draft(store, settings, *, data, user_id, chat_id, message_id, callback_id):
    if type(user_id) is not int or type(chat_id) is not int or user_id != settings.pavel_user_id or chat_id != settings.pavel_user_id:
        raise ValueError("owner_dm_required")
    if not isinstance(data, str) or not re.fullmatch(r"af:[A-Za-z0-9_-]{24}:[a-z]+", data):
        raise ValueError("invalid_callback")
    _, nonce, action = data.split(":")
    now = utc_now().timestamp()
    with store.conn:
        store.conn.execute("BEGIN IMMEDIATE")
        found = store.conn.execute("SELECT * FROM editor_drafts WHERE nonce=?", (nonce,)).fetchone()
        if not found or found["preview_message_id"] != message_id or found["channel_id"] != settings.telegram_channel_id:
            raise ValueError("stale_or_unbound_button")
        row = dict(found)
        phase = row["status"]
        if row["expires_at"] < now and phase in EDITABLE:
            raise ValueError("preview_expired_request_fresh_preview")
        status, stamp, approved = phase, row["scheduled_at"], row["approved_at"]
        label = "Готово"
        if action == "status" and phase in {"queued", "sending", "sent", "blocked", "expired", "uncertain"}:
            receipt = json.loads(row["result_json"])
            label = (f"В очереди на {format_time(stamp)}." if phase == "queued" else
                     f"Отправлено. message_id: {receipt.get('message_id')}." if phase == "sent" else
                     "Отправка выполняется, повторять её нельзя." if phase == "sending" else
                     f"Статус: {phase}. {receipt.get('error', '')}. Попроси ВЕКТОРА проверить результат.")
        elif phase == "draft" and action == "publish":
            status, label = "confirm_publish", "Подтверди публикацию именно этого поста в канале."
        elif phase == "draft" and action == "queue":
            status, label = "choose_time", "Выбери время. Другое время можно написать ВЕКТОРУ."
        elif phase == "draft" and action in {"edit", "image"}:
            status = "edit_requested" if action == "edit" else "image_requested"
            label = "Ответь на этот пост: что меняем? После правки нужен новый показ."
        elif action == "cancel" and phase in EDITABLE | {"queued"}:
            status, label = "canceled", "Черновик отменён. В канал не отправится."
        elif action == "back" and phase in {"confirm_publish", "confirm_queue", "choose_time"}:
            status, stamp, label = "draft", None, "Вернулись к черновику."
        elif phase == "choose_time" and action in {"hour", "slot"}:
            if action == "hour":
                stamp = now + 3600
            else:
                slot = datetime.fromtimestamp(now, ZoneInfo("Europe/Moscow")).replace(hour=11, minute=30, second=0, microsecond=0)
                if slot.timestamp() < now + 60:
                    slot += timedelta(days=1)
                while slot.weekday() == 6:
                    slot += timedelta(days=1)
                stamp = slot.timestamp()
            status, label = "confirm_queue", f"Подтверди время: {format_time(stamp)}."
        elif action == "confirm" and phase in {"confirm_publish", "confirm_queue"}:
            stamp = now if phase == "confirm_publish" else stamp
            if stamp is None or stamp < now - 60:
                raise ValueError("chosen_time_has_passed")
            check_slot(store, row, stamp)
            status, approved = "queued", now
            label = "Одобрено. Отправку выполнит очередь с проверкой прав и лимитов."
        else:
            raise ValueError("action_not_allowed_in_current_phase")
        # Every rendered phase gets a fresh nonce: old confirmations cannot approve a new time.
        store.conn.execute(
            "UPDATE editor_drafts SET status=?,nonce=?,scheduled_at=?,approved_at=?,approval_source=? WHERE draft_id=?",
            (status, secrets.token_urlsafe(18), stamp, approved,
             f"callback:{callback_id}" if action == "confirm" and approved and status == "queued" else row["approval_source"], row["draft_id"]),
        )
        event(store, row, action, f"callback:{callback_id}")
    updated = get_draft(store, row["draft_id"])
    return {"ok": True, **summary(updated), "notice": label, "reply_markup": keyboard(updated)}


async def process_due(settings, store, telegram):
    """One due item per poll cycle. Recovery never blindly repeats a send."""
    try:
        # Separate short-lived queue lock; publish_post owns the shared outbound lock.
        class QueueLock:
            path = store.path.with_name(store.path.name + ".queue.db")
        with outbound_lock(QueueLock()):
            for old in store.conn.execute("SELECT * FROM editor_drafts WHERE status='sending'").fetchall():
                attempt = get_attempt(store, "post:draft-" + old["draft_id"])
                recovered = attempt and attempt["status"] == "sent"
                result = json.loads(attempt["result_json"]) if recovered else {"error": "interrupted_send_requires_readback", "retry_allowed": False}
                store.conn.execute("UPDATE editor_drafts SET status=?,result_json=? WHERE draft_id=?",
                                   ("sent" if recovered else "uncertain", canonical(result), old["draft_id"]))
            store.conn.commit()
            now = utc_now().timestamp()
            with store.conn:
                store.conn.execute("BEGIN IMMEDIATE")
                row = store.conn.execute("SELECT * FROM editor_drafts WHERE status='queued' AND scheduled_at<=? ORDER BY scheduled_at LIMIT 1", (now,)).fetchone()
                if row is None:
                    return {"ok": True, "processed": False}
                row = dict(row)
                store.conn.execute("UPDATE editor_drafts SET status='sending' WHERE draft_id=?", (row["draft_id"],))
            result, state = {}, "blocked"
            try:
                if not row["approved_at"] or not row["approval_source"] or not row["preview_message_id"]:
                    raise ValueError("durable_owner_approval_missing")
                if row["channel_id"] != settings.telegram_channel_id:
                    raise ValueError("queued_target_mismatch")
                if now - row["scheduled_at"] > 900:
                    state = "expired"
                    raise ValueError("missed_slot_over_15_minutes_reapprove")
                content = json.loads(row["content_json"])
                digest, _, _ = prepare_content(content)
                if digest != row["payload_hash"]:
                    raise ValueError("snapshot_integrity_failure")
                me = await telegram.get_me()
                if str(me.get("username", "")).lower() != settings.telegram_bot_username.lower():
                    raise ValueError("configured_bot_mismatch")
                rights = await telegram.get_chat_member(settings.telegram_channel_id, me["id"])
                if rights.get("status") != "creator" and rights.get("can_post_messages") is not True:
                    raise ValueError("insufficient_channel_rights")
                result = await publish_post(settings, store, telegram, request_id="draft-" + row["draft_id"],
                                            text="", confirm_hash=digest, article=content.get("article"), telegram_spec=content.get("spec"))
                state = "sent" if result.get("ok") else "uncertain" if result.get("readback_required") or "readback" in result.get("error", "") else "blocked"
            except ValueError as exc:
                result = {"ok": False, "error": str(exc), "retry_allowed": False}
            except Exception as exc:
                state = "uncertain"
                result = {"ok": False, "error": "queue_result_uncertain", "error_type": type(exc).__name__, "retry_allowed": False}
            with store.conn:
                store.conn.execute("UPDATE editor_drafts SET status=?,result_json=? WHERE draft_id=?", (state, canonical(result), row["draft_id"]))
                event(store, row, state, "queue_worker")
            return {"ok": state == "sent", "processed": True, "draft_id": row["draft_id"], "status": state, "result": result}
    except OutboundBusy:
        return {"ok": True, "processed": False, "busy": True}
