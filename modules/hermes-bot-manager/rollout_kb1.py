#!/usr/bin/env python3
"""Switch profiles to their `kb1` release (env-overridable OpenRouter STT endpoint) with the fleet's own tool (VPS, root).

  rollout_kb1.py --owner X [--owner Y] [--apply] [--purpose P] [--promote-channel C]

Steps per profile:
1. Target release lookup via runtime.json (parent_release_id == current, verification.purpose contains purpose).
2. For pavel: refuse while /opt/vektor/video-editor/runtime.lock is held.
3. upgrade_profile.py preflight -> --apply (stop, repoint symlink, register, start, rollback on failure).
   apply timeout is None because upgrade_profile has its own rollback mechanism.
4. Policy update: pinned -> policyctl assign; stable/other -> policyctl promote.
   Profiles not in policy['profiles'] require explicit --promote-channel.
   Policy is updated even if profile is already on the target release (resume after crash).
Failed/busy profiles do not abort the batch; errors are recorded and exit code 1 is returned at the end.
"""
from __future__ import annotations

import argparse
import json
import os
import pwd
import subprocess
import sys
import time
from pathlib import Path

UPGRADE = "/opt/vektor/admin/upgrades/v0.21.0/upgrade_profile.py"
POLICYCTL = "/opt/proai-hermes-fleet-ops/policyctl.py"
POLICY = Path("/etc/proai-hermes-fleet-policy.json")
LOCK = Path("/opt/vektor/video-editor/runtime.lock")
RELEASES_DIR = Path("/opt/vektor/releases")


def sh(cmd: list[str], timeout: int | None = 900) -> tuple[int, str]:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr).strip()


