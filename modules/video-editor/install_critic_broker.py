#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

RUNTIME = Path("/opt/vektor/video-editor")
PRIVATE = RUNTIME / "private"
SERVICE_NAME = "vektor-video-critic-broker.service"


def _validate_secret(path: Path, *, expected_uid: int = 0) -> None:
    try: info = path.lstat()
    except OSError as exc: raise RuntimeError("lingsuan_secret_missing") from exc
    if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid != expected_uid or (info.st_mode & 0o077):
        raise RuntimeError("lingsuan_secret_unsafe")
    if info.st_size <= 0 or info.st_size > 4096:
        raise RuntimeError("lingsuan_secret_size_invalid")
    found = any(line.startswith("LINGSUAN_API_KEY=") and len(line.partition("=")[2].strip()) >= 32 for line in path.read_text().splitlines())
    if not found: raise RuntimeError("lingsuan_key_missing")
def _root_dir(path: Path, mode: int = 0o700) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=mode)
    info = path.lstat()
    if path.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid != 0:
        raise RuntimeError("runtime_directory_unsafe")
    path.chmod(mode)


def _atomic_bytes(path: Path, data: bytes, mode: int) -> None:
    fd, name = tempfile.mkstemp(prefix=".critic-install-", dir=path.parent)
    temp = Path(name)
    try:
        os.fchmod(fd, mode); os.fchown(fd, 0, 0)
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(data); handle.flush(); os.fsync(handle.fileno())
        os.close(fd); fd = -1
        os.replace(temp, path); path.chmod(mode); os.chown(path, 0, 0)
    finally:
        if fd >= 0: os.close(fd)
        temp.unlink(missing_ok=True)


def _wait_health(attempts: int = 30) -> dict:
    last = "unavailable"
    for _ in range(attempts):
        try:
            with urllib.request.urlopen("http://127.0.0.1:8778/health", timeout=2) as response:
                data = json.loads(response.read().decode())
            if data.get("ok"): return data
            last = str(data.get("error") or "not_ok")
        except Exception as exc: last = type(exc).__name__
        time.sleep(0.3)
    raise RuntimeError(f"critic_health_failed:{last}")
def install(secret: Path) -> None:
    if os.geteuid() != 0: raise RuntimeError("root_required")
    _validate_secret(secret)
    module = Path(__file__).resolve().parent
    broker_src = module / "critic_broker.py"
    service_src = module / SERVICE_NAME
    if not broker_src.is_file() or broker_src.is_symlink(): raise RuntimeError("critic_broker_source_missing")
    if not service_src.is_file() or service_src.is_symlink(): raise RuntimeError("critic_service_source_missing")
    _root_dir(RUNTIME, 0o755)
    _root_dir(RUNTIME / "broker", 0o755)
    for path in (PRIVATE, PRIVATE / "critic-clients", PRIVATE / "critic-policies", PRIVATE / "critic-tmp"):
        _root_dir(path, 0o700)
    canonical = PRIVATE / "lingsuan.env"
    if secret.resolve() != canonical.resolve():
        _atomic_bytes(canonical, secret.read_bytes(), 0o600)
    _validate_secret(canonical)
    _atomic_bytes(RUNTIME / "broker" / "critic_broker.py", broker_src.read_bytes(), 0o755)
    _atomic_bytes(Path("/etc/systemd/system") / SERVICE_NAME, service_src.read_bytes(), 0o644)
    subprocess.run(["systemctl","daemon-reload"],check=True)
    subprocess.run(["systemctl","enable",SERVICE_NAME],check=True,stdout=subprocess.DEVNULL)
    subprocess.run(["systemctl","restart",SERVICE_NAME],check=True)
    if subprocess.check_output(["systemctl","is-active",SERVICE_NAME],text=True).strip() != "active":
        raise RuntimeError("critic_broker_not_active")
    health = _wait_health()
    print("critic_broker_installed=true")
    print("lingsuan_secret_printed=false")
    print(f"model={health.get('model')}")
    print(f"service={SERVICE_NAME}")
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--secret", default=str(PRIVATE / "lingsuan.env"))
    args = parser.parse_args()
    try: install(Path(args.secret))
    except Exception as exc:
        print(f"error={type(exc).__name__}:{exc}", file=__import__("sys").stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
