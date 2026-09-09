#!/usr/bin/env python3
"""Install the private Docker bridge + Traefik HTTPS edge for Hermes Forge."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

SOURCE = Path(__file__).resolve().parent
EDGE_SOURCE = SOURCE / "deploy" / "edge"
TARGET = Path("/opt/proai-hermes-forge-edge")
SYSTEMD = Path("/etc/systemd/system")
BACKUPS = Path("/opt/backups")
BRIDGE_UNIT = "proai-hermes-forge-bridge.service"
DEFAULT_HOSTNAME = "forge.srv1250550.hstgr.cloud"
DOCKER_NETWORK = "n8n_default"
DOCKER_GATEWAY = "172.18.0.1"
EDGE_IMAGE = "nginx:1.27-alpine"
BRIDGE_HEALTH_URL = "http://host.docker.internal:8650/healthz"
HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{1,251}[a-z0-9])?$")


def run(*args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError(Path(args[0]).name + "_failed")
    return result


def hostname(value: str) -> str:
    candidate = str(value or "").strip().lower().rstrip(".")
    if not HOST_RE.fullmatch(candidate) or ".." in candidate:
        raise ValueError("hostname_invalid")
    return candidate


def atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chown(temp, 0, 0)
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def docker_gateway() -> str:
    raw = run("/usr/bin/docker", "network", "inspect", DOCKER_NETWORK).stdout
    payload = json.loads(raw)
    return str(payload[0]["IPAM"]["Config"][0]["Gateway"])


def bridge_probe() -> bool:
    """Probe the bridge from the same Docker network used by the HTTPS edge."""
    try:
        result = run(
            "/usr/bin/docker", "run", "--rm",
            "--network", DOCKER_NETWORK,
            "--add-host", f"host.docker.internal:{DOCKER_GATEWAY}",
            "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true",
            EDGE_IMAGE, "wget", "-qO-", "-T", "2", BRIDGE_HEALTH_URL,
            timeout=15,
        )
        payload = json.loads(result.stdout)
        return isinstance(payload, dict) and payload.get("ok") is True
    except Exception:
        return False


def wait_bridge(attempts: int = 12, delay: float = 0.25) -> None:
    count = max(1, int(attempts))
    for attempt in range(count):
        if bridge_probe():
            return
        if attempt + 1 < count:
            time.sleep(max(0.0, float(delay)))
    raise RuntimeError("bridge_health_failed")


def install(public_hostname: str) -> Path:
    if os.geteuid() != 0:
        raise RuntimeError("root_required")
    public_hostname = hostname(public_hostname)
    if shutil.which("socat") != "/usr/bin/socat":
        raise RuntimeError("socat_missing")
    if docker_gateway() != DOCKER_GATEWAY:
        raise RuntimeError("docker_gateway_changed")
    run("/usr/bin/docker", "compose", "version")

    backup = BACKUPS / f"hermes-forge-edge-install-{time.time_ns()}"
    backup.mkdir(parents=True, mode=0o700)
    os.chmod(backup, 0o700)
    if TARGET.exists():
        shutil.copytree(TARGET, backup / "edge", symlinks=True)
    live_unit = SYSTEMD / BRIDGE_UNIT
    if live_unit.is_file() and not live_unit.is_symlink():
        shutil.copy2(live_unit, backup / BRIDGE_UNIT)

    TARGET.mkdir(parents=True, exist_ok=True, mode=0o755)
    os.chown(TARGET, 0, 0)
    os.chmod(TARGET, 0o755)
    for name in ("docker-compose.yml", "nginx.conf"):
        source = EDGE_SOURCE / name
        target = TARGET / name
        shutil.copy2(source, target)
        os.chown(target, 0, 0)
        os.chmod(target, 0o644)
    atomic_write(TARGET / ".env", f"FORGE_HOSTNAME={public_hostname}\n", 0o600)

    shutil.copy2(SOURCE / BRIDGE_UNIT, live_unit)
    os.chown(live_unit, 0, 0)
    os.chmod(live_unit, 0o644)
    run("/usr/bin/systemctl", "daemon-reload")
    run("/usr/bin/systemctl", "enable", "--now", BRIDGE_UNIT)
    wait_bridge()

    compose = str(TARGET / "docker-compose.yml")
    env_file = str(TARGET / ".env")
    run("/usr/bin/docker", "compose", "--env-file", env_file, "-f", compose, "config", "-q")
    run("/usr/bin/docker", "compose", "--env-file", env_file, "-f", compose, "up", "-d", timeout=120)
    return backup


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hostname", default=DEFAULT_HOSTNAME)
    args = parser.parse_args()
    path = install(args.hostname)
    print("installed=true")
    print(f"hostname={hostname(args.hostname)}")
    print(f"bridge={DOCKER_GATEWAY}:8650->127.0.0.1:8650")
    print(f"backup={path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
