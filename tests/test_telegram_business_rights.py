from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HERMES_ROOT = REPO_ROOT / "hermes-agent"
PASSIVE_MODULE_ROOT = REPO_ROOT / "modules" / "passive-secretary"
sys.path.insert(0, str(HERMES_ROOT))
sys.path.insert(0, str(PASSIVE_MODULE_ROOT))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


from telegram_business_rights import (  # noqa: E402
    classify_business_connection_rights_mapping,
)
from passive_secretary_plugin.normalizer import PassiveEventNormalizer  # noqa: E402
from passive_secretary_plugin.owner_intent import OwnerReplyIntentGate  # noqa: E402
from passive_secretary_plugin.settings import Settings  # noqa: E402


main_passive_updates = load_module(
    "main_telegram_passive_updates_release_test",
    HERMES_ROOT / "plugins" / "platforms" / "telegram" / "passive_updates.py",
)
frozen_passive_updates = load_module(
    "frozen_telegram_passive_updates_release_test",
    PASSIVE_MODULE_ROOT
    / "hermes-core-patch"
    / "plugins"
    / "platforms"
    / "telegram"
    / "passive_updates.py",
)
business_connection_rights_state = (
    main_passive_updates.business_connection_rights_state
)
_normalize_connection_snapshot_mapping = (
    main_passive_updates._normalize_connection_snapshot_mapping
)


class FakeRights:
    def __init__(self, values: dict[str, bool]):
        self.values = values

    def to_dict(self) -> dict[str, bool]:
        return dict(self.values)


def flags_for(rights: dict[str, bool], *, replies_enabled: bool = True) -> dict[str, bool]:
    _rights, rights_valid, receive_only, reply_only = (
        classify_business_connection_rights_mapping(rights)
    )
    return {
        "rights_valid": rights_valid,
        "receive_only": receive_only,
        "reply_only": reply_only,
        "capture_authorized": rights_valid
        and (receive_only or (replies_enabled and reply_only)),
        "is_enabled": True,
    }


def snapshot_for(rights: dict[str, bool]) -> dict[str, object]:
    return {
        "id": "connection-1",
        "user": {"id": 123, "is_bot": False, "first_name": "Owner"},
        "user_chat_id": 123,
        "date_utc": "2026-08-31T00:00:00Z",
        "rights": rights,
        **flags_for(rights),
    }


