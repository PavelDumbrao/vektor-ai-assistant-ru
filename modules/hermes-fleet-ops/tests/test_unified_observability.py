from __future__ import annotations

import json
import os
import sys
import pytest
from pathlib import Path
from types import SimpleNamespace

MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE))
import collector
import report
import store as store_module
from store import AnalyticsStore


def lm_record(run_id="lm-123456789abc", **extra):
    value = {
        "run_id": run_id, "owner": "pavel", "mode": "apply", "ok": True,
        "observed_at": "2026-09-13T05:00:00+00:00",
        "messages_scanned": 12, "accepted_operations": 2,
        "rejected_operations": 1, "primary_failures": 1,
        "contract_retries": 1, "cursor_advanced": True,
        "after": {"active": 4, "hypothesis": 1},
    }
    value.update(extra)
    return value


def test_living_memory_record_exports_only_bounded_aggregates():
    raw = lm_record(secret_memory_text="never centralize me", routes=[{"private": "x"}])
    row = collector._living_memory_record("pavel", raw, "2026-09-13T06:00:00Z")
    serialized = json.dumps(row, sort_keys=True)
    assert row["messages_scanned"] == 12
    assert row["active_memories"] == 4
    assert row["hypothesis_memories"] == 1
    assert "never centralize me" not in serialized
    assert "routes" not in serialized
    assert "secret_memory_text" not in serialized


def test_living_memory_record_rejects_wrong_owner_and_unbounded_counts():
    for raw in (
        lm_record(owner="bebov"),
        lm_record(messages_scanned=-1),
        lm_record(messages_scanned=1_000_000_001),
        lm_record(mode="arbitrary"),
    ):
        try:
            collector._living_memory_record("pavel", raw, "2026-09-13T06:00:00Z")
        except ValueError:
            pass
        else:
            raise AssertionError("unsafe Living Memory audit row must be rejected")


def test_old_living_memory_record_uses_file_time_fallback():
    raw = lm_record()
    raw.pop("observed_at")
    raw.pop("ok")
    row = collector._living_memory_record("pavel", raw, "2026-09-13T06:00:00Z")
    assert row["outcome"] == "success"
    assert row["observed_at"] == "2026-09-13T06:00:00Z"


def test_collect_living_memory_deduplicates_without_persisting_text(monkeypatch, tmp_path):
    home = tmp_path / "pavel" / ".hermes"
    root = home / "living_memory"
    root.mkdir(parents=True)
    audit = root / "audit.jsonl"
    raw = lm_record(secret_memory_text="private-user-memory")
    audit.write_text(json.dumps(raw) + "\n", encoding="utf-8")
    audit.chmod(0o600)
    uid = os.getuid()
    monkeypatch.setattr(collector, "_profile_uid", lambda _owner: uid)
    store = AnalyticsStore(tmp_path / "analytics" / "db.sqlite3")
    first = collector.collect_living_memory(store, "pavel", home)
    second = collector.collect_living_memory(store, "pavel", home)
    assert first[0] == 1 and second[0] == 0
    with store.connect() as db:
        rows = [dict(x) for x in db.execute("SELECT * FROM living_memory_runs")]
    serialized = json.dumps(rows, sort_keys=True)
    assert len(rows) == 1
    assert "private-user-memory" not in serialized


def test_failed_living_memory_run_keeps_only_failure_status():
    raw = {"run_id": "lm-abcdef123456", "owner": "pavel", "mode": "apply",
           "ok": False, "error_type": "ProviderSecretError",
           "observed_at": "2026-09-13T05:00:00Z", "messages_scanned": 99}
    row = collector._living_memory_record("pavel", raw, "2026-09-13T06:00:00Z")
    assert row["outcome"] == "failed"
    assert row["messages_scanned"] == 0
    assert "ProviderSecretError" not in json.dumps(row)


def test_unified_report_contains_memory_and_delivery_without_content(tmp_path):
    store = AnalyticsStore(tmp_path / "analytics" / "db.sqlite3")
    row = collector._living_memory_record("pavel", lm_record(), "2026-09-13T06:00:00Z")
    store.record_living_memory_run(row)
    store.upsert_observability({
        "profile": "pavel", "telemetry_enabled": True,
        "metrics_database_present": True, "exporter_timer_state": "active",
        "living_memory_timer_state": "active",
        "living_memory_last_run_at": "2026-09-13T05:00:00Z",
        "living_memory_last_outcome": "success", "living_memory_active": 4,
        "living_memory_hypothesis": 1, "video_editor_version": "0.6.0",
        "observed_at": "2026-09-13T05:05:00Z",
    })
    data = report.summary(store)
    assert data["living_memory"]["active_memories"] == 4
    assert data["living_memory"]["hypotheses"] == 1
    assert data["telemetry_delivery"]["profiles_enabled"] == 1
    assert data["telemetry_delivery"]["exporters_active"] == 1
    assert data["profiles"][0]["video_editor_version"] == "0.6.0"
    serialized = json.dumps(data, sort_keys=True)
    for forbidden in ("private-user-memory", "prompt", "response_text", "memory_text"):
        assert forbidden not in serialized


def test_store_deduplicates_living_memory_run(tmp_path):
    store = AnalyticsStore(tmp_path / "analytics" / "db.sqlite3")
    row = collector._living_memory_record("pavel", lm_record(), "2026-09-13T06:00:00Z")
    assert store.record_living_memory_run(row) is True
    assert store.record_living_memory_run(row) is False


def test_production_analytics_root_still_requires_root(monkeypatch, tmp_path):
    monkeypatch.setattr(store_module.os, "geteuid", lambda: 1001)
    with pytest.raises(store_module.StoreError, match="analytics_root_requires_root"):
        store_module._secure_root(tmp_path / "prod", root_owned=True)


def test_explicit_diagnostic_store_does_not_chown_root(monkeypatch, tmp_path):
    def forbidden_chown(*_args, **_kwargs):
        raise AssertionError("explicit diagnostic DB must not chown root")
    monkeypatch.setattr(store_module.os, "chown", forbidden_chown)
    store = AnalyticsStore(tmp_path / "analytics" / "db.sqlite3")
    assert store.path.is_file()
