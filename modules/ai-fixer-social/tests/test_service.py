import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from ai_fixer_social.config import Settings
from ai_fixer_social.db import StateStore
from ai_fixer_social.models import ReplyDecision
from ai_fixer_social.outbound import get_attempt
from ai_fixer_social.service import CommentService


CHANNEL_ID = -100100
GROUP_ID = -100200


class FakeTelegram:
    def __init__(self):
        self.sent = []

    async def send_reply(self, **payload):
        self.sent.append(payload)
        return {"message_id": 999}

    async def close(self):
        return None


class FakeLlm:
    def __init__(self, decision=None):
        self.calls = []
        self.decision = decision or ReplyDecision(
            action="reply",
            risk="low",
            confidence=0.99,
            intent="question",
            reason="safe",
            reply_text="Смотри, агент получает задачу и возвращает проверяемый результат.",
        )

    async def decide_reply(self, **payload):
        self.calls.append(payload)
        return self.decision

    async def close(self):
        return None


def settings(root: Path, mode="shadow"):
    policy = root / "policy.md"
    policy.write_text("Тестовая политика", encoding="utf-8")
    return Settings(
        telegram_bot_token="test-token",
        telegram_channel_id=CHANNEL_ID,
        telegram_discussion_id=GROUP_ID,
        telegram_channel_username="ProAiCommunity",
        telegram_bot_username="PavelDAiTG_bot",
        pavel_user_id=1,
        llm_base_url="http://127.0.0.1:1",
        llm_api_key="test-key",
        llm_comment_model="test-model",
        state_db=root / "state.sqlite3",
        editorial_policy=policy,
        comment_mode=mode,
        bootstrap_skip_pending=True,
        max_reply_chars=600,
        min_reply_confidence=0.88,
        max_replies_per_hour=10,
        max_replies_per_thread_hour=2,
        max_replies_per_day=40,
        user_cooldown_minutes=60,
        max_comment_age_seconds=1800,
        log_level="INFO",
    )


def root_message():
    return {
        "message_id": 100,
        "date": int(time.time()),
        "chat": {"id": GROUP_ID},
        "sender_chat": {"id": CHANNEL_ID},
        "is_automatic_forward": True,
        "forward_origin": {"message_id": 3008},
        "caption": "Пост про AI-агента",
    }


def comment_message(text="А как это работает?"):
    return {
        "message_id": 101,
        "message_thread_id": 100,
        "date": int(time.time()),
        "chat": {"id": GROUP_ID},
        "from": {"id": 77, "is_bot": False},
        "text": text,
        "reply_to_message": root_message(),
    }


class CommentServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def make_service(self, mode="shadow"):
        cfg = settings(self.root, mode)
        telegram = FakeTelegram()
        llm = FakeLlm()
        store = StateStore(cfg.state_db)
        service = CommentService(
            cfg,
            store=store,
            telegram=telegram,
            llm=llm,
            policy_text="Тестовая политика",
        )
        return service, telegram, llm

    def test_shadow_comment_is_decided_but_not_sent(self):
        service, telegram, llm = self.make_service("shadow")
        asyncio.run(service.process_update({"update_id": 2, "message": comment_message()}))
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(telegram.sent, [])
        self.assertTrue(service.store.root_exists(GROUP_ID, 100))
        self.assertEqual(service.store.root_text(GROUP_ID, 100), "Пост про AI-агента")
        self.assertTrue(service.store.is_processed(GROUP_ID, 101))
        asyncio.run(service.close())

    def test_live_comment_is_sent_once(self):
        service, telegram, llm = self.make_service("live")
        asyncio.run(service.process_update({"update_id": 1, "message": root_message()}))
        asyncio.run(service.process_update({"update_id": 2, "message": comment_message()}))
        asyncio.run(service.process_update({"update_id": 2, "message": comment_message()}))
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(len(telegram.sent), 1)
        self.assertEqual(telegram.sent[0]["message_id"], 101)
        self.assertEqual(service.store.stats()["replies"], 1)
        asyncio.run(service.close())

    def test_sensitive_comment_never_reaches_model(self):
        service, telegram, llm = self.make_service("live")
        asyncio.run(service.process_update({"update_id": 1, "message": root_message()}))
        asyncio.run(
            service.process_update(
                {"update_id": 2, "message": comment_message("Сколько стоит и куда оплатить?")}
            )
        )
        self.assertEqual(llm.calls, [])
        self.assertEqual(telegram.sent, [])
        asyncio.run(service.close())

    def test_general_chat_does_not_reach_model(self):
        service, telegram, llm = self.make_service("live")
        message = comment_message("Кто сегодня смотрел новости?")
        message.pop("message_thread_id")
        message.pop("reply_to_message")
        asyncio.run(service.process_update({"update_id": 2, "message": message}))
        self.assertEqual(llm.calls, [])
        self.assertEqual(telegram.sent, [])
        asyncio.run(service.close())

    def test_operator_revisit_does_not_rewrite_completed_reply(self):
        service, telegram, llm = self.make_service("live")
        update = {"message": comment_message()}
        asyncio.run(service.process_update(update))
        asyncio.run(service.process_update(update, revisit=True))
        self.assertEqual(len(telegram.sent), 1)
        self.assertEqual(len(llm.calls), 1)
        asyncio.run(service.close())

    def test_operator_cannot_retry_uncertain_reply(self):
        service, telegram, llm = self.make_service("live")
        async def fail_send(**payload):
            telegram.sent.append(payload)
            raise TimeoutError("uncertain send")
        telegram.send_reply = fail_send
        update = {"message": comment_message()}
        asyncio.run(service.process_update(update))
        attempt = get_attempt(service.store, f"reply:{GROUP_ID}:101")
        self.assertEqual(attempt["status"], "uncertain")
        asyncio.run(service.process_update(update, revisit=True))
        self.assertEqual(len(telegram.sent), 1)
        self.assertEqual(len(llm.calls), 1)
        asyncio.run(service.close())

    def test_operator_revisit_keeps_age_limit(self):
        service, telegram, llm = self.make_service("live")
        message = comment_message()
        message["date"] = int(time.time()) - 3600
        asyncio.run(service.process_update({"message": message}, revisit=True))
        self.assertEqual(llm.calls, [])
        self.assertEqual(telegram.sent, [])
        asyncio.run(service.close())

    def test_poll_updates_do_not_reach_llm_or_record_people(self):
        service, telegram, llm = self.make_service("live")
        service.store.record_telegram_object(CHANNEL_ID, {"message_id": 20, "poll": {"id": "our-poll", "question": "Q", "total_voter_count": 0}}, "poll")
        asyncio.run(service.process_update({"poll": {"id": "our-poll", "total_voter_count": 2}}))
        asyncio.run(service.process_update({"poll_answer": {"poll_id": "our-poll", "user": {"id": 123}}}))
        self.assertEqual(service.store.telegram_object(CHANNEL_ID, 20)["message"]["poll"]["total_voter_count"], 2)
        self.assertEqual(service.store.stats()["messages"], 0)
        self.assertFalse(llm.calls)
        self.assertFalse(telegram.sent)
        asyncio.run(service.close())

    def test_only_tracked_channel_edits_refresh_the_cache(self):
        service, telegram, llm = self.make_service("live")
        service.store.record_telegram_object(CHANNEL_ID, {"message_id": 20, "text": "Старый"}, "text")
        asyncio.run(service.process_update({"edited_channel_post": {"message_id": 20, "chat": {"id": CHANNEL_ID}, "text": "Новый"}}))
        asyncio.run(service.process_update({"edited_channel_post": {"message_id": 21, "chat": {"id": CHANNEL_ID}, "text": "Чужой"}}))
        self.assertEqual(service.store.telegram_object(CHANNEL_ID, 20)["message"]["text"], "Новый")
        self.assertIsNone(service.store.telegram_object(CHANNEL_ID, 21))
        self.assertFalse(llm.calls)
        asyncio.run(service.close())


if __name__ == "__main__":
    unittest.main()
