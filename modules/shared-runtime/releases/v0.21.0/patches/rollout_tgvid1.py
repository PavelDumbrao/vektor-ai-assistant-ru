#!/usr/bin/env python3
"""Switch profiles to their `<parent>-tgvid1` release with the fleet's own tool (run on VPS as root).

  rollout_tgvid1.py --owner pavel [--apply]       preflight (no --apply) or apply one profile
  rollout_tgvid1.py --owner X --owner Y --apply
Steps per profile: resolve current release from ~/.hermes/hermes-agent symlink -> target = current + "-tgvid1"
(must exist, state ready); for pavel: refuse if /opt/vektor/video-editor/runtime.lock is held (fuser);
upgrade_profile.py preflight -> --apply (stops/starts service, repoints symlink, updates /opt/vektor/profiles/<owner>.json,
rollback on failure); then policy: pinned profiles -> `policyctl assign ... --channel pinned --release-id <new>`;
stable-channel profiles (Forge clients) -> `policyctl promote --track modern --channel stable --release-id <new>`.
Verifies service active + Telegram connected afterwards.
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
LOCK = Path("/opt/vektor/video-editor/runtime.lock")
SUFFIX = "-tgvid1"

ap = argparse.ArgumentParser()
ap.add_argument("--owner", action="append", required=True)
ap.add_argument("--apply", action="store_true")
args = ap.parse_args()


def sh(cmd, timeout=900):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr).strip()


policy = json.loads(POLICY.read_text(encoding="utf-8"))
for owner in args.owner:
    entry = pwd.getpwnam(owner)
    hermes = Path(entry.pw_dir) / ".hermes"
    current = Path(os.readlink(hermes / "hermes-agent")).parent.name
    target = current + SUFFIX
    if len(target) - len("hermes-") > 80:   # validator limit ^hermes-[A-Za-z0-9_.-]{1,80}$
        target = current + "-tv1"
    rel = Path("/opt/vektor/releases") / target
    runtime = json.loads((rel / "runtime.json").read_text(encoding="utf-8"))
    assert runtime.get("state") == "ready" and runtime.get("release_id") == target, runtime.get("state")
    print(f"[{owner}] {current} -> {target}")
    if owner == "pavel":
        rc, out = sh(["fuser", str(LOCK)])
        if rc == 0 and out:
            print(f"[{owner}] video-editor runtime.lock is held ({out}) — refusing to restart now")
            sys.exit(3)
        print(f"[{owner}] video-editor lock free")
    rc, out = sh(["python3", UPGRADE, "--owner", owner, "--release-id", target])
    print(f"[{owner}] preflight rc={rc}: {out[-400:]}")
    if rc:
        sys.exit(rc)
    if not args.apply:
        continue
    rc, out = sh(["python3", UPGRADE, "--owner", owner, "--release-id", target, "--apply"])
    print(f"[{owner}] apply rc={rc}: {out[-600:]}")
    if rc:
        sys.exit(rc)
    time.sleep(25)
    rc, state = sh(["systemctl", "is-active", f"{owner}-hermes.service"])
    gw = json.loads((hermes / "gateway_state.json").read_text(encoding="utf-8"))
    link = Path(os.readlink(hermes / "hermes-agent")).parent.name
    print(f"[{owner}] service={state} telegram={gw['platforms']['telegram'].get('state')} link={link}")
    # policy bookkeeping
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
