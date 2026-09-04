from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace


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


def flags_for(rights: dict[str, bool]) -> dict[str, bool]:
    _rights, rights_valid, receive_only, reply_only = (
        classify_business_connection_rights_mapping(rights)
    )
    return {
        "rights_valid": rights_valid,
        "receive_only": receive_only,
        "reply_only": reply_only,
        "capture_authorized": rights_valid,
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


CAPTURE_RIGHTS_CASES = (
    {},
    {"can_view_gifts_and_stars": True},
    {"can_read_messages": True},
    {"can_reply": True},
    {
        "can_reply": True,
        "can_read_messages": True,
        "can_delete_all_messages": True,
        "can_edit_name": True,
        "can_manage_stories": True,
        "can_transfer_stars": True,
        "can_future_action": True,
    },
)


def connection_for(rights, *, owner_id=123, enabled=True):
    return SimpleNamespace(
        id="connection-1",
        user=SimpleNamespace(id=owner_id, is_bot=False, first_name="Owner"),
        user_chat_id=owner_id,
        date=datetime(2026, 8, 31, tzinfo=timezone.utc),
        rights=FakeRights(rights),
        is_enabled=enabled,
    )


def normalizer_for_owner():
    settings = Settings(
        tenant_id="tenant",
        source_id="telegram_business",
        test_run_id="",
        owner_telegram_user_ids=("123",),
        postgres_dsn_env="PASSIVE_SECRETARY_DATABASE_URL",
        source_ref_key_env="PASSIVE_SECRETARY_SOURCE_REF_KEY",
        retention_days=365,
    )
    return PassiveEventNormalizer(settings, b"x" * 32)


class IncomingCaptureRightsTests(unittest.TestCase):
    def test_lifecycle_capture_does_not_require_an_action_rights_profile(self):
        for module in (main_passive_updates, frozen_passive_updates):
            for rights in CAPTURE_RIGHTS_CASES:
                for replies_enabled in (False, True):
                    with self.subTest(module=module.__name__, rights=rights,
                                      replies_enabled=replies_enabled):
                        event = module.build_business_update_dto(
                            SimpleNamespace(update_id=100, business_connection=connection_for(rights)),
                            tenant_owner_id=123,
                            business_reply_enabled=replies_enabled,
                        )
                        self.assertTrue(event["payload"]["capture_authorized"])
                        normalized = normalizer_for_owner().normalize(event)
                        self.assertIsNotNone(normalized)
                        self.assertTrue(normalized["enabled"])

    def test_recovered_message_reaches_archive_with_additional_rights(self):
        for module in (main_passive_updates, frozen_passive_updates):
            for rights in CAPTURE_RIGHTS_CASES:
                with self.subTest(module=module.__name__, rights=rights):
                    snapshot = module.normalize_recovered_business_connection_snapshot(
                        connection_for(rights),
                        expected_connection_id="connection-1",
                        tenant_owner_id=123,
                        business_reply_enabled=False,
                    )
                    message = SimpleNamespace(
                        message_id=101,
                        business_connection_id="connection-1",
                        date=datetime(2026, 8, 31, tzinfo=timezone.utc),
                        chat=SimpleNamespace(id=456, type="private", first_name="Contact"),
                        from_user=SimpleNamespace(id=456, is_bot=False, first_name="Contact"),
                        text="Synthetic archive regression message",
                    )
                    event = module.build_business_update_dto(
                        SimpleNamespace(update_id=101, business_message=message),
                        tenant_owner_id=123,
                        business_reply_enabled=False,
                        connection_snapshot=snapshot,
                    )
                    normalized = normalizer_for_owner().normalize(event)
                    self.assertIsNotNone(normalized)
                    self.assertEqual(normalized["body"], message.text)
                    self.assertTrue(normalized["connection_snapshot"]["enabled"])

    def test_disabled_connection_does_not_become_enabled(self):
        event = main_passive_updates.build_business_update_dto(
            SimpleNamespace(update_id=100, business_connection=connection_for(
                {"can_view_gifts_and_stars": True}, enabled=False)),
            tenant_owner_id=123,
        )
        self.assertFalse(normalizer_for_owner().normalize(event)["enabled"])
        with self.assertRaises(ValueError):
            main_passive_updates.normalize_recovered_business_connection_snapshot(
                connection_for({}, enabled=False),
                expected_connection_id="connection-1",
                tenant_owner_id=123,
                business_reply_enabled=False,
            )

    def test_wrong_owner_and_connection_id_remain_rejected(self):
        for owner_id, connection_id in ((999, "connection-1"), (123, "other-connection")):
            with self.subTest(owner_id=owner_id, connection_id=connection_id):
                with self.assertRaises(ValueError):
                    main_passive_updates.normalize_recovered_business_connection_snapshot(
                        connection_for(CAPTURE_RIGHTS_CASES[-1], owner_id=owner_id),
                        expected_connection_id=connection_id,
                        tenant_owner_id=123,
                        business_reply_enabled=False,
                    )
        event = main_passive_updates.build_business_update_dto(
            SimpleNamespace(update_id=100, business_connection=connection_for({}, owner_id=999)),
            tenant_owner_id=999,
        )
        self.assertIsNone(normalizer_for_owner().normalize(event))

    def test_malformed_rights_cannot_forge_capture_authorization(self):
        for rights in ({"can_reply": "true"}, {"can_reply": 1}, {"unexpected": True}):
            with self.subTest(rights=rights):
                payload = snapshot_for({})
                payload.update(rights=rights, rights_valid=True, receive_only=False,
                               reply_only=False, capture_authorized=True)
                normalized = normalizer_for_owner()._connection(
                    {}, payload, "123", datetime(2026, 8, 31, tzinfo=timezone.utc))
                self.assertFalse(normalized["enabled"])
                with self.assertRaises(ValueError):
                    main_passive_updates._normalize_connection_snapshot_mapping(
                        payload,
                        expected_connection_id="connection-1",
                        tenant_owner_id=123,
                        business_reply_enabled=False,
                    )

    def test_digest_tampering_remains_rejected(self):
        event = main_passive_updates.build_business_update_dto(
            SimpleNamespace(update_id=100, business_connection=connection_for({})),
            tenant_owner_id=123,
        )
        event["payload"]["rights"] = {"can_view_gifts_and_stars": True}
        self.assertIsNone(normalizer_for_owner().normalize(event))

    def test_missing_rights_metadata_is_not_treated_as_an_empty_rights_object(self):
        connection = connection_for({})
        connection.rights = None
        event = main_passive_updates.build_business_update_dto(
            SimpleNamespace(update_id=100, business_connection=connection),
            tenant_owner_id=123,
        )
        self.assertFalse(event["payload"]["rights_valid"])
        self.assertFalse(normalizer_for_owner().normalize(event)["enabled"])

    def test_non_private_business_message_remains_rejected(self):
        snapshot = main_passive_updates.normalize_recovered_business_connection_snapshot(
            connection_for(CAPTURE_RIGHTS_CASES[-1]),
            expected_connection_id="connection-1",
            tenant_owner_id=123,
            business_reply_enabled=False,
        )
        message = SimpleNamespace(
            message_id=101,
            business_connection_id="connection-1",
            date=datetime(2026, 8, 31, tzinfo=timezone.utc),
            chat=SimpleNamespace(id=-456, type="supergroup", title="Excluded group"),
            from_user=SimpleNamespace(id=456, is_bot=False, first_name="Contact"),
            text="Synthetic out-of-scope message",
        )
        event = main_passive_updates.build_business_update_dto(
            SimpleNamespace(update_id=101, business_message=message),
            tenant_owner_id=123,
            connection_snapshot=snapshot,
        )
        self.assertIsNone(normalizer_for_owner().normalize(event))

    def test_additional_rights_do_not_enable_outbound_profile(self):
        for rights in (CAPTURE_RIGHTS_CASES[1], CAPTURE_RIGHTS_CASES[2], CAPTURE_RIGHTS_CASES[-1]):
            with self.subTest(rights=rights):
                event = main_passive_updates.build_business_update_dto(
                    SimpleNamespace(update_id=100, business_connection=connection_for(rights)),
                    tenant_owner_id=123,
                )
                normalized = normalizer_for_owner().normalize(event)
                self.assertTrue(normalized["enabled"])
                self.assertFalse(normalized["telegram_can_reply"])


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

    def test_read_without_reply_is_not_an_outbound_profile(self) -> None:
        self.assert_profile(
            {"can_reply": False, "can_read_messages": True},
            receive_only=False,
            reply_only=False,
        )

    def test_additional_action_right_remains_forbidden_for_outbound(self) -> None:
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
