from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE))
import collector
from store import AnalyticsStore


def package(metric=None):
    return {
        "schema_version": "hermes.shared_metrics.v1",
        "package_id": "123e4567-e89b-12d3-a456-426614174000",
        "install_id": "123e4567-e89b-12d3-a456-426614174001",
        "period_start": "2026-09-09T00:00:00Z",
        "period_end": "2026-09-10T00:00:00Z",
        "generated_at": "2026-09-09T12:00:00Z",
        "resource": {"hermes_version": "0.21.0"},
        "metrics": [metric or {
            "name": "hermes.tool_call.count",
            "type": "counter",
            "dimensions": {"duration_bucket": "1s_to_5s", "outcome": "success", "tool_family": "maton"},
            "value": 3,
        }],
    }


def test_package_validation_rejects_raw_or_unknown_dimensions():
    good = collector.validate_package(package())
    assert good["metrics"][0]["dimensions"]["tool_family"] == "maton"
    bad = package()
    bad["metrics"][0]["dimensions"]["tool_family"] = "private_customer_tool_name"
    try:
        collector.validate_package(bad)
    except ValueError as exc:
        assert str(exc) == "metric_dimension_value_invalid"
    else:
        raise AssertionError("unknown tool family must fail")


def test_central_store_does_not_persist_install_id_or_payload(tmp_path):
    store = AnalyticsStore(tmp_path / "analytics" / "db.sqlite3")
    payload = package()
    assert store.ingest_package("salavat", collector.validate_package(payload), "2026-09-09T12:01:00Z")
    assert not store.ingest_package("salavat", payload, "2026-09-09T12:02:00Z")
    with store.connect() as db:
        row = db.execute("SELECT * FROM metric_packages").fetchone()
        columns = set(row.keys())
        assert "install_id" not in columns
        metric = db.execute("SELECT * FROM metric_counters").fetchone()
        serialized = json.dumps(dict(metric))
    assert "maton" in serialized
    assert "install_id" not in serialized


def test_safe_error_never_preserves_arbitrary_message():
    assert collector.safe_error("telegram_auth_failed") == "telegram_auth_failed"
    assert collector.safe_error("secret token leaked in error") == "unknown_error"


def test_health_row_exports_only_bounded_gateway_fields(monkeypatch, tmp_path):
    owner = "testowner"
    home = tmp_path / owner / ".hermes"
    home.mkdir(parents=True)
    (home / "gateway_state.json").write_text(json.dumps({
        "pid": 111, "code_version": "0.21.0", "active_agents": 2,
        "sensitive": "do-not-export",
        "platforms": {"telegram": {
            "state": "connected", "error_code": None,
            "error_message": "do-not-export", "needs_attention": False,
        }},
    }))
    monkeypatch.setattr(collector, "HOME_ROOT", tmp_path)
    monkeypatch.setattr(collector, "service_state", lambda _owner: "active")
    row = collector.health_row({"owner": owner, "release_id": "hermes-test"})
    assert row["telegram_state"] == "connected"
    assert "error_message" not in row
    assert "sensitive" not in row


def test_health_history_records_only_state_changes(tmp_path):
    store = AnalyticsStore(tmp_path / "analytics" / "db.sqlite3")
    row = {
        "profile": "salavat", "release_id": "hermes-r1", "service_state": "active",
        "telegram_state": "connected", "error_code": "", "needs_attention": False,
        "code_version": "0.21.0", "active_agents": 0,
        "observed_at": "2026-09-09T12:00:00Z",
    }
    store.upsert_health(row)
    store.upsert_health({**row, "observed_at": "2026-09-09T12:05:00Z", "active_agents": 1})
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM health_events").fetchone()[0] == 1
    store.upsert_health({
        **row, "telegram_state": "error", "error_code": "telegram_auth_failed",
        "needs_attention": True, "observed_at": "2026-09-09T12:10:00Z",
    })
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM health_events").fetchone()[0] == 2