class BusinessRightsPolicyTests(unittest.TestCase):
    def assert_profile(
        self,
        rights: dict[str, bool],
        *,
        valid: bool = True,
        receive_only: bool,
        reply_only: bool,
    ) -> None:
        serialized, actual_valid, actual_receive, actual_reply = (
            classify_business_connection_rights_mapping(rights)
        )
        self.assertEqual(serialized, rights)
        self.assertIs(actual_valid, valid)
        self.assertIs(actual_receive, receive_only)
        self.assertIs(actual_reply, reply_only)
        self.assertEqual(
            business_connection_rights_state(FakeRights(rights)),
            (rights, valid, receive_only, reply_only),
        )

    def test_receive_only_profile_is_allowed(self) -> None:
        self.assert_profile(
            {"can_reply": False, "can_read_messages": False},
            receive_only=True,
            reply_only=False,
        )

    def test_reply_with_desktop_coupled_read_permission_is_allowed(self) -> None:
        self.assert_profile(
            {
                "can_reply": True,
                "can_read_messages": True,
                "can_delete_sent_messages": False,
            },
            receive_only=False,
            reply_only=True,
        )

    def test_reply_without_read_permission_remains_allowed(self) -> None:
        self.assert_profile(
            {"can_reply": True, "can_read_messages": False},
            receive_only=False,
            reply_only=True,
        )

    def test_read_without_reply_is_not_an_authorized_profile(self) -> None:
        self.assert_profile(
            {"can_reply": False, "can_read_messages": True},
            receive_only=False,
            reply_only=False,
        )

    def test_known_action_right_remains_forbidden(self) -> None:
        for action in (
            "can_delete_sent_messages",
            "can_delete_all_messages",
            "can_edit_name",
            "can_edit_bio",
            "can_edit_username",
            "can_edit_profile_photo",
            "can_manage_stories",
            "can_transfer_and_upgrade_gifts",
            "can_transfer_stars",
        ):
            with self.subTest(action=action):
                self.assert_profile(
                    {"can_reply": True, "can_read_messages": True, action: True},
                    receive_only=False,
                    reply_only=False,
                )

    def test_unknown_future_right_is_fail_closed_only_when_enabled(self) -> None:
        self.assert_profile(
            {
                "can_reply": True,
                "can_read_messages": True,
                "can_future_action": True,
            },
            receive_only=False,
            reply_only=False,
        )
        self.assert_profile(
            {
                "can_reply": True,
                "can_read_messages": True,
                "can_future_action": False,
            },
            receive_only=False,
            reply_only=True,
        )

    def test_frozen_patch_uses_the_same_rights_matrix(self) -> None:
        profiles = (
            {"can_reply": False, "can_read_messages": False},
            {"can_reply": True, "can_read_messages": False},
            {"can_reply": True, "can_read_messages": True},
            {"can_reply": False, "can_read_messages": True},
            {"can_reply": True, "can_read_messages": True, "can_future_action": True},
            {"can_reply": True, "can_read_messages": True, "can_future_action": False},
        )
        for rights in profiles:
            with self.subTest(rights=rights):
                self.assertEqual(
                    frozen_passive_updates.classify_business_connection_rights_mapping(
                        rights
                    ),
                    classify_business_connection_rights_mapping(rights),
                )

    def test_recovered_snapshot_uses_the_same_policy(self) -> None:
        snapshot = snapshot_for(
            {"can_reply": True, "can_read_messages": True, "can_edit_bio": False}
        )
        normalized = _normalize_connection_snapshot_mapping(
            snapshot,
            expected_connection_id="connection-1",
            tenant_owner_id=123,
            business_reply_enabled=True,
        )
        self.assertTrue(normalized["reply_only"])
        self.assertTrue(normalized["capture_authorized"])

    def test_standalone_normalizer_uses_the_same_policy(self) -> None:
        settings = Settings(
            tenant_id="tenant",
            source_id="telegram_business",
            test_run_id="",
            owner_telegram_user_ids=("123",),
            postgres_dsn_env="PASSIVE_SECRETARY_DATABASE_URL",
            source_ref_key_env="PASSIVE_SECRETARY_SOURCE_REF_KEY",
            retention_days=365,
        )
        normalizer = PassiveEventNormalizer(settings, b"x" * 32)
        payload = snapshot_for(
            {"can_reply": True, "can_read_messages": True, "can_edit_name": False}
        )
        result = normalizer._connection(
            {}, payload, "123", datetime(2026, 8, 31, tzinfo=timezone.utc)
        )
        self.assertIsNotNone(result)
        self.assertTrue(result["enabled"])
        self.assertTrue(result["telegram_can_reply"])

    def test_scheduled_or_internal_event_still_cannot_authorize_send(self) -> None:
        gate = OwnerReplyIntentGate(ttl_seconds=120)
        self.assertFalse(
            gate.observe_turn(
                owner_id="123",
                session_id="session",
                turn_id="turn",
                raw_user_message="ОТВЕТИТЬ: Алексею — тест",
                is_internal_event=True,
            )
        )

    def test_no_business_read_receipt_call_is_added(self) -> None:
        for relative in (
            "hermes-agent/plugins/platforms/telegram/passive_updates.py",
            "hermes-agent/plugins/platforms/telegram/adapter.py",
            "modules/passive-secretary/hermes-core-patch/plugins/platforms/telegram/passive_updates.py",
            "modules/passive-secretary/hermes-core-patch/plugins/platforms/telegram/adapter.py",
        ):
            text = (REPO_ROOT / relative).read_text(encoding="utf-8").casefold()
            self.assertNotIn("read_business_message", text, relative)
            self.assertNotIn("readbusinessmessage", text, relative)


if __name__ == "__main__":
    unittest.main()
