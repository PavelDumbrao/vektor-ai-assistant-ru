from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from unittest import mock
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "modules" / "passive-secretary" / "passive_secretary_plugin"


def load_submodule(name: str):
    package_name = "recall_test_plugin"
    if package_name not in sys.modules:
        import types

        package = types.ModuleType(package_name)
        package.__path__ = [str(PLUGIN)]
        sys.modules[package_name] = package
    full_name = f"{package_name}.{name}"
    spec = importlib.util.spec_from_file_location(full_name, PLUGIN / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


settings_mod = load_submodule("settings")
load_submodule("retrieval")
load_submodule("archive")
recall = load_submodule("recall")


class RecallInputTests(unittest.TestCase):
    def test_query_is_required_and_bounded(self):
        with self.assertRaisesRegex(recall.RecallInputError, "query is required"):
            recall._query("   ")
        self.assertEqual(recall._query("  продажи   магазина "), "продажи магазина")

    def test_moscow_date_bounds_are_half_open(self):
        start, end = recall._bounds({"date": "2026-09-09"}, "Europe/Moscow")
        self.assertEqual(start, datetime(2026, 9, 8, 21, 0, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 9, 9, 21, 0, tzinfo=timezone.utc))

    def test_modes_and_origins_fail_closed(self):
        self.assertEqual(recall._mode(None), "hybrid")
        self.assertEqual(recall._origin(None), "any")
        with self.assertRaises(recall.RecallInputError):
            recall._mode("semantic-magic")
        with self.assertRaises(recall.RecallInputError):
            recall._origin("everything")


class _FakeConn:
    def cursor(self):
        return object()


class _FakeArchive:
    def ensure_schema(self):
        return None

    def _connect(self):
        return _FakeConn()

    def _close(self, _conn, _cursor=None):
        return None


def _settings():
    return settings_mod.Settings(
        tenant_id="tester",
        source_id="telegram_business",
        test_run_id="",
        owner_telegram_user_ids=("1",),
        postgres_dsn_env="PASSIVE_SECRETARY_DATABASE_URL",
        source_ref_key_env="PASSIVE_SECRETARY_SOURCE_REF_KEY",
        retention_days=365,
    )


class RecallHybridCascadeTests(unittest.TestCase):
    def test_hybrid_skips_fuzzy_when_fts_fills_limit(self):
        engine = recall.HybridRecall(_settings(), _FakeArchive())
        rows = [{"message_ref": f"message:{i}"} for i in range(3)]
        with mock.patch.object(engine, "_execute", return_value=rows) as execute, \
             mock.patch.object(engine, "_render", return_value="ok"):
            self.assertEqual(engine.search({"query": "задача", "limit": 3}, owner_id="1"), "ok")
        self.assertEqual([call.kwargs["mode"] for call in execute.call_args_list], ["fts"])

    def test_hybrid_uses_fuzzy_only_when_fts_is_short(self):
        engine = recall.HybridRecall(_settings(), _FakeArchive())
        fts = [{"message_ref": "message:1", "match_score": 0.7, "sent_at": datetime.now(timezone.utc)}]
        fuzzy = [{"message_ref": "message:2", "match_score": 0.4, "sent_at": datetime.now(timezone.utc)}]
        with mock.patch.object(engine, "_execute", side_effect=[fts, fuzzy]) as execute, \
             mock.patch.object(engine, "_render", return_value="ok"):
            self.assertEqual(engine.search({"query": "лоялност", "limit": 3}, owner_id="1"), "ok")
        self.assertEqual([call.kwargs["mode"] for call in execute.call_args_list], ["fts", "fuzzy"])


class RecallReleaseContractTests(unittest.TestCase):
    def test_plugin_manifest_exposes_recall(self):
        manifest = (PLUGIN / "plugin.yaml").read_text(encoding="utf-8")
        self.assertIn("passive_secretary_recall", manifest)
    def test_schema_enables_fuzzy_and_full_text_indexes(self):
        schema = (PLUGIN / "schema.sql").read_text(encoding="utf-8")
        self.assertIn("CREATE EXTENSION IF NOT EXISTS pg_trgm", schema)
        self.assertIn("messages_search_fts_idx", schema)
        self.assertIn("messages_search_trgm_idx", schema)
        self.assertIn("media_transcript_fts_idx", schema)
        self.assertIn("media_transcript_trgm_idx", schema)

    def test_recall_tool_keeps_history_and_live_distinct(self):
        schema = recall.RECALL_TOOL_SCHEMA
        props = schema["parameters"]["properties"]
        self.assertEqual(props["origin"]["enum"], ["any", "live", "history"])
        self.assertIn("query", schema["parameters"]["required"])


class TrustedInviterPatchTests(unittest.TestCase):
    def test_patch_only_starts_owner_consent_for_trusted_inviter(self):
        patch = (
            ROOT
            / "modules"
            / "shared-runtime"
            / "releases"
            / "v0.21.0"
            / "trusted-group-inviter.patch"
        ).read_text(encoding="utf-8")
        self.assertIn("group_passive_trusted_inviter_ids", patch)
        self.assertIn("actor_id != owner_id and actor_id not in trusted_inviters", patch)
        self.assertIn("owner", patch.lower())
        self.assertNotIn("registry.approve", patch)


if __name__ == "__main__":
    unittest.main()
