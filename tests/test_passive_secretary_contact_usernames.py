from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "modules" / "passive-secretary"
CORE_PATCH = MODULE / "hermes-core-patch"
sys.path.insert(0, str(CORE_PATCH))
sys.path.insert(0, str(MODULE))

from passive_secretary_plugin.recall import HybridRecall
from passive_secretary_plugin.settings import Settings
from passive_secretary_plugin.retrieval import (
    normalize_source_username,
    render_sources_result,
    render_tool_result,
)


class PassiveSecretaryContactUsernameTests(unittest.TestCase):
    def test_username_normalizer_accepts_only_telegram_style_handles(self):
        self.assertEqual(normalize_source_username("@artemtol"), "@artemtol")
        self.assertEqual(normalize_source_username("artemtol"), "@artemtol")
        self.assertEqual(normalize_source_username("  @nastenaroderman  "), "@nastenaroderman")
        self.assertEqual(normalize_source_username("@bad-name"), "")
        self.assertEqual(normalize_source_username(""), "")
        self.assertEqual(normalize_source_username(None), "")

    def test_sources_surface_explicit_username(self):
        payload = json.loads(
            render_sources_result(
                [
                    {
                        "source_ref": "chat:abc123",
                        "chat_label": "Артем Т",
                        "source_username": "@artemtol",
                        "last_message_at": datetime(2026, 9, 18, 10, tzinfo=timezone.utc),
                        "message_count": 7,
                    }
                ],
                timezone_name="Europe/Moscow",
                query="Артем",
                status="resolved",
                limit=10,
                truncated=False,
            )
        )
        self.assertEqual(payload["sources"][0]["source_username"], "@artemtol")

    def test_search_record_surfaces_explicit_username_and_identity_contract(self):
        payload = json.loads(
            render_tool_result(
                [
                    {
                        "source_ref": "chat:abc123",
                        "chat_label": "Anastasiya Roderman",
                        "source_username": "@nastenaroderman",
                        "message_ref": "message:m1",
                        "sender_ref": "sender:s1",
                        "sender_label": "Anastasiya Roderman",
                        "direction": "incoming",
                        "body": "Привет",
                        "caption": None,
                        "content_kind": "text",
                        "attachments": [],
                        "media_transcripts": [],
                        "sent_at": datetime(2026, 9, 17, 9, 24, tzinfo=timezone.utc),
                        "edited_at": None,
                    }
                ],
                timezone_name="Europe/Moscow",
                ranges=[
                    (
                        datetime(2026, 9, 16, 21, tzinfo=timezone.utc),
                        datetime(2026, 9, 17, 21, tzinfo=timezone.utc),
                    )
                ],
                has_more=False,
                now=datetime(2026, 9, 18, 12, tzinfo=timezone.utc),
                cursor_factory=lambda _ref: "cursor",
            )
        )
        self.assertEqual(payload["records"][0]["source_username"], "@nastenaroderman")
        identity = payload["analysis_contract"]["identity"].lower()
        self.assertIn("username", identity)
        self.assertIn("never infer", identity)

    def test_recall_render_surfaces_username_and_identity_contract(self):
        settings = Settings(
            tenant_id="tester",
            source_id="telegram_business",
            test_run_id="",
            owner_telegram_user_ids=("1",),
            postgres_dsn_env="PASSIVE_SECRETARY_DATABASE_URL",
            source_ref_key_env="PASSIVE_SECRETARY_SOURCE_REF_KEY",
            retention_days=365,
            timezone="Europe/Moscow",
        )
        engine = HybridRecall(settings, archive=object())
        payload = json.loads(
            engine._render(
                [
                    {
                        "source_ref": "chat:abc123",
                        "chat_label": "Anastasiya Roderman",
                        "source_username": "@nastenaroderman",
                        "message_ref": "message:m1",
                        "sender_label": "Anastasiya Roderman",
                        "body": "2143 руб.",
                        "caption": None,
                        "content_kind": "text",
                        "attachment": {},
                        "media_transcripts": [],
                        "ingest_origin": "business_update",
                        "sent_at": datetime(2026, 9, 17, 9, 24, tzinfo=timezone.utc),
                        "match_score": 0.9,
                        "fts_rank": 0.8,
                        "fuzzy_rank": 0.0,
                        "exact_boost": 1.0,
                    }
                ],
                query="2143",
                mode="fts",
                origin="any",
                start=None,
                end=None,
            )
        )
        self.assertEqual(payload["records"][0]["source_username"], "@nastenaroderman")
        self.assertIn("never infer", payload["analysis_contract"]["identity"].lower())


if __name__ == "__main__":
    unittest.main()
