#!/usr/bin/env python3
"""Switch profiles to their `kb1` release (env-overridable OpenRouter STT endpoint) with the fleet's own tool (VPS, root).

  rollout_kb1.py --owner X [--apply]        preflight only / apply
Target id = derived from the current release the same way build_keybroker_kb1.py names it:
`-kb1` if it fits the 80-char validator limit, else `-tgvid1` → `-tgvid1k1`, `-tv1` → `-tk1`.
Steps per profile: upgrade_profile.py preflight → --apply (stop, repoint symlink, register, start, rollback on failure);
policy: pinned → `policyctl assign`, stable channel (Forge) → `policyctl promote`. For pavel: refuse while
/opt/vektor/video-editor/runtime.lock is held.
"""
import argparse
import json
import os
import pwd
import re
import subprocess
import sys
import time
from pathlib import Path

UPGRADE = "/opt/vektor/admin/upgrades/v0.21.0/upgrade_profile.py"
POLICYCTL = "/opt/proai-hermes-fleet-ops/policyctl.py"
POLICY = Path("/etc/proai-hermes-fleet-policy.json")
LOCK = Path("/opt/vektor/video-editor/runtime.lock")

ap = argparse.ArgumentParser()
ap.add_argument("--owner", action="append", required=True)
ap.add_argument("--apply", action="store_true")
args = ap.parse_args()


def derived_id(parent_id: str) -> str:
    for candidate in (parent_id + "-kb1",
                      re.sub(r"-tgvid1$", "-tgvid1k1", parent_id),
                      re.sub(r"-tv1$", "-tk1", parent_id)):
        if candidate != parent_id and len(candidate) - len("hermes-") <= 80:
            return candidate
    raise RuntimeError(f"cannot derive a valid id from {parent_id}")


def sh(cmd, timeout=900):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr).strip()


policy = json.loads(POLICY.read_text(encoding="utf-8"))
for owner in args.owner:
    entry = pwd.getpwnam(owner)
    hermes = Path(entry.pw_dir) / ".hermes"
    current = Path(os.readlink(hermes / "hermes-agent")).parent.name
    if "kb1" in current or current.endswith(("-tgvid1k1", "-tk1")):
        print(f"[{owner}] already on {current}")
        continue
    target = derived_id(current)
    rel = Path("/opt/vektor/releases") / target
    runtime = json.loads((rel / "runtime.json").read_text(encoding="utf-8"))
    assert runtime.get("state") == "ready" and runtime.get("release_id") == target, runtime.get("state")
    print(f"[{owner}] {current} -> {target}")
    if owner == "pavel":
        rc, out = sh(["fuser", str(LOCK)])
        if rc == 0 and out:
            print(f"[{owner}] video-editor runtime.lock is held ({out}) — refusing to restart now")
            sys.exit(3)
    rc, out = sh(["python3", UPGRADE, "--owner", owner, "--release-id", target])
    print(f"[{owner}] preflight rc={rc}: {out[-300:]}")
    if rc:
        sys.exit(rc)
    if not args.apply:
        continue
    rc, out = sh(["python3", UPGRADE, "--owner", owner, "--release-id", target, "--apply"])
    print(f"[{owner}] apply rc={rc}: {out[-500:]}")
    if rc:
        sys.exit(rc)
    time.sleep(25)
    rc, state = sh(["systemctl", "is-active", f"{owner}-hermes.service"])
    gw = json.loads((hermes / "gateway_state.json").read_text(encoding="utf-8"))
    link = Path(os.readlink(hermes / "hermes-agent")).parent.name
    print(f"[{owner}] service={state} telegram={gw['platforms']['telegram'].get('state')} link={link}")
    prof = (policy.get("profiles") or {}).get(owner)
    if prof and prof.get("channel") == "pinned":
        rc, out = sh(["python3", POLICYCTL, "assign", "--profile", owner, "--track", prof.get("track", "modern"),
                      "--channel", "pinned", "--release-id", target])
        print(f"[{owner}] policy assign pinned rc={rc}: {out[-200:]}")
    else:
        track = (prof or {}).get("track") or policy.get("defaults", {}).get("forge_new", {}).get("track", "modern")
        channel = (prof or {}).get("channel") or policy.get("defaults", {}).get("forge_new", {}).get("channel", "stable")
        rc, out = sh(["python3", POLICYCTL, "promote", "--track", track, "--channel", channel, "--release-id", target])
        print(f"[{owner}] policy promote {track}/{channel} rc={rc}: {out[-200:]}")
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
print("done")
