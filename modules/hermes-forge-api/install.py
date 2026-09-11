#!/usr/bin/env python3
"""Install the bounded Hermes Forge root-control + unprivileged API services."""
from __future__ import annotations

import os
import pwd
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

SOURCE = Path(__file__).resolve().parent
KITCHEN_SOURCE = SOURCE.parent / "hermes-forge-kitchen" / "kitchen.py"
TARGET = Path("/opt/proai-hermes-forge-api")
SYSTEMD = Path("/etc/systemd/system")
BACKUPS = Path("/opt/backups")
FILES = ("control.py", "api.py")
UNITS = ("proai-hermes-forge-control.service", "proai-hermes-forge-api.service")


def run(*args: str, timeout: int = 60) -> None:
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError(Path(args[0]).name + "_failed")


def atomic_copy(source: Path, target: Path, mode: int) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temp = Path(raw)
    os.close(fd)
    try:
        shutil.copyfile(source, temp)
        os.chown(temp, 0, 0)
        os.chmod(temp, mode)
        os.replace(temp, target)
    finally:
        if temp.exists():
            temp.unlink()


def install() -> Path:
    if os.geteuid() != 0:
        raise RuntimeError("root_required")
    pwd.getpwnam("www-data")
    run(
        "/usr/bin/python3", "-m", "py_compile",
        str(SOURCE / "control.py"), str(SOURCE / "api.py"), str(KITCHEN_SOURCE),
    )
    backup = BACKUPS / f"hermes-forge-api-install-{time.time_ns()}"
    backup.mkdir(parents=True, mode=0o700)
    os.chmod(backup, 0o700)
    if TARGET.exists():
        shutil.copytree(TARGET, backup / "app", symlinks=True)
    for unit in UNITS:
        live = SYSTEMD / unit
        if live.is_file() and not live.is_symlink():
            shutil.copy2(live, backup / unit)
    TARGET.mkdir(parents=True, exist_ok=True, mode=0o755)
    os.chown(TARGET, 0, 0)
    os.chmod(TARGET, 0o755)
    for name in FILES:
        atomic_copy(SOURCE / name, TARGET / name, 0o644)
    atomic_copy(KITCHEN_SOURCE, TARGET / "kitchen.py", 0o644)
    static = TARGET / "static"
    static.mkdir(exist_ok=True, mode=0o755)
    os.chown(static, 0, 0)
    os.chmod(static, 0o755)
    for source in sorted((SOURCE / "static").iterdir()):
        if source.is_file() and not source.is_symlink():
            atomic_copy(source, static / source.name, 0o644)
    for unit in UNITS:
        atomic_copy(SOURCE / unit, SYSTEMD / unit, 0o644)
    run("/usr/bin/systemctl", "daemon-reload")
    run("/usr/bin/systemctl", "enable", "--now", "proai-hermes-forge-control.service")
    run("/usr/bin/systemctl", "enable", "--now", "proai-hermes-forge-api.service")
    return backup


if __name__ == "__main__":
    path = install()
    print("installed=true")
    print("listen=127.0.0.1:8650")
    print("control_socket=/run/proai-hermes-forge/control.sock")
    print(f"backup={path}")
