#!/usr/bin/env python3
"""Progressive, rollback-safe Fleet Update Manager for Hermes Forge."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from collector import health_row, load_profiles
from store import AnalyticsStore

POLICY_PATH = Path("/etc/proai-hermes-fleet-policy.json")
RELEASE_ROOT = Path("/opt/vektor/releases")
UPGRADE = Path("/opt/vektor/admin/upgrades/v0.21.0/upgrade_profile.py")
OWNER_RE = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
RELEASE_RE = re.compile(r"^hermes-[A-Za-z0-9_.-]{1,80}$")
CHANNELS = frozenset({"canary", "preview", "stable", "pinned"})


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_policy(path: Path = POLICY_PATH) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("fleet_policy_missing")
    if path.stat().st_uid != 0 or path.stat().st_mode & 0o022:
        raise ValueError("fleet_policy_unsafe")
    policy = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(policy, dict) or policy.get("schema_version") != 1:
        raise ValueError("fleet_policy_invalid")
    if not isinstance(policy.get("enabled"), bool):
        raise ValueError("fleet_policy_enabled_invalid")
    if not isinstance(policy.get("tracks"), dict) or not isinstance(policy.get("profiles"), dict):
        raise ValueError("fleet_policy_shape_invalid")
    return policy


def verified_release(release_id: str) -> None:
    if not RELEASE_RE.fullmatch(release_id):
        raise ValueError("release_id_invalid")
    path = RELEASE_ROOT / release_id / "runtime.json"
    if path.is_symlink() or not path.is_file():
        raise ValueError("release_missing")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("state") != "ready" or payload.get("release_id") != release_id:
        raise ValueError("release_not_ready")
    if payload.get("schema_rollback_compatible") is not True:
        raise ValueError("release_not_rollback_compatible")


def assignment(owner: str, policy: dict[str, Any]) -> dict[str, str] | None:
    raw = policy["profiles"].get(owner)
    if raw is None and re.fullmatch(r"h[1-9][0-9]{4,18}", owner):
        raw = policy.get("defaults", {}).get("forge_new")
    if not isinstance(raw, dict):
        return None
    track = str(raw.get("track") or "")
    channel = str(raw.get("channel") or "")
    if not track or channel not in CHANNELS:
        raise ValueError("profile_assignment_invalid")
    result = {"track": track, "channel": channel}
    if channel == "pinned":
        release_id = str(raw.get("release_id") or "")
        verified_release(release_id)
        result["release_id"] = release_id
    return result


def channel_policy(policy: dict[str, Any], track: str, channel: str) -> dict[str, Any]:
    track_policy = policy["tracks"].get(track)
    if not isinstance(track_policy, dict):
        raise ValueError("track_missing")
    raw = track_policy.get(channel)
    if not isinstance(raw, dict):
        raise ValueError("channel_missing")
    release_id = str(raw.get("release_id") or "")
    verified_release(release_id)
    stages = raw.get("stages", [100])
    if not isinstance(stages, list) or not stages or any(type(x) is not int or x < 1 or x > 100 for x in stages):
        raise ValueError("rollout_stages_invalid")
    stages = sorted(set(stages))
    if stages[-1] != 100:
        stages.append(100)
    healthy_cycles = int(raw.get("healthy_cycles_to_advance", 2))
    if healthy_cycles < 1 or healthy_cycles > 24:
        raise ValueError("healthy_cycles_invalid")
    return {"release_id": release_id, "stages": stages, "healthy_cycles_to_advance": healthy_cycles}


def rollout_state(store: AnalyticsStore, track: str, channel: str, config: dict[str, Any]) -> dict[str, Any]:
    current = store.get_rollout(track, channel)
    release_id = config["release_id"]
    if current is None or current["release_id"] != release_id:
        current = {
            "track": track,
            "channel": channel,
            "release_id": release_id,
            "rollout_percent": config["stages"][0],
            "healthy_cycles": 0,
            "paused": False,
            "updated_at": now(),
        }
        store.set_rollout(current)
    else:
        current["paused"] = bool(current["paused"])
    return current


def cohort(owners: list[str], percent: int) -> list[str]:
    if not owners or percent <= 0:
        return []
    ranked = sorted(
        owners,
        key=lambda owner: hashlib.sha256(owner.encode("utf-8")).hexdigest(),
    )
    count = max(1, math.ceil(len(ranked) * min(100, percent) / 100))
    return ranked[:count]


def classify_upgrade_failure(stderr: str) -> tuple[str, str]:
    safe = stderr[-4000:]
    if "profile_not_idle_or_identity_changed" in safe or "profile_changed_before_stop" in safe:
        return "deferred", "profile_busy"
    if "database_compatibility_not_verified" in safe:
        return "failed", "database_compatibility"
    if "release_not_verified" in safe:
        return "failed", "release_not_verified"
    return "failed", "upgrade_failed"


def run_upgrade(owner: str, release_id: str) -> tuple[str, str]:
    base = [
        "/usr/bin/python3", str(UPGRADE),
        "--owner", owner, "--release-id", release_id,
    ]
    preflight = subprocess.run(base, capture_output=True, text=True, timeout=300, check=False)
    if preflight.returncode:
        return classify_upgrade_failure(preflight.stderr)
    applied = subprocess.run([*base, "--apply"], capture_output=True, text=True, timeout=600, check=False)
    if applied.returncode:
        return classify_upgrade_failure(applied.stderr)
    return "success", ""


def record_attempt(
    store: AnalyticsStore, *, owner: str, track: str, channel: str,
    from_release: str, to_release: str, outcome: str, error_code: str,
    started_at: str,
) -> None:
    store.record_update_attempt({
        "attempt_id": str(uuid.uuid4()),
        "profile": owner,
        "track": track,
        "channel": channel,
        "from_release": from_release,
        "to_release": to_release,
        "outcome": outcome,
        "error_code": error_code,
        "started_at": started_at,
        "finished_at": now(),
    })


def profile_healthy(profile: dict[str, str]) -> bool:
    row = health_row(profile)
    return (
        row["service_state"] == "active"
        and row["telegram_state"] == "connected"
        and not row["needs_attention"]
    )


def advance_rollout(store: AnalyticsStore, state: dict[str, Any], config: dict[str, Any]) -> None:
    stages = config["stages"]
    current = int(state["rollout_percent"])
    try:
        index = stages.index(current)
    except ValueError:
        index = 0
    state["healthy_cycles"] = int(state.get("healthy_cycles", 0)) + 1
    if state["healthy_cycles"] >= config["healthy_cycles_to_advance"] and index < len(stages) - 1:
        state["rollout_percent"] = stages[index + 1]
        state["healthy_cycles"] = 0
    state["updated_at"] = now()
    store.set_rollout(state)


def run_channel(
    store: AnalyticsStore,
    policy: dict[str, Any],
    profiles: list[dict[str, str]],
    track: str,
    channel: str,
) -> dict[str, int]:
    config = channel_policy(policy, track, channel)
    state = rollout_state(store, track, channel, config)
    if state["paused"]:
        return {"updated": 0, "deferred": 0, "failed": 0, "paused": 1}

    assigned = []
    for profile in profiles:
        item = assignment(profile["owner"], policy)
        if item and item["track"] == track and item["channel"] == channel:
            assigned.append(profile)
    eligible_names = set(cohort([item["owner"] for item in assigned], int(state["rollout_percent"])))
    eligible = [item for item in assigned if item["owner"] in eligible_names]
    updated = deferred = failed = 0
    target = config["release_id"]
    for profile in eligible:
        if profile["release_id"] == target:
            continue
        started = now()
        outcome, error_code = run_upgrade(profile["owner"], target)
        record_attempt(
            store, owner=profile["owner"], track=track, channel=channel,
            from_release=profile["release_id"], to_release=target,
            outcome=outcome, error_code=error_code, started_at=started,
        )
        if outcome == "success":
            profile["release_id"] = target
            updated += 1
        elif outcome == "deferred":
            deferred += 1
        else:
            failed += 1
            state["paused"] = True
            state["healthy_cycles"] = 0
            state["updated_at"] = now()
            store.set_rollout(state)
            break

    if failed == 0 and deferred == 0 and eligible:
        all_current = all(item["release_id"] == target for item in eligible)
        all_healthy = all(profile_healthy(item) for item in eligible)
        if all_current and all_healthy:
            advance_rollout(store, state, config)
        else:
            state["healthy_cycles"] = 0
            state["updated_at"] = now()
            store.set_rollout(state)
    return {"updated": updated, "deferred": deferred, "failed": failed, "paused": int(bool(state["paused"]))}


def run_pinned(store: AnalyticsStore, policy: dict[str, Any], profiles: list[dict[str, str]]) -> dict[str, int]:
    """Observe pinned profiles without ever mutating their runtime binding.

    Pinned is the escape hatch for compatibility holds, pilots and operator-led
    changes. A release mismatch is therefore drift to surface, not desired state
    for the automatic updater to enforce. This prevents a stale Fleet policy from
    undoing a separately verified/manual rollout.
    """
    del store  # Pinned observation intentionally creates no update attempt.
    drifted = 0
    for profile in profiles:
        item = assignment(profile["owner"], policy)
        if not item or item["channel"] != "pinned":
            continue
        if profile["release_id"] != item["release_id"]:
            drifted += 1
    return {"updated": 0, "deferred": 0, "failed": 0, "pinned_drift": drifted}


def update_fleet(store: AnalyticsStore, policy: dict[str, Any]) -> dict[str, Any]:
    profiles = load_profiles()
    if not policy["enabled"]:
        return {"enabled": False, "profiles": len(profiles), "updated": 0, "deferred": 0, "failed": 0}
    totals = run_pinned(store, policy, profiles)
    paused = 0
    seen = set()
    for profile in profiles:
        item = assignment(profile["owner"], policy)
        if not item or item["channel"] == "pinned":
            continue
        key = (item["track"], item["channel"])
        if key in seen:
            continue
        seen.add(key)
        result = run_channel(store, policy, profiles, *key)
        for name in ("updated", "deferred", "failed"):
            totals[name] += result[name]
        paused += result["paused"]
    return {"enabled": True, "profiles": len(profiles), **totals, "paused_channels": paused}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("update", choices=("update",))
    parser.add_argument("--policy", type=Path, default=POLICY_PATH)
    parser.add_argument("--db", type=Path, default=None)
    args = parser.parse_args()
    if os.geteuid() != 0:
        print("error=root_required", file=__import__("sys").stderr)
        return 1
    try:
        policy = load_policy(args.policy)
        store = AnalyticsStore(args.db) if args.db else AnalyticsStore()
        result = update_fleet(store, policy)
        print(json.dumps({"ok": True, **result}, sort_keys=True))
        return 0
    except Exception as exc:
        print("error=" + type(exc).__name__, file=__import__("sys").stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
