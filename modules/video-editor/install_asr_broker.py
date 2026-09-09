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
SERVICE_NAME = "vektor-video-asr-broker.service"


def _read_key(path: Path, *, expected_uid: int = 0) -> str:
    try:
        info = path.lstat()
    except OSError as exc:
        raise RuntimeError("openrouter_source_env_missing") from exc
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise RuntimeError("openrouter_source_env_unsafe")
    if info.st_uid != expected_uid or (info.st_mode & 0o077):
        raise RuntimeError("openrouter_source_env_permissions_unsafe")
    if info.st_size <= 0 or info.st_size > 64 * 1024:
        raise RuntimeError("openrouter_source_env_size_invalid")
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("OPENROUTER_API_KEY="):
            value = line.partition("=")[2].strip()
            if len(value) >= 16 and not any(ch.isspace() for ch in value):
                return value
    return ""


def _root_dir(path: Path, mode: int) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=mode)
    info = path.lstat()
    if path.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid != 0:
        raise RuntimeError("runtime_directory_unsafe")
    path.chmod(mode)


def _atomic_bytes(path: Path, data: bytes, mode: int) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=".install-", dir=path.parent)
    temp = Path(temp_name)
    try:
        os.fchmod(fd, mode)
        os.fchown(fd, 0, 0)
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.close(fd)
        fd = -1
        os.replace(temp, path)
        path.chmod(mode)
        os.chown(path, 0, 0)
    finally:
        if fd >= 0:
            os.close(fd)
        temp.unlink(missing_ok=True)


def _wait_health(attempts: int = 20, delay: float = 0.25) -> dict:
    last = "broker_health_unavailable"
    for _ in range(max(1, attempts)):
        try:
            with urllib.request.urlopen("http://127.0.0.1:8777/health", timeout=2) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if isinstance(payload, dict) and payload.get("ok"):
                return payload
            last = "broker_health_not_ok"
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = type(exc).__name__
        time.sleep(max(0.01, min(1.0, delay)))
    raise RuntimeError(f"broker_health_failed:{last}")


def install(source_env: Path) -> None:
    if os.geteuid() != 0:
        raise RuntimeError("root_required")
    key = _read_key(source_env)
    if not key:
        raise RuntimeError("openrouter_key_missing")
    module = Path(__file__).resolve().parent
    broker_src = module / "asr_broker.py"
    service_src = module / SERVICE_NAME
    if not broker_src.is_file() or broker_src.is_symlink():
        raise RuntimeError("broker_source_missing")
    if not service_src.is_file() or service_src.is_symlink():
        raise RuntimeError("service_source_missing")

    _root_dir(RUNTIME, 0o755)
    broker_dir = RUNTIME / "broker"
    private = RUNTIME / "private"
    clients = private / "clients"
    _root_dir(broker_dir, 0o755)
    _root_dir(private, 0o700)
    _root_dir(clients, 0o700)
    _atomic_bytes(broker_dir / "asr_broker.py", broker_src.read_bytes(), 0o755)
    _atomic_bytes(private / "openrouter.env", ("OPENROUTER_API_KEY=" + key + "\n").encode("utf-8"), 0o600)

    service_target = Path("/etc/systemd/system") / SERVICE_NAME
    _atomic_bytes(service_target, service_src.read_bytes(), 0o644)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", SERVICE_NAME], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["systemctl", "restart", SERVICE_NAME], check=True)
    active = subprocess.check_output(["systemctl", "is-active", SERVICE_NAME], text=True).strip()
    if active != "active":
        raise RuntimeError("broker_not_active")

    _wait_health()
    print("broker_installed=true")
    print("openrouter_secret_printed=false")
    print(f"service={SERVICE_NAME}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-env", default=os.getenv("VEKTOR_OPENROUTER_ENV", "/opt/telegram-api-engine/.env.openrouter"))
    args = parser.parse_args()
    try:
        install(Path(args.source_env))
    except Exception as exc:
        print(f"error={type(exc).__name__}:{exc}", file=__import__("sys").stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
