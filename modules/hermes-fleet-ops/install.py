#!/usr/bin/env python3
"""Install Hermes Forge Fleet Operations without overwriting live policy."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE
TARGET = Path("/opt/proai-hermes-fleet-ops")
EXPORTER_TARGET = Path("/opt/proai-hermes-shared-metrics-exporter")
POLICY = Path("/etc/proai-hermes-fleet-policy.json")
SYSTEMD = Path("/etc/systemd/system")
FILES = ("store.py", "collector.py", "updater.py", "policyctl.py", "report.py", "telemetryctl.py", "enroll_shared_metrics_exporters.py")
UNITS = (
    "proai-hermes-analytics-collector.service",
    "proai-hermes-analytics-collector.timer",
    "proai-hermes-fleet-updater.service",
    "proai-hermes-fleet-updater.timer",
    "proai-hermes-shared-metrics-export@.service",
    "proai-hermes-shared-metrics-export@.timer",
    "proai-hermes-shared-metrics-enroll.service",
    "proai-hermes-shared-metrics-enroll.timer",
)


def atomic_copy(source: Path, target: Path, mode: int) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name("." + target.name + ".tmp")
    shutil.copy2(source, temporary)
    os.chown(temporary, 0, 0)
    os.chmod(temporary, mode)
    os.replace(temporary, target)


def main() -> int:
    if os.geteuid() != 0:
        raise RuntimeError("root_required")
    TARGET.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chown(TARGET, 0, 0)
    os.chmod(TARGET, 0o700)
    EXPORTER_TARGET.mkdir(parents=True, exist_ok=True, mode=0o755)
    os.chown(EXPORTER_TARGET, 0, 0)
    os.chmod(EXPORTER_TARGET, 0o755)
    atomic_copy(SOURCE / "export_shared_metrics.py", EXPORTER_TARGET / "export_shared_metrics.py", 0o644)
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
    for timer in (
        "proai-hermes-analytics-collector.timer",
        "proai-hermes-fleet-updater.timer",
        "proai-hermes-shared-metrics-enroll.timer",
    ):
        subprocess.run(["/usr/bin/systemctl", "enable", "--now", timer], check=True)
    subprocess.run(["/usr/bin/systemctl", "start", "proai-hermes-shared-metrics-enroll.service"], check=True)
    print("installed=true")
    print("policy_preserved=true")
    print("analytics_timer=enabled")
    print("updater_timer=enabled")
    print("shared_metrics_enrollment=enabled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
