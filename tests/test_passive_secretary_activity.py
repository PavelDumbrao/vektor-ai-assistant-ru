from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "modules" / "passive-secretary"
CORE_PATCH = MODULE / "hermes-core-patch"
sys.path.insert(0, str(CORE_PATCH))
sys.path.insert(0, str(MODULE))

from passive_secretary_plugin import controller as controller_mod
from passive_secretary_plugin.controller import PassiveSecretaryController
from passive_secretary_plugin.settings import Settings


class FakeArchive:
    def __init__(self):
        self.calls = []

    def query_activity_days(self, ranges, **kwargs):
        self.calls.append(("days", ranges, kwargs))
        return [
            {
                "local_day": "2026-09-17",
                "message_count": 567,
                "contact_count": 66,
                "incoming_count": 363,
                "outgoing_count": 204,
            },
            {
                "local_day": "2026-09-18",
                "message_count": 284,
                "contact_count": 49,
                "incoming_count": 168,
                "outgoing_count": 116,
            },
        ]

    def query_activity_contacts(self, ranges, **kwargs):
        self.calls.append(("contacts", ranges, kwargs))
        return {
            "rows": [
                {
                    "source_ref": "chat:abc123",
                    "chat_label": "Даша",
                    "source_username": "@DariaArseneva",
                    "message_count": 17,
                    "incoming_count": 7,
                    "outgoing_count": 10,
                    "active_days": 2,
                    "first_message_at": datetime(2026, 9, 17, 8, 0, tzinfo=timezone.utc),
                    "last_message_at": datetime(2026, 9, 18, 12, 30, tzinfo=timezone.utc),
                }
            ],
            "total_contacts": 1,
            "has_more": False,
        }


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


class PassiveSecretaryActivityTests(unittest.TestCase):
    def setUp(self):
        self.archive = FakeArchive()
        self.controller = PassiveSecretaryController(
            settings(), archive=self.archive, reply_service=FakeReplyService()
        )

    def _call(self, args):
        with mock.patch.object(self.controller, "_read_owner_for", return_value="1"):
            return json.loads(
                self.controller.handle_activity(args, session_id="owner-session")
            )

    def test_days_mode_returns_compact_period_map(self):
        payload = self._call(
            {
                "mode": "days",
                "start_date": "2026-09-17",
                "end_date": "2026-09-18",
            }
        )
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["mode"], "days")
        self.assertEqual(payload["timezone"], "Europe/Moscow")
        self.assertEqual(payload["totals"]["messages"], 851)
        self.assertEqual(payload["totals"]["days_with_activity"], 2)
        self.assertEqual(len(payload["days"]), 2)
        self.assertNotIn("body", json.dumps(payload, ensure_ascii=False))

    def test_contacts_mode_is_bounded_and_exposes_only_opaque_source_refs(self):
        payload = self._call(
            {
                "mode": "contacts",
                "start_date": "2026-09-17",
                "end_date": "2026-09-18",
                "limit": 50,
                "offset": 0,
            }
        )
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["mode"], "contacts")
        self.assertEqual(payload["total_contacts"], 1)
        self.assertFalse(payload["has_more"])
        self.assertIsNone(payload["next_offset"])
        self.assertEqual(payload["contacts"][0]["source_ref"], "chat:abc123")
        self.assertEqual(payload["contacts"][0]["username"], "@DariaArseneva")
        self.assertEqual(payload["contacts"][0]["active_days"], 2)
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("chat_id", serialized)
        self.assertNotIn("message_id", serialized)
        kind, _ranges, kwargs = self.archive.calls[-1]
        self.assertEqual(kind, "contacts")
        self.assertEqual(kwargs["limit"], 50)
        self.assertEqual(kwargs["offset"], 0)

    def test_contacts_pagination_reports_next_offset(self):
        self.archive.query_activity_contacts = lambda ranges, **kwargs: {
            "rows": [],
            "total_contacts": 350,
            "has_more": True,
        }
        payload = self._call(
            {
                "mode": "contacts",
                "date": "2026-09-18",
                "limit": 200,
                "offset": 200,
            }
        )
        self.assertTrue(payload["has_more"])
        self.assertEqual(payload["next_offset"], 400)

    def test_activity_tool_schema_teaches_self_planned_broad_audits(self):
        schema = controller_mod.ACTIVITY_TOOL_SCHEMA
        self.assertEqual(schema["name"], "passive_secretary_activity")
        description = schema["description"]
        self.assertIn("passive_secretary_search", description)
        self.assertIn("large", description.lower())
        self.assertIn("contacts", schema["parameters"]["properties"]["mode"]["enum"])

        search_description = controller_mod.EXACT_DATE_TOOL_SCHEMA["description"]
        self.assertIn("passive_secretary_activity", search_description)
        self.assertIn("next_cursor", search_description)
        self.assertIn("username", search_description.lower())

    def test_plugin_manifest_exposes_activity(self):
        manifest = (MODULE / "passive_secretary_plugin" / "plugin.yaml").read_text(
            encoding="utf-8"
        )
        self.assertIn("passive_secretary_activity", manifest)


if __name__ == "__main__":
    unittest.main()
