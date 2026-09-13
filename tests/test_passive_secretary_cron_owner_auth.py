from __future__ import annotations

import asyncio
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
    def query_sources(self, **_kwargs):
        return []


class FakeReplyService:
    def available(self):
        return False

    async def send(self, *_args, **_kwargs):
        raise AssertionError("scheduled read auth must never reach outbound send")


def settings() -> Settings:
    return Settings(
        tenant_id="tester",
        source_id="telegram_business",
        test_run_id="",
        owner_telegram_user_ids=("717276059",),
        postgres_dsn_env="PASSIVE_SECRETARY_DATABASE_URL",
        source_ref_key_env="PASSIVE_SECRETARY_SOURCE_REF_KEY",
        retention_days=365,
    )


def owner_job(**overrides):
    job = {
        "id": "e92d9cc02eb6",
        "deliver": "origin",
        "attach_to_session": True,
        "origin": {
            "platform": "telegram",
            "chat_id": "717276059",
            "user_id": "717276059",
        },
    }
    job.update(overrides)
    return job


class ScheduledOwnerReadAuthTests(unittest.TestCase):
    def setUp(self):
        self.controller = PassiveSecretaryController(
            settings(), archive=FakeArchive(), reply_service=FakeReplyService()
        )
        self.session = "cron_e92d9cc02eb6_20260913_210134"

    def test_owner_bound_origin_cron_can_read(self):
        with mock.patch.object(self.controller, "_cron_context_active", return_value=True), mock.patch.object(
            self.controller, "_load_cron_job", return_value=owner_job()
        ):
            self.assertEqual(self.controller._read_owner_for(self.session), "717276059")
            payload = self.controller.handle_sources({"label": "Ольга Moon"}, session_id=self.session)
            self.assertIn('"ok": true', payload)

    def test_spoofed_cron_session_outside_cron_context_is_denied(self):
        with mock.patch.object(self.controller, "_cron_context_active", return_value=False), mock.patch.object(
            self.controller, "_load_cron_job", return_value=owner_job()
        ):
            self.assertIsNone(self.controller._read_owner_for(self.session))

    def test_foreign_or_non_origin_cron_is_denied(self):
        cases = [
            owner_job(origin={"platform": "telegram", "chat_id": "2", "user_id": "2"}),
            owner_job(deliver="telegram:717276059"),
            owner_job(attach_to_session=False),
            owner_job(origin={"platform": "telegram", "chat_id": "-1001", "user_id": "717276059"}),
        ]
        for job in cases:
            with self.subTest(job=job), mock.patch.object(
                self.controller, "_cron_context_active", return_value=True
            ), mock.patch.object(self.controller, "_load_cron_job", return_value=job):
                self.assertIsNone(self.controller._read_owner_for(self.session))

    def test_scheduled_read_auth_does_not_authorize_outbound_reply(self):
        with mock.patch.object(
            self.controller, "_scheduled_read_owner_for", return_value="717276059"
        ):
            payload = asyncio.run(self.controller.handle_reply({}, session_id=self.session))
        self.assertIn("owner_session_not_authorized", payload)


if __name__ == "__main__":
    unittest.main()
