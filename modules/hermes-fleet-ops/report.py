#!/usr/bin/env python3
"""Return bounded fleet/product analytics for Forge admin surfaces."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from store import AnalyticsStore


def summary(store: AnalyticsStore) -> dict:
    with store.connect() as db:
        health = db.execute("SELECT service_state, telegram_state, needs_attention FROM runtime_health").fetchall()
        versions = db.execute("SELECT release_id, COUNT(*) AS n FROM runtime_health GROUP BY release_id ORDER BY n DESC").fetchall()
        tools = db.execute("""
            SELECT dimensions_json, SUM(value) AS n
            FROM metric_counters
            WHERE metric_name='hermes.tool_call.count'
            GROUP BY dimensions_json ORDER BY n DESC
        """).fetchall()
        models = db.execute("""
            SELECT metric_name, dimensions_json, SUM(value) AS n
            FROM metric_counters
            WHERE metric_name IN ('hermes.model_call.count','hermes.model_route.count')
            GROUP BY metric_name, dimensions_json ORDER BY n DESC
        """).fetchall()
        approvals = db.execute("""
            SELECT dimensions_json, SUM(value) AS n
            FROM metric_counters
            WHERE metric_name='hermes.tool_approval.count'
            GROUP BY dimensions_json ORDER BY n DESC
        """).fetchall()
        skills = db.execute("""
            SELECT metric_name, dimensions_json, SUM(value) AS n
            FROM metric_counters
            WHERE metric_name IN ('hermes.skill.lifecycle.count','hermes.skill.load.count')
            GROUP BY metric_name, dimensions_json ORDER BY n DESC
        """).fetchall()
        tasks = db.execute("""
            SELECT dimensions_json, SUM(value) AS n
            FROM metric_counters
            WHERE metric_name='hermes.task_run.finished'
            GROUP BY dimensions_json ORDER BY n DESC
        """).fetchall()
        updates = db.execute("""
            SELECT outcome, COUNT(*) AS n
            FROM update_attempts GROUP BY outcome ORDER BY n DESC
        """).fetchall()
        issues = db.execute("""
            SELECT service_state, telegram_state, error_code, COUNT(*) AS n
            FROM health_events
            WHERE julianday(observed_at) >= julianday('now','-7 days')
              AND (service_state <> 'active' OR telegram_state <> 'connected' OR error_code <> '')
            GROUP BY service_state, telegram_state, error_code ORDER BY n DESC
        """).fetchall()
        rollouts = db.execute("SELECT * FROM rollout_state ORDER BY track, channel").fetchall()
        observability = db.execute("""
            SELECT o.*,
                   COALESCE((SELECT MAX(m.ingested_at) FROM metric_packages m
                             WHERE m.profile=o.profile), '') AS last_metric_package_at
            FROM profile_observability o ORDER BY o.profile
        """).fetchall()
        living_24h = db.execute("""
            SELECT COUNT(*) AS runs,
                   COALESCE(SUM(messages_scanned),0) AS messages,
                   COALESCE(SUM(accepted_operations),0) AS accepted,
                   COALESCE(SUM(rejected_operations),0) AS rejected,
                   COALESCE(SUM(primary_failures),0) AS primary_failures,
                   COALESCE(SUM(contract_retries),0) AS contract_retries
            FROM living_memory_runs
            WHERE mode='apply' AND outcome='success'
              AND julianday(observed_at) >= julianday('now','-1 day')
        """).fetchone()
        living_failed_7d = db.execute("""
            SELECT COUNT(*) AS n FROM living_memory_runs
            WHERE outcome='failed'
              AND julianday(observed_at) >= julianday('now','-7 days')
        """).fetchone()
        living_profiles = db.execute(
            "SELECT COUNT(DISTINCT profile) AS n FROM living_memory_runs"
        ).fetchone()
    healthy = sum(1 for row in health if row["service_state"] == "active" and row["telegram_state"] == "connected" and not row["needs_attention"])
    degraded = len(health) - healthy
    tool_rows = []
    for row in tools:
        dimensions = json.loads(row["dimensions_json"])
        tool_rows.append({
            "tool_category": dimensions.get("tool_category") or dimensions.get("tool_family", "other"),
            "outcome": dimensions.get("outcome", "unknown"),
            "latency_bucket": dimensions.get("latency_bucket") or dimensions.get("duration_bucket", "unknown"),
            "approval_outcome": dimensions.get("approval_outcome", "unknown"),
            "retry_count_bucket": dimensions.get("retry_count_bucket", "unknown"),
            "count": int(row["n"]),
        })
    observability_rows = []
    for raw in observability:
        row = dict(raw)
        for key in ("telemetry_enabled", "metrics_database_present"):
            row[key] = bool(row[key])
        observability_rows.append(row)
    living_memory = {
        "profiles_with_runs": int(living_profiles["n"] if living_profiles else 0),
        "successful_runs_24h": int(living_24h["runs"] if living_24h else 0),
        "failed_runs_7d": int(living_failed_7d["n"] if living_failed_7d else 0),
        "messages_reviewed_24h": int(living_24h["messages"] if living_24h else 0),
        "changes_accepted_24h": int(living_24h["accepted"] if living_24h else 0),
        "changes_rejected_24h": int(living_24h["rejected"] if living_24h else 0),
        "primary_failures_24h": int(living_24h["primary_failures"] if living_24h else 0),
        "fallback_contract_retries_24h": int(living_24h["contract_retries"] if living_24h else 0),
        "active_memories": sum(int(row["living_memory_active"]) for row in observability_rows),
        "hypotheses": sum(int(row["living_memory_hypothesis"]) for row in observability_rows),
    }
    telemetry_delivery = {
        "profiles_enabled": sum(1 for row in observability_rows if row["telemetry_enabled"]),
        "exporters_active": sum(1 for row in observability_rows if row["exporter_timer_state"] == "active"),
        "metrics_databases_present": sum(1 for row in observability_rows if row["metrics_database_present"]),
    }

    return {
        "fleet": {"profiles": len(health), "healthy": healthy, "degraded": degraded},
        "versions": [{"release_id": row["release_id"], "profiles": int(row["n"])} for row in versions],
        "tool_usage": tool_rows,
        "model_usage": [{"metric": row["metric_name"], **json.loads(row["dimensions_json"]), "count": int(row["n"])} for row in models],
        "tool_approvals": [{**json.loads(row["dimensions_json"]), "count": int(row["n"])} for row in approvals],
        "skill_activity": [{"metric": row["metric_name"], **json.loads(row["dimensions_json"]), "count": int(row["n"])} for row in skills],
        "task_outcomes": [{**json.loads(row["dimensions_json"]), "count": int(row["n"])} for row in tasks],
        "updates": [{"outcome": row["outcome"], "count": int(row["n"])} for row in updates],
        "health_issues_7d": [dict(row) for row in issues],
        "rollouts": [dict(row) for row in rollouts],
        "living_memory": living_memory,
        "telemetry_delivery": telemetry_delivery,
        "profiles": observability_rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("summary", choices=("summary",))
    parser.add_argument("--db", type=Path, default=None)
    args = parser.parse_args()
    try:
        store = AnalyticsStore(args.db) if args.db else AnalyticsStore()
        print(json.dumps(summary(store), ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        print("error=" + type(exc).__name__, file=__import__("sys").stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
