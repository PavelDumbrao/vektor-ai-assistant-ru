#!/usr/bin/env python3
"""Collect privacy-bounded Hermes metrics and fleet health into Forge analytics."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from store import AnalyticsStore

PROFILE_ROOT = Path("/opt/vektor/profiles")
HOME_ROOT = Path("/home")
OWNER_RE = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
RELEASE_RE = re.compile(r"^hermes-[A-Za-z0-9_.-]{1,80}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
SAFE_ERROR_RE = re.compile(r"^[A-Za-z0-9_.:-]{0,80}$")
SAFE_VERSION_RE = re.compile(r"^[A-Za-z0-9_.+-]{1,64}$")
MAX_PACKAGE_BYTES = 1024 * 1024

COUNT_BUCKETS = {"0", "1", "2", "3_to_5", "6_to_10", "gte_11"}
DURATION_BUCKETS = {"lt_1s", "1s_to_5s", "5s_to_30s", "30s_to_2m", "2m_to_10m", "gte_10m"}
EXECUTION_SURFACES = {"api", "batch", "cli", "desktop", "gateway", "other", "python", "scheduled_task", "tui", "unknown"}
TASK_ENTRYPOINTS = {"api", "background", "batch", "delegated", "gateway_message", "interactive", "other", "python", "scheduled_task", "unknown"}
MODEL_FAMILIES = {"claude", "deepseek", "gemini", "gemma", "glm", "gpt", "grok", "kimi", "llama", "minimax", "mimo", "mistral", "nemotron", "nova", "o1", "o3", "o4", "qwen", "step", "trinity", "unknown"}
TOOL_FAMILIES = {"browser", "cron", "delegation", "files", "image_gen", "maton", "memory", "other", "passive_secretary", "terminal", "video_editor", "web_search"}

METRIC_DIMENSIONS = {
    "hermes.model_call.count": {
        "call_role": {"primary"}, "locality": {"local", "remote", "unknown"},
        "model_family": MODEL_FAMILIES, "outcome": {"cancelled", "failed", "success"},
        "provider_family": {"aggregator", "custom", "direct", "local", "unknown"},
    },
    "hermes.task_run.started": {
        "entrypoint": TASK_ENTRYPOINTS, "execution_surface": EXECUTION_SURFACES,
    },
    "hermes.task_run.finished": {
        "duration_bucket": DURATION_BUCKETS,
        "end_reason": {"approval_denied", "completed", "failed", "guardrail_blocked", "iteration_limit", "system_aborted", "timed_out", "unknown", "user_cancelled"},
        "entrypoint": TASK_ENTRYPOINTS, "execution_surface": EXECUTION_SURFACES,
        "model_call_count_bucket": COUNT_BUCKETS,
        "outcome": {"cancelled", "failed", "success", "timed_out", "unknown"},
        "retry_count_bucket": COUNT_BUCKETS,
        "termination": {"none", "system_aborted", "timed_out", "unknown", "user_cancelled"},
        "tool_call_count_bucket": COUNT_BUCKETS,
    },
    "hermes.tool_call.count": {
        "duration_bucket": DURATION_BUCKETS,
        "outcome": {"cancelled", "failed", "success"},
        "tool_family": TOOL_FAMILIES,
    },
}



def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_profiles(root: Path = PROFILE_ROOT) -> list[dict[str, str]]:
    profiles = []
    for path in sorted(root.glob("*.json")):
        if path.is_symlink() or not path.is_file():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        owner = str(payload.get("owner") or "")
        release_id = str(payload.get("release_id") or "")
        if not OWNER_RE.fullmatch(owner) or not RELEASE_RE.fullmatch(release_id):
            continue
        profiles.append({"owner": owner, "release_id": release_id})
    return profiles


def validate_package(payload: Any) -> dict[str, Any]:
    required = {"schema_version", "package_id", "install_id", "period_start", "period_end", "generated_at", "resource", "metrics"}
    if not isinstance(payload, dict) or set(payload) != required:
        raise ValueError("package_shape_invalid")
    if payload["schema_version"] != "hermes.shared_metrics.v1":
        raise ValueError("package_schema_invalid")
    if not UUID_RE.fullmatch(str(payload["package_id"])) or not UUID_RE.fullmatch(str(payload["install_id"])):
        raise ValueError("package_id_invalid")
    resource = payload["resource"]
    if not isinstance(resource, dict) or set(resource) != {"hermes_version"}:
        raise ValueError("resource_invalid")
    version = str(resource["hermes_version"])
    if not version or len(version) > 64:
        raise ValueError("version_invalid")
    metrics = payload["metrics"]
    if not isinstance(metrics, list) or not metrics:
        raise ValueError("metrics_invalid")
    for metric in metrics:
        if not isinstance(metric, dict) or set(metric) != {"name", "type", "dimensions", "value"}:
            raise ValueError("metric_shape_invalid")
        name = str(metric["name"])
        contract = METRIC_DIMENSIONS.get(name)
        dimensions = metric["dimensions"]
        if contract is None or metric["type"] != "counter":
            raise ValueError("metric_name_invalid")
        if not isinstance(dimensions, dict) or set(dimensions) != set(contract):
            raise ValueError("metric_dimensions_invalid")
        if any(not isinstance(value, str) or value not in contract[key] for key, value in dimensions.items()):
            raise ValueError("metric_dimension_value_invalid")
        if not isinstance(metric["value"], int) or isinstance(metric["value"], bool) or metric["value"] <= 0:
            raise ValueError("metric_value_invalid")
    for field in ("period_start", "period_end", "generated_at"):
        if not isinstance(payload[field], str) or len(payload[field]) > 64:
            raise ValueError("timestamp_invalid")
    return payload


def ingest_outbox(store: AnalyticsStore, profile: str, home: Path) -> tuple[int, int]:
    outbox = home / "telemetry" / "shared_metrics" / "outbox"
    if not outbox.is_dir() or outbox.is_symlink():
        return 0, 0
    ingested = rejected = 0
    for path in sorted(outbox.glob("*.json")):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_PACKAGE_BYTES:
            rejected += 1
            continue
        try:
            package = validate_package(json.loads(path.read_text(encoding="utf-8")))
            ingested += 1 if store.ingest_package(profile, package, utc_now()) else 0
        except Exception:
            rejected += 1
    return ingested, rejected


def service_state(owner: str) -> str:
    result = subprocess.run(
        ["/usr/bin/systemctl", "is-active", f"{owner}-hermes.service"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    value = result.stdout.strip().lower()
    return value if value in {"active", "inactive", "failed", "activating", "deactivating"} else "unknown"


def safe_error(value: Any) -> str:
    text = str(value or "")[:80]
    return text if SAFE_ERROR_RE.fullmatch(text) else ("unknown_error" if text else "")


def health_row(profile: dict[str, str]) -> dict[str, Any]:
    owner = profile["owner"]
    gateway_path = HOME_ROOT / owner / ".hermes" / "gateway_state.json"
    gateway: dict[str, Any] = {}
    if gateway_path.is_file() and not gateway_path.is_symlink():
        try:
            gateway = json.loads(gateway_path.read_text(encoding="utf-8"))
        except Exception:
            gateway = {}
    telegram = (gateway.get("platforms") or {}).get("telegram") or {}
    telegram_state = str(telegram.get("state") or "unknown").lower()
    if telegram_state not in {"connected", "disconnected", "connecting", "error", "unknown"}:
        telegram_state = "unknown"
    return {
        "profile": owner,
        "release_id": profile["release_id"],
        "service_state": service_state(owner),
        "telegram_state": telegram_state,
        "error_code": safe_error(telegram.get("error_code")),
        "needs_attention": bool(telegram.get("needs_attention", False)),
        "code_version": (lambda value: value if SAFE_VERSION_RE.fullmatch(value) else "unknown")(str(gateway.get("code_version") or "unknown")[:64]),
        "active_agents": max(0, int(gateway.get("active_agents") or 0)) if str(gateway.get("active_agents") or "0").isdigit() else 0,
        "observed_at": utc_now(),
    }


def collect(store: AnalyticsStore, profile_root: Path = PROFILE_ROOT) -> dict[str, int]:
    packages = rejected = 0
    profiles = load_profiles(profile_root)
    for profile in profiles:
        owner = profile["owner"]
        home = HOME_ROOT / owner / ".hermes"
        if not home.is_dir() or home.is_symlink():
            continue
        accepted, denied = ingest_outbox(store, owner, home)
        packages += accepted
        rejected += denied
        try:
            row = health_row(profile)
        except Exception:
            row = {
                "profile": owner, "release_id": profile["release_id"],
                "service_state": "unknown", "telegram_state": "unknown",
                "error_code": "collector_parse_failed", "needs_attention": True,
                "code_version": "unknown", "active_agents": 0, "observed_at": utc_now(),
            }
        store.upsert_health(row)
    return {"profiles": len(profiles), "packages_ingested": packages, "packages_rejected": rejected}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("collect", choices=("collect",))
    parser.add_argument("--db", type=Path, default=None)
    args = parser.parse_args()
    if __import__("os").geteuid() != 0:
        print("error=root_required", file=__import__("sys").stderr)
        return 1
    try:
        store = AnalyticsStore(args.db) if args.db else AnalyticsStore()
        result = collect(store)
        print(json.dumps({"ok": True, **result}, sort_keys=True))
        return 0
    except Exception as exc:
        print("error=" + type(exc).__name__, file=__import__("sys").stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
