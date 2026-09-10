#!/usr/bin/env python3
"""Install the Hermes planned-restart systemd contract for existing Forge profiles."""
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

PROFILE_ROOT = Path("/opt/vektor/profiles")
SYSTEMD_ROOT = Path("/etc/systemd/system")
BACKUP_ROOT = Path("/opt/backups")
SYSTEMCTL = "/usr/bin/systemctl"
DROPIN_NAME = "20-hermes-planned-restart.conf"
DROPIN_TEXT = "[Service]\nSuccessExitStatus=75\n"
OWNER_RE = re.compile(r"^[a-z][a-z0-9_-]{1,39}$")
SERVICE_RE = re.compile(r"^[a-z][a-z0-9_-]{1,39}-hermes\.service$")


def run(*args: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError(Path(args[0]).name + "_failed")
    return result

def owners() -> list[str]:
    result: list[str] = []
    if PROFILE_ROOT.is_symlink() or not PROFILE_ROOT.is_dir():
        raise RuntimeError("profile_root_unsafe")
    for path in sorted(PROFILE_ROOT.glob("*.json")):
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("profile_registry_unsafe")
        payload = json.loads(path.read_text(encoding="utf-8"))
        owner = str(payload.get("owner") or "")
        if not OWNER_RE.fullmatch(owner) or path.stem != owner:
            raise RuntimeError("profile_owner_invalid")
        service = f"{owner}-hermes.service"
        unit = SYSTEMD_ROOT / service
        if not SERVICE_RE.fullmatch(service) or unit.is_symlink() or not unit.is_file():
            raise RuntimeError("profile_service_unsafe")
        result.append(owner)
    if not result:
        raise RuntimeError("profile_registry_empty")
    return result


def properties(owner: str) -> dict[str, str]:
    service = f"{owner}-hermes.service"
    raw = run(SYSTEMCTL, "show", service, "-p", "ExecReload", "-p", "RestartForceExitStatus", "-p", "SuccessExitStatus").stdout
    values = {}
    for line in raw.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values

def has_status(raw: str, code: int) -> bool:
    return str(code) in {item for item in re.split(r"[ ,]+", str(raw or "").strip()) if item}


def preflight(owner: str) -> dict[str, object]:
    values = properties(owner)
    if "USR1" not in values.get("ExecReload", ""):
        raise RuntimeError("profile_exec_reload_unsupported")
    if not has_status(values.get("RestartForceExitStatus", ""), 75):
        raise RuntimeError("profile_restart_exit_contract_missing")
    return {
        "owner": owner,
        "service": f"{owner}-hermes.service",
        "success_exit_75": has_status(values.get("SuccessExitStatus", ""), 75),
    }


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    if path.parent.is_symlink() or path.is_symlink():
        raise RuntimeError("dropin_path_unsafe")
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chown(temp, 0, 0)
        os.chmod(temp, 0o644)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()

def install(apply: bool) -> dict[str, object]:
    if os.geteuid() != 0:
        raise RuntimeError("root_required")
    found = owners()
    plan = [preflight(owner) for owner in found]
    if not apply:
        return {"applied": False, "profiles": plan}

    backup = BACKUP_ROOT / f"hermes-planned-restart-contract-{time.time_ns()}"
    backup.mkdir(parents=True, mode=0o700)
    os.chmod(backup, 0o700)
    changes: list[tuple[Path, Path | None]] = []
    changed = 0
    try:
        for item in plan:
            owner = str(item["owner"])
            if bool(item.get("success_exit_75")):
                continue
            directory = SYSTEMD_ROOT / f"{owner}-hermes.service.d"
            target = directory / DROPIN_NAME
            if target.exists() and target.read_text(encoding="utf-8") == DROPIN_TEXT:
                continue
            previous: Path | None = None
            if target.exists():
                if target.is_symlink() or not target.is_file():
                    raise RuntimeError("dropin_path_unsafe")
                previous = backup / f"{owner}.{DROPIN_NAME}.before"
                shutil.copy2(target, previous)
            atomic_write(target, DROPIN_TEXT)
            changes.append((target, previous))
            changed += 1

        run(SYSTEMCTL, "daemon-reload")
        verified = [preflight(owner) for owner in found]
        if not all(bool(item["success_exit_75"]) for item in verified):
            raise RuntimeError("restart_contract_verify_failed")
        return {"applied": True, "changed": changed, "backup": str(backup), "profiles": verified}
    except Exception:
        for target, previous in reversed(changes):
            try:
                if previous is None:
                    target.unlink(missing_ok=True)
                else:
                    shutil.copy2(previous, target)
                    os.chown(target, 0, 0)
                    os.chmod(target, 0o644)
            except OSError:
                pass
        try:
            run(SYSTEMCTL, "daemon-reload")
        except Exception:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = install(args.apply)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
