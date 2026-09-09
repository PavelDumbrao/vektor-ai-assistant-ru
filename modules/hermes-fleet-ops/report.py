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
