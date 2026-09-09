#!/usr/bin/env python3
"""Install Hermes Forge Fleet Operations without overwriting live policy."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
TARGET = Path("/opt/proai-hermes-fleet-ops")
POLICY = Path("/etc/proai-hermes-fleet-policy.json")
SYSTEMD = Path("/etc/systemd/system")
FILES = ("store.py", "collector.py", "updater.py", "policyctl.py", "report.py", "telemetryctl.py")
UNITS = (
    "proai-hermes-analytics-collector.service",
    "proai-hermes-analytics-collector.timer",
    "proai-hermes-fleet-updater.service",
    "proai-hermes-fleet-updater.timer",
)


def main() -> int:
    if os.geteuid() != 0:
        raise RuntimeError("root_required")
    TARGET.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chown(TARGET, 0, 0)
    os.chmod(TARGET, 0o700)
    for name in FILES:
        shutil.copy2(HERE / name, TARGET / name)
        os.chown(TARGET / name, 0, 0)
        os.chmod(TARGET / name, 0o600)
    if not POLICY.exists():
        shutil.copy2(HERE / "fleet-policy.initial.json", POLICY)
        os.chown(POLICY, 0, 0)
        os.chmod(POLICY, 0o600)
    elif POLICY.is_symlink() or POLICY.stat().st_uid != 0 or POLICY.stat().st_mode & 0o022:
        raise RuntimeError("existing_policy_unsafe")
    for name in UNITS:
        shutil.copy2(HERE / name, SYSTEMD / name)
        os.chown(SYSTEMD / name, 0, 0)
        os.chmod(SYSTEMD / name, 0o644)
    subprocess.run(["/usr/bin/systemctl", "daemon-reload"], check=True)
    for timer in ("proai-hermes-analytics-collector.timer", "proai-hermes-fleet-updater.timer"):
        subprocess.run(["/usr/bin/systemctl", "enable", "--now", timer], check=True)
    print("installed=true")
    print("policy_preserved=true")
    print("analytics_timer=enabled")
    print("updater_timer=enabled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
