from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "modules" / "passive-secretary"
CORE_PATCH = MODULE / "hermes-core-patch"
sys.path.insert(0, str(CORE_PATCH))
sys.path.insert(0, str(MODULE))

from passive_secretary_plugin.controller import PassiveSecretaryController
from passive_secretary_plugin.settings import Settings


class FakeArchive:
    pass


class FakeReplyService:
    def available(self):
        return False


def settings() -> Settings:
    return Settings(
        tenant_id="tester",
        source_id="telegram_business",
        test_run_id="",
        owner_telegram_user_ids=("1",),
        postgres_dsn_env="PASSIVE_SECRETARY_DATABASE_URL",
        source_ref_key_env="PASSIVE_SECRETARY_SOURCE_REF_KEY",
        retention_days=365,
        timezone="Europe/Moscow",
        auto_context_enabled=False,
    )


class PassiveSecretaryOwnerRoutingTests(unittest.TestCase):
    def setUp(self):
        self.controller = PassiveSecretaryController(
            settings(), archive=FakeArchive(), reply_service=FakeReplyService()
        )

    def _owner_turn(self, text: str):
        with mock.patch.object(
            self.controller.authorizer,
            "learn",
            return_value="1",
        ):
            return self.controller.on_pre_llm_call(
                session_id="owner-session",
                turn_id="turn-1",
                user_message=text,
                raw_user_message=text,
                sender_id="1",
                platform="telegram",
                chat_type="dm",
            )

    def test_archive_username_request_gets_native_tool_routing_even_when_auto_context_off(self):
        result = self._owner_turn(
            "Найди в моём пассивном архиве эти контакты и выведи имя — @username"
        )
        self.assertIsNotNone(result)
        self.assertFalse(result["persist"])
        context = result["context"]
        self.assertIn("passive_secretary_activity", context)
        self.assertIn("passive_secretary_sources", context)
        self.assertIn("passive_secretary_search", context)
        self.assertIn("passive_secretary_recall", context)
        self.assertIn("execute_code", context)
        self.assertIn("DO NOT", context)
        self.assertIn("source_username", context)
        self.assertIn("never infer", context.lower())

    def test_general_archive_request_routes_to_native_tools(self):
        result = self._owner_turn(
            "Посмотри мои переписки за неделю и найди незакрытые договорённости"
        )
        self.assertIsNotNone(result)
        self.assertIn("PASSIVE SECRETARY ROUTING CONTRACT", result["context"])

    def test_unrelated_owner_request_does_not_inject_routing_when_auto_context_off(self):
        result = self._owner_turn("Напиши мне план тренировки на завтра")
        self.assertIsNone(result)

    def test_non_owner_never_gets_routing_context(self):
        with mock.patch.object(
            self.controller.authorizer,
            "learn",
            return_value=None,
        ):
            result = self.controller.on_pre_llm_call(
                session_id="foreign",
                turn_id="turn-x",
                user_message="Покажи архив переписок и username",
                raw_user_message="Покажи архив переписок и username",
                sender_id="2",
                platform="telegram",
                chat_type="dm",
            )
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
