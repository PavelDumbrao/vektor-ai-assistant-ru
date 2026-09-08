from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Settings
from .db import StateStore
from .llm import LlmClient, ModelError
from .models import ReplyDecision
from .outbound import OutboundBusy, finish_attempt, get_attempt, outbound_lock, reserve_attempt
from .policy import enforce_reply_policy, is_channel_root, precheck_comment, route_message
from .telegram import TelegramApiError, TelegramClient


LOG = logging.getLogger("ai_fixer_social")


def _message_text(message: dict) -> str:
    return str(message.get("text") or message.get("caption") or "").strip()


def _message_iso_date(message: dict) -> str:
    raw = message.get("date")
    if isinstance(raw, int):
        return datetime.fromtimestamp(raw, UTC).isoformat()
    return datetime.now(UTC).isoformat()


def _channel_post_id(message: dict) -> int | None:
    legacy = message.get("forward_from_message_id")
    if isinstance(legacy, int):
        return legacy
    origin = message.get("forward_origin") or {}
    value = origin.get("message_id")
    return int(value) if isinstance(value, int) else None


class CommentService:
    def __init__(
        self,
        settings: Settings,
        *,
        store: StateStore | None = None,
        telegram: Any | None = None,
        llm: Any | None = None,
        policy_text: str | None = None,
    ):
        self.settings = settings
        self.store = store or StateStore(settings.state_db)
        loaded_policy = policy_text
        if loaded_policy is None:
            loaded_policy = Path(settings.editorial_policy).read_text(encoding="utf-8")
        self.telegram = telegram or TelegramClient(settings.telegram_bot_token)
        self.llm = llm or LlmClient(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.llm_comment_model,
            editorial_policy=loaded_policy,
        )
        self.bot_id: int | None = None

    async def close(self) -> None:
        await self.llm.close()
        await self.telegram.close()
        self.store.close()

    async def startup_check(self) -> None:
        me = await self.telegram.get_me()
        self.bot_id = int(me["id"])
        live_username = str(me.get("username") or "")
        if live_username.lower() != self.settings.telegram_bot_username.lower():
            raise RuntimeError("Configured bot username does not match getMe")
        member = await self.telegram.get_chat_member(self.settings.telegram_discussion_id, self.bot_id)
        if member.get("status") not in {"administrator", "creator"}:
            raise RuntimeError("Bot is not an administrator of the discussion group")
        LOG.info(
            "startup_ok bot_id=%s discussion_id=%s comment_mode=%s",
            self.bot_id,
            self.settings.telegram_discussion_id,
            self.settings.comment_mode,
        )

    async def bootstrap_offset(self) -> int:
        saved = self.store.get_int("telegram_update_offset")
        if saved is not None:
            return saved
        if not self.settings.bootstrap_skip_pending:
            self.store.set_int("telegram_update_offset", 0)
            return 0
        updates = await self.telegram.get_updates(offset=-1, timeout=0)
        offset = max((int(item["update_id"]) for item in updates), default=-1) + 1
        self.store.set_int("telegram_update_offset", offset)
        LOG.info("bootstrap_skip_pending next_offset=%s", offset)
        return offset

    async def process_update(self, update: dict, *, revisit: bool = False) -> None:
        if isinstance(update.get("poll"), dict):
            self.store.update_poll(update["poll"])
            return
        edited_post = update.get("edited_channel_post")
        if isinstance(edited_post, dict) and (edited_post.get("chat") or {}).get("id") == self.settings.telegram_channel_id:
            known = self.store.telegram_object(self.settings.telegram_channel_id, edited_post.get("message_id", 0))
            if known:
                self.store.record_telegram_object(self.settings.telegram_channel_id, edited_post, known["kind"])
            return
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat_id = (message.get("chat") or {}).get("id")
        message_id = message.get("message_id")
        if not isinstance(chat_id, int) or not isinstance(message_id, int):
            return
        if chat_id != self.settings.telegram_discussion_id:
            return
        if revisit:
            replied = self.store.conn.execute(
                "SELECT 1 FROM replies WHERE chat_id=? AND source_message_id=?", (chat_id, message_id)
            ).fetchone()
            if replied or get_attempt(self.store, f"reply:{chat_id}:{message_id}"):
                return
        if self.store.is_processed(chat_id, message_id) and not revisit:
            return

        if is_channel_root(message, self.settings.telegram_channel_id):
            self.store.remember_root(
                chat_id,
                message_id,
                channel_post_id=_channel_post_id(message),
                root_text=_message_text(message),
                created_at=_message_iso_date(message),
            )
            self.store.record_message(
                chat_id=chat_id,
                message_id=message_id,
                thread_id=message_id,
                user_id=None,
                is_bot=False,
                text=_message_text(message),
                created_at=_message_iso_date(message),
            )
            self.store.record_simple_decision(
                chat_id, message_id, action="ignore", risk="low", reason="channel_root"
            )
            LOG.info("channel_root message_id=%s channel_post_id=%s", message_id, _channel_post_id(message))
            return

        reply = message.get("reply_to_message") or {}
        if is_channel_root(reply, self.settings.telegram_channel_id):
            reply_id = int(reply["message_id"])
            self.store.remember_root(
                chat_id,
                reply_id,
                channel_post_id=_channel_post_id(reply),
                root_text=_message_text(reply),
                created_at=_message_iso_date(reply),
            )

        route = route_message(
            message,
            discussion_id=self.settings.telegram_discussion_id,
            channel_id=self.settings.telegram_channel_id,
            bot_username=self.settings.telegram_bot_username,
            root_exists=lambda root_id: self.store.root_exists(chat_id, root_id),
        )
        sender = message.get("from") or {}
        user_id = sender.get("id") if isinstance(sender.get("id"), int) else None
        text = _message_text(message)
        self.store.record_message(
            chat_id=chat_id,
            message_id=message_id,
            thread_id=route.thread_id,
            user_id=user_id,
            is_bot=bool(sender.get("is_bot")),
            text=text,
            created_at=_message_iso_date(message),
        )

        if route.kind in {"ignore", "root"}:
            self.store.record_simple_decision(
                chat_id, message_id, action="ignore", risk="low", reason=route.reason
            )
            return
        if self.settings.comment_mode == "off":
            self.store.record_simple_decision(
                chat_id, message_id, action="ignore", risk="low", reason="comment_mode_off"
            )
            return

        age = max(0, int(time.time()) - int(message.get("date") or int(time.time())))
        if age > self.settings.max_comment_age_seconds:
            self.store.record_simple_decision(
                chat_id, message_id, action="ignore", risk="low", reason="comment_too_old"
            )
            return

        precheck = precheck_comment(text)
        if precheck.action != "continue":
            self.store.record_simple_decision(
                chat_id,
                message_id,
                action=precheck.action,
                risk=precheck.risk,
                reason=precheck.reason,
            )
            LOG.info(
                "precheck message_id=%s action=%s reason=%s",
                message_id,
                precheck.action,
                precheck.reason,
            )
            return

        allowed, limit_reason = self.store.reply_limits_ok(
            thread_id=route.thread_id,
            user_id=user_id,
            per_hour=self.settings.max_replies_per_hour,
            per_thread_hour=self.settings.max_replies_per_thread_hour,
            per_day=self.settings.max_replies_per_day,
            user_cooldown_minutes=self.settings.user_cooldown_minutes,
        )
        if not allowed:
            self.store.record_simple_decision(
                chat_id, message_id, action="ignore", risk="low", reason=limit_reason
            )
            return

        try:
            decision = await self.llm.decide_reply(
                comment=text,
                post_context=self.store.root_text(chat_id, route.thread_id),
                thread_context=self.store.recent_thread_context(chat_id, route.thread_id),
                direct_mention=route.kind == "mention",
                runtime_state={
                    "reads_new_channel_comments": True,
                    "automatic_low_risk_replies_enabled": self.settings.comment_mode == "live",
                    "general_group_chat_is_ignored": True,
                    "sensitive_topics_are_escalated": True,
                    "can_delete_or_ban_users": False,
                },
            )
            decision = enforce_reply_policy(
                decision,
                min_confidence=self.settings.min_reply_confidence,
                max_chars=self.settings.max_reply_chars,
            )
        except (ModelError, ValueError) as exc:
            LOG.warning("model_failure message_id=%s type=%s", message_id, type(exc).__name__)
            decision = ReplyDecision(
                action="escalate",
                risk="medium",
                confidence=0.0,
                intent="",
                reason=f"model_failure:{type(exc).__name__}",
                reply_text="",
            )
        self.store.record_decision(chat_id, message_id, decision)

        if decision.action != "reply":
            LOG.info(
                "decision message_id=%s action=%s risk=%s confidence=%.2f",
                message_id,
                decision.action,
                decision.risk,
                decision.confidence,
            )
            return
        if self.settings.comment_mode == "shadow":
            LOG.info("shadow_reply message_id=%s confidence=%.2f", message_id, decision.confidence)
            return

        action_id = f"reply:{chat_id}:{message_id}"
        try:
            with outbound_lock(self.store):
                prior_reply = self.store.conn.execute(
                    "SELECT 1 FROM replies WHERE chat_id=? AND source_message_id=?", (chat_id, message_id)
                ).fetchone()
                if prior_reply or get_attempt(self.store, action_id):
                    return
                allowed, _ = self.store.reply_limits_ok(
                    thread_id=route.thread_id, user_id=user_id,
                    per_hour=self.settings.max_replies_per_hour,
                    per_thread_hour=self.settings.max_replies_per_thread_hour,
                    per_day=self.settings.max_replies_per_day,
                    user_cooldown_minutes=self.settings.user_cooldown_minutes,
                )
                if not allowed:
                    return
                reserve_attempt(self.store, action_id, StateStore.payload_hash(decision.reply_text))
                try:
                    sent = await self.telegram.send_reply(
                        chat_id=chat_id, message_id=message_id, text=decision.reply_text,
                    )
                    reply_id = int(sent["message_id"])
                    self.store.record_reply(
                        chat_id=chat_id, source_message_id=message_id, reply_message_id=reply_id,
                        thread_id=route.thread_id, user_id=user_id,
                    )
                    finish_attempt(self.store, action_id, "sent", {"message_id": reply_id})
                    LOG.info("reply_sent source_message_id=%s reply_message_id=%s", message_id, reply_id)
                except Exception as exc:
                    finish_attempt(self.store, action_id, "uncertain", {"error_type": type(exc).__name__})
                    LOG.warning("reply_uncertain source_message_id=%s type=%s", message_id, type(exc).__name__)
        except OutboundBusy:
            LOG.info("reply_deferred_outbound_busy source_message_id=%s", message_id)

    async def run(self) -> None:
        from .editor import process_due

        await self.startup_check()
        offset = await self.bootstrap_offset()
        while True:
            try:
                queued = await process_due(self.settings, self.store, self.telegram)
                if queued.get("processed"):
                    LOG.info("queue_completed draft_id=%s status=%s", queued["draft_id"], queued["status"])
                updates = await self.telegram.get_updates(offset=offset, timeout=25)
                for update in updates:
                    update_id = int(update["update_id"])
                    try:
                        await self.process_update(update)
                    except Exception as exc:
                        LOG.exception("update_failed update_id=%s type=%s", update_id, type(exc).__name__)
                    finally:
                        offset = update_id + 1
                        self.store.set_int("telegram_update_offset", offset)
            except TelegramApiError as exc:
                LOG.warning("poll_failed type=%s", type(exc).__name__)
                await asyncio.sleep(5)


async def _async_main() -> None:
    settings = Settings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # httpx logs full request URLs at INFO. Telegram embeds the bot token in the URL.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    service = CommentService(settings)
    try:
        await service.run()
    finally:
        await service.close()


def main() -> None:
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()