def find_target_release(current_id: str, purpose: str, releases_dir: Path = RELEASES_DIR) -> str:
    if not releases_dir.is_dir():
        raise RuntimeError(f"releases directory not found: {releases_dir}")
    candidates: list[str] = []
    for child in releases_dir.iterdir():
        if not child.is_dir():
            continue
        runtime_file = child / "runtime.json"
        if not runtime_file.is_file():
            continue
        try:
            data = json.loads(runtime_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("state") != "ready":
            continue
        if data.get("parent_release_id") == current_id:
            verif = data.get("verification") or {}
            if purpose in str(verif.get("purpose") or ""):
                candidates.append(data.get("release_id") or child.name)
    if not candidates:
        raise RuntimeError(f"no ready release found with parent_release_id={current_id} and purpose={purpose}")
    candidates.sort()
    return candidates[0]


def is_already_on_target(current_id: str, purpose: str, releases_dir: Path = RELEASES_DIR) -> bool:
    runtime_file = releases_dir / current_id / "runtime.json"
    if runtime_file.is_file():
        try:
            data = json.loads(runtime_file.read_text(encoding="utf-8"))
            verif = data.get("verification") or {}
            if purpose in str(verif.get("purpose") or ""):
                return True
        except Exception:
            pass
    if purpose == "keybroker_stt_endpoint":
        if "kb1" in current_id or current_id.endswith(("-tgvid1k1", "-tk1")):
            return True
    return False


def update_policy(
    owner: str,
    target: str,
    policy: dict,
    promote_channel: str | None,
    policy_path: Path = POLICY,
    policyctl: str = POLICYCTL,
) -> dict:
    prof = (policy.get("profiles") or {}).get(owner)
    if prof:
        if prof.get("channel") == "pinned":
            track = prof.get("track", "modern")
            rc, out = sh([
                "python3", policyctl, "assign",
                "--profile", owner,
                "--track", track,
                "--channel", "pinned",
                "--release-id", target,
            ])
            print(f"[{owner}] policy assign pinned rc={rc}: {out[-200:]}")
            if rc:
                raise RuntimeError(f"policy assign failed (rc={rc}): {out}")
        else:
            track = prof.get("track") or policy.get("defaults", {}).get("forge_new", {}).get("track", "modern")
            channel = prof.get("channel") or policy.get("defaults", {}).get("forge_new", {}).get("channel", "stable")
            rc, out = sh([
                "python3", policyctl, "promote",
                "--track", track,
                "--channel", channel,
                "--release-id", target,
            ])
            print(f"[{owner}] policy promote {track}/{channel} rc={rc}: {out[-200:]}")
            if rc:
                raise RuntimeError(f"policy promote failed (rc={rc}): {out}")
    else:
        if not promote_channel:
            raise RuntimeError(f"owner '{owner}' not in policy profiles and --promote-channel not specified")
        track = policy.get("defaults", {}).get("forge_new", {}).get("track", "modern")
        channel = promote_channel
        rc, out = sh([
            "python3", policyctl, "promote",
            "--track", track,
            "--channel", channel,
            "--release-id", target,
        ])
        print(f"[{owner}] policy promote {track}/{channel} rc={rc}: {out[-200:]}")
        if rc:
            raise RuntimeError(f"policy promote failed (rc={rc}): {out}")

    return json.loads(policy_path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Roll out keybroker release to profiles.")
    ap.add_argument("--owner", action="append", required=True, help="Linux profile owner(s)")
    ap.add_argument("--apply", action="store_true", help="Apply upgrades and policy updates")
    ap.add_argument("--purpose", default="keybroker_stt_endpoint", help="Verification purpose in target runtime.json")
    ap.add_argument("--promote-channel", help="Explicit promote channel for profiles not registered in policy")
    ap.add_argument("--policy-file", default=str(POLICY), help="Path to fleet policy JSON")
    ap.add_argument("--releases-dir", default=str(RELEASES_DIR), help="Path to releases dir")
    ap.add_argument("--upgrade-script", default=UPGRADE, help="Path to upgrade_profile.py")
    ap.add_argument("--policyctl-script", default=POLICYCTL, help="Path to policyctl.py")
    args = ap.parse_args(argv)

    policy_path = Path(args.policy_file)
    releases_dir = Path(args.releases_dir)
    upgrade_script = args.upgrade_script
    policyctl_script = args.policyctl_script

    try:
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"failed to read policy file {policy_path}: {exc}", file=sys.stderr)
        return 1

    errors: dict[str, str] = {}

    for owner in args.owner:
        try:
            entry = pwd.getpwnam(owner)
        except KeyError:
            err = f"unknown linux user {owner}"
            print(f"[{owner}] {err}", file=sys.stderr)
            errors[owner] = err
            continue

        hermes = Path(entry.pw_dir) / ".hermes"
        agent_link = hermes / "hermes-agent"
        try:
            current = Path(os.readlink(agent_link)).parent.name
        except OSError as exc:
            err = f"failed to readlink {agent_link}: {exc}"
            print(f"[{owner}] {err}", file=sys.stderr)
            errors[owner] = err
            continue

        # Check if owner not in policy["profiles"] and no --promote-channel
        prof = (policy.get("profiles") or {}).get(owner)
        if not prof and not args.promote_channel:
            err = f"owner '{owner}' not in policy profiles and --promote-channel not specified"
            print(f"[{owner}] {err}", file=sys.stderr)
            errors[owner] = err
            continue

        if is_already_on_target(current, args.purpose, releases_dir=releases_dir):
            print(f"[{owner}] already on {current}")
            if args.apply:
                try:
                    policy = update_policy(
                        owner,
                        current,
                        policy,
                        args.promote_channel,
                        policy_path=policy_path,
                        policyctl=policyctl_script,
                    )
                except Exception as exc:
                    print(f"[{owner}] policy update failed: {exc}", file=sys.stderr)
                    errors[owner] = str(exc)
            continue

        try:
            target = find_target_release(current, args.purpose, releases_dir=releases_dir)
        except Exception as exc:
            print(f"[{owner}] target lookup failed: {exc}", file=sys.stderr)
            errors[owner] = str(exc)
            continue

        print(f"[{owner}] {current} -> {target}")

        if owner == "pavel":
            rc, out = sh(["fuser", str(LOCK)])
            if rc == 0 and out:
                err = f"video-editor runtime.lock is held ({out}) — refusing to restart now"
                print(f"[{owner}] {err}", file=sys.stderr)
                errors[owner] = err
                continue

        rc, out = sh(["python3", upgrade_script, "--owner", owner, "--release-id", target])
        print(f"[{owner}] preflight rc={rc}: {out[-300:]}")
        if rc:
            err = f"preflight failed (rc={rc}): {out[-300:]}"
            errors[owner] = err
            continue

        if not args.apply:
            continue

        # timeout=None for apply because upgrade_profile.py handles its own rollback
        rc, out = sh(["python3", upgrade_script, "--owner", owner, "--release-id", target, "--apply"], timeout=None)
        print(f"[{owner}] apply rc={rc}: {out[-500:]}")
        if rc:
            err = f"apply failed (rc={rc}): {out[-500:]}"
            errors[owner] = err
            continue

        time.sleep(25)
        rc, state = sh(["systemctl", "is-active", f"{owner}-hermes.service"])
        try:
            gw = json.loads((hermes / "gateway_state.json").read_text(encoding="utf-8"))
            tg_state = gw.get("platforms", {}).get("telegram", {}).get("state")
        except Exception:
            tg_state = "unknown"

        try:
            link = Path(os.readlink(hermes / "hermes-agent")).parent.name
        except OSError:
            link = "unknown"

        print(f"[{owner}] service={state} telegram={tg_state} link={link}")

        try:
            policy = update_policy(
                owner,
                target,
                policy,
                args.promote_channel,
                policy_path=policy_path,
                policyctl=policyctl_script,
            )
        except Exception as exc:
            print(f"[{owner}] policy update failed: {exc}", file=sys.stderr)
            errors[owner] = str(exc)
            continue

    if errors:
        print(f"rollout completed with errors for: {list(errors.keys())}", file=sys.stderr)
        return 1

    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
