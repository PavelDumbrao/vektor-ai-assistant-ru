from __future__ import annotations

import json
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
    def __init__(self, *, username=None):
        self.username = username
        self.resolve_calls = []

    def query_sources(self, **kwargs):
        return [{
            "source_ref": "chat:roman",
            "chat_label": "Роман Мурашко",
            "source_username": self.username,
            "last_message_at": None,
            "message_count": 3,
        }]

    def resolve_source_chat_ids(self, *, tenant_owner_id, source_refs):
        self.resolve_calls.append((tenant_owner_id, tuple(source_refs)))
        return {"chat:roman": 694403684}


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
    )


class PassiveSecretaryLiveIdentityTests(unittest.TestCase):
    def _controller(self, archive, resolver):
        return PassiveSecretaryController(
            settings(),
            archive=archive,
            reply_service=FakeReplyService(),
            identity_resolver=resolver,
        )

    def _sources(self, controller):
        with mock.patch.object(controller, "_read_owner_for", return_value="1"):
            return json.loads(
                controller.handle_sources(
                    {"label": "Роман Мурашко", "limit": 10},
                    session_id="owner-session",
                )
            )

    def test_sources_live_fills_missing_username_by_exact_chat_id(self):
        archive = FakeArchive(username=None)
        calls = []

        def resolver(**kwargs):
            calls.append(kwargs)
            return [{
                "status": "resolved",
                "telegram_id": 694403684,
                "username": "romanmurash",
                "display_name": "Роман Мурашко",
            }]

        payload = self._sources(self._controller(archive, resolver))
        self.assertEqual(payload["sources"][0]["source_username"], "@romanmurash")
        self.assertEqual(calls[0]["owner_id"], "1")
        self.assertEqual(calls[0]["chat_ids"], [694403684])
        self.assertEqual(calls[0]["session_id"], "owner-session")

    def test_archive_username_wins_without_live_call(self):
        archive = FakeArchive(username="@cached_name")
        resolver = mock.Mock(side_effect=AssertionError("live resolver must not run"))
        payload = self._sources(self._controller(archive, resolver))
        self.assertEqual(payload["sources"][0]["source_username"], "@cached_name")
        resolver.assert_not_called()

    def test_live_identity_id_mismatch_is_ignored(self):
        archive = FakeArchive(username=None)

        def resolver(**_kwargs):
            return [{
                "status": "resolved",
                "telegram_id": 999,
                "username": "wrong_person",
                "display_name": "Wrong",
            }]

        payload = self._sources(self._controller(archive, resolver))
        self.assertIsNone(payload["sources"][0]["source_username"])

    def test_live_null_username_remains_null(self):
        archive = FakeArchive(username=None)

        def resolver(**_kwargs):
            return [{
                "status": "resolved",
                "telegram_id": 694403684,
                "username": None,
                "display_name": "Роман Мурашко",
            }]

        payload = self._sources(self._controller(archive, resolver))
        self.assertIsNone(payload["sources"][0]["source_username"])

    def test_live_resolver_failure_is_fail_soft(self):
        archive = FakeArchive(username=None)

        def resolver(**_kwargs):
            raise RuntimeError("telegram unavailable")

        payload = self._sources(self._controller(archive, resolver))
        self.assertIsNone(payload["sources"][0]["source_username"])


if __name__ == "__main__":
    unittest.main()
