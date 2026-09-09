#!/usr/bin/env python3
"""Safely promote releases, assign profiles and resume Hermes rollout channels."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from store import AnalyticsStore
from updater import CHANNELS, POLICY_PATH, assignment, load_policy, verified_release


def atomic_policy(path: Path, payload: dict) -> None:
    fd, raw = tempfile.mkstemp(prefix=".hermes-fleet-policy.", dir=path.parent)
    temp = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chown(temp, 0, 0)
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def promote(policy: dict, track: str, channel: str, release_id: str) -> None:
    if channel not in {"canary", "preview", "stable"}:
        raise ValueError("promote_channel_invalid")
    verified_release(release_id)
    track_policy = policy.get("tracks", {}).get(track)
    if not isinstance(track_policy, dict) or not isinstance(track_policy.get(channel), dict):
        raise ValueError("track_channel_missing")
    track_policy[channel]["release_id"] = release_id


def assign(policy: dict, profile: str, track: str, channel: str, release_id: str = "") -> None:
    if channel not in CHANNELS:
        raise ValueError("assignment_channel_invalid")
    if not profile or not track:
        raise ValueError("assignment_invalid")
    data = {"track": track, "channel": channel}
    if channel == "pinned":
        verified_release(release_id)
        data["release_id"] = release_id
    else:
        if track not in policy.get("tracks", {}):
            raise ValueError("assignment_track_missing")
    policy.setdefault("profiles", {})[profile] = data
    assignment(profile, policy)


def resume(store: AnalyticsStore, track: str, channel: str) -> None:
    current = store.get_rollout(track, channel)
    if current is None:
        raise ValueError("rollout_state_missing")
    current["paused"] = False
    current["healthy_cycles"] = 0
    current["updated_at"] = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat().replace("+00:00", "Z")
    store.set_rollout(current)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, default=POLICY_PATH)
    sub = parser.add_subparsers(dest="command", required=True)
    p_promote = sub.add_parser("promote")
    p_promote.add_argument("--track", required=True)
    p_promote.add_argument("--channel", required=True)
    p_promote.add_argument("--release-id", required=True)
    p_assign = sub.add_parser("assign")
    p_assign.add_argument("--profile", required=True)
    p_assign.add_argument("--track", required=True)
    p_assign.add_argument("--channel", required=True)
    p_assign.add_argument("--release-id", default="")
    p_resume = sub.add_parser("resume")
    p_resume.add_argument("--track", required=True)
    p_resume.add_argument("--channel", required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        print("error=root_required", file=__import__("sys").stderr)
        return 1
    try:
        if args.command == "resume":
            resume(AnalyticsStore(), args.track, args.channel)
        else:
            policy = load_policy(args.policy)
            if args.command == "promote":
                promote(policy, args.track, args.channel, args.release_id)
            else:
                assign(policy, args.profile, args.track, args.channel, args.release_id)
            atomic_policy(args.policy, policy)
        print("ok=true")
        return 0
    except Exception as exc:
        print("error=" + type(exc).__name__, file=__import__("sys").stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
