#!/usr/bin/env python3
"""Switch profiles to the `kb2` release of their line (VPS, root). Target is found by metadata, not by name:
the release whose runtime.json has parent_release_id == <original parent of the current release> and
verification.purpose == keybroker_stt_endpoint_kb2 (state ready, env_override_verified).

  rollout_kb2.py --owner X [--owner Y ...] [--apply]
Per profile: upgrade_profile.py preflight → --apply (stop, repoint, register, start, rollback on failure) → policy
(pinned → assign; otherwise promote only if --promote-channel is given) → service/telegram check. pavel is refused
while /opt/vektor/video-editor/runtime.lock is held. A failing profile does not stop the batch; exit code 1 at the end.
"""
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
RELEASES = Path("/opt/vektor/releases")
LOCK = Path("/opt/vektor/video-editor/runtime.lock")
PURPOSE = "keybroker_stt_endpoint_kb2"

ap = argparse.ArgumentParser()
ap.add_argument("--owner", action="append", required=True)
ap.add_argument("--apply", action="store_true")
ap.add_argument("--promote-channel", help="for owners absent from the policy: promote this channel of track modern")
args = ap.parse_args()


def sh(cmd, timeout=None):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr).strip()


def runtime(release_id: str) -> dict:
    return json.loads((RELEASES / release_id / "runtime.json").read_text(encoding="utf-8"))


def original_parent(release_id: str) -> str:
    """Walk parent_release_id until a release that is not a keybroker patch."""
    rid = release_id
    for _ in range(5):
        rt = runtime(rid)
        if not str((rt.get("verification") or {}).get("purpose", "")).startswith("keybroker_stt_endpoint"):
            return rid
        rid = rt["parent_release_id"]
    raise RuntimeError(f"parent chain too deep for {release_id}")


def find_target(parent: str) -> str:
    for d in RELEASES.iterdir():
        if not d.is_dir() or d.name.startswith("."):
            continue
        try:
            rt = runtime(d.name)
        except Exception:
            continue
        ver = rt.get("verification") or {}
        if rt.get("parent_release_id") == parent and ver.get("purpose") == PURPOSE and rt.get("state") == "ready" \
                and ver.get("env_override_verified") is True:
            return d.name
    raise RuntimeError(f"no ready {PURPOSE} release derived from {parent}")


policy = json.loads(POLICY.read_text(encoding="utf-8"))
failed = 0
for owner in args.owner:
    try:
        entry = pwd.getpwnam(owner)
        hermes = Path(entry.pw_dir) / ".hermes"
        current = Path(os.readlink(hermes / "hermes-agent")).parent.name
        target = find_target(original_parent(current))
        prof = (policy.get("profiles") or {}).get(owner)
        if current == target:
            print(f"[{owner}] already on {target}")
        else:
            print(f"[{owner}] {current} -> {target}")
            if owner == "pavel":
                rc, out = sh(["fuser", str(LOCK)])
                if rc == 0 and out:
                    raise RuntimeError(f"video-editor runtime.lock is held ({out}) — refusing to restart now")
            rc, out = sh(["python3", UPGRADE, "--owner", owner, "--release-id", target])
            print(f"[{owner}] preflight rc={rc}: {out[-200:]}")
            if rc:
                raise RuntimeError("preflight failed")
            if not args.apply:
                continue
            rc, out = sh(["python3", UPGRADE, "--owner", owner, "--release-id", target, "--apply"])
            print(f"[{owner}] apply rc={rc}: {out[-300:]}")
            if rc:
                raise RuntimeError("apply failed")
            time.sleep(25)
            rc, state = sh(["systemctl", "is-active", f"{owner}-hermes.service"])
            gw = json.loads((hermes / "gateway_state.json").read_text(encoding="utf-8"))
            tg = (gw.get("platforms") or {}).get("telegram", {}).get("state")
            print(f"[{owner}] service={state} telegram={tg} link={Path(os.readlink(hermes / 'hermes-agent')).parent.name[-14:]}")
        if not args.apply:
            continue
        # policy bookkeeping (also on "already on", in case a previous run died before this step)
        if prof and prof.get("channel") == "pinned":
            rc, out = sh(["python3", POLICYCTL, "assign", "--profile", owner, "--track", prof.get("track", "modern"),
                          "--channel", "pinned", "--release-id", target])
            print(f"[{owner}] policy assign pinned rc={rc}: {out[-120:]}")
        elif args.promote_channel:
            rc, out = sh(["python3", POLICYCTL, "promote", "--track", "modern", "--channel", args.promote_channel, "--release-id", target])
            print(f"[{owner}] policy promote modern/{args.promote_channel} rc={rc}: {out[-120:]}")
        else:
            print(f"[{owner}] not in policy profiles and no --promote-channel: policy untouched")
        policy = json.loads(POLICY.read_text(encoding="utf-8"))
    except Exception as exc:
        failed += 1
        print(f"!!! [{owner}] {exc}")
print("done" if not failed else f"done with {failed} failure(s)")
sys.exit(1 if failed else 0)
