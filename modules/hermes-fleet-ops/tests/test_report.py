from __future__ import annotations

import json
import sys
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE))
import report
from store import AnalyticsStore


def _ingest(store, package_id, metric):
    package = {
        "package_id": package_id,
        "period_start": "2026-09-09T00:00:00Z",
        "period_end": "2026-09-10T00:00:00Z",
        "generated_at": "2026-09-09T12:00:00Z",
        "resource": {"hermes_version": "0.21.0"},
        "metrics": [metric],
    }
    store.ingest_package("pavel", package, "2026-09-09T12:01:00Z")


def test_summary_normalizes_v1_and_v2_tool_dimensions(tmp_path):
    store = AnalyticsStore(tmp_path / "analytics" / "db.sqlite3")
    _ingest(store, "v1-tool", {
        "name": "hermes.tool_call.count", "type": "counter",
        "dimensions": {"tool_family": "maton", "outcome": "success", "duration_bucket": "1s_to_5s"},
        "value": 2,
    })
    _ingest(store, "v2-tool", {
        "name": "hermes.tool_call.count", "type": "counter",
        "dimensions": {"tool_category": "mcp", "outcome": "failed", "latency_bucket": "1s_to_2s", "approval_outcome": "approved", "retry_count_bucket": "1"},
        "value": 3,
    })
    rows = report.summary(store)["tool_usage"]
    assert {row["tool_category"] for row in rows} == {"maton", "mcp"}
    v2 = next(row for row in rows if row["tool_category"] == "mcp")
    assert v2["latency_bucket"] == "1s_to_2s"
    assert v2["approval_outcome"] == "approved"
    assert v2["retry_count_bucket"] == "1"


def test_summary_exposes_bounded_v2_product_metrics(tmp_path):
    store = AnalyticsStore(tmp_path / "analytics" / "db.sqlite3")
    _ingest(store, "model", {"name": "hermes.model_route.count", "type": "counter", "dimensions": {"model": "gpt-5.6-sol", "provider": "openrouter"}, "value": 5})
    _ingest(store, "approval", {"name": "hermes.tool_approval.count", "type": "counter", "dimensions": {"attribution": "tool_call", "outcome": "approved"}, "value": 2})
    _ingest(store, "skill", {"name": "hermes.skill.lifecycle.count", "type": "counter", "dimensions": {"action": "installed", "provenance": "installed"}, "value": 1})
    _ingest(store, "task", {"name": "hermes.task_run.finished", "type": "counter", "dimensions": {"duration_bucket": "1s_to_5s", "end_reason": "completed", "entrypoint": "gateway_message", "execution_surface": "gateway", "model_call_count_bucket": "1", "outcome": "success", "retry_count_bucket": "0", "termination": "none", "tool_call_count_bucket": "2"}, "value": 7})
    data = report.summary(store)
    assert data["model_usage"][0]["metric"] == "hermes.model_route.count"
    assert data["model_usage"][0]["model"] == "gpt-5.6-sol"
    assert data["tool_approvals"][0]["outcome"] == "approved"
    assert data["skill_activity"][0]["action"] == "installed"
    assert data["task_outcomes"][0]["outcome"] == "success"
    serialized = json.dumps(data, sort_keys=True)
    assert "tool_args" not in serialized
    assert "tool_result" not in serialized
    assert "prompt" not in serialized
    assert "response_text" not in serialized
