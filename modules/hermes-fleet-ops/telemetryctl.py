#!/usr/bin/env python3
"""Bounded per-profile switch for Forge privacy-safe shared metrics."""
from __future__ import annotations

import argparse
import os
import pwd
import re
import shutil
import tempfile
import time
from pathlib import Path

import yaml

OWNER_RE = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
PROFILE_ROOT = Path("/opt/vektor/profiles")


def configure(owner: str, enabled: bool) -> dict[str, object]:
    if os.geteuid() != 0 or not OWNER_RE.fullmatch(owner):
        raise ValueError("root_and_valid_owner_required")
    if not (PROFILE_ROOT / f"{owner}.json").is_file():
        raise ValueError("managed_profile_required")
    entry = pwd.getpwnam(owner)
    config_path = Path(entry.pw_dir) / ".hermes" / "config.yaml"
    if config_path.is_symlink() or not config_path.is_file():
        raise ValueError("profile_config_missing")
    info = config_path.stat()
    if info.st_uid != entry.pw_uid:
        raise ValueError("profile_config_owner_invalid")
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(config, dict):
        raise ValueError("profile_config_invalid")
    telemetry = config.setdefault("telemetry", {})
    if not isinstance(telemetry, dict):
        raise ValueError("telemetry_config_invalid")
    shared = telemetry.setdefault("shared_metrics", {})
    if not isinstance(shared, dict):
        raise ValueError("shared_metrics_config_invalid")
    current = shared.get("enabled") is True
    if current == enabled:
        return {"owner": owner, "enabled": enabled, "changed": False, "restart_required": False}
    backup = Path(entry.pw_dir) / ".hermes" / "backups" / f"telemetry-config-{time.time_ns()}"
    backup.mkdir(parents=True, mode=0o700)
    os.chown(backup, entry.pw_uid, entry.pw_gid)
    shutil.copy2(config_path, backup / "config.yaml")
    os.chown(backup / "config.yaml", entry.pw_uid, entry.pw_gid)
    os.chmod(backup / "config.yaml", 0o600)
    shared["enabled"] = enabled
    fd, raw = tempfile.mkstemp(prefix=".config.", dir=config_path.parent)
    temp = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(config, handle, allow_unicode=True, sort_keys=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.chown(temp, entry.pw_uid, entry.pw_gid)
        os.chmod(temp, 0o600)
        os.replace(temp, config_path)
    finally:
        if temp.exists():
            temp.unlink()
    return {"owner": owner, "enabled": enabled, "changed": True, "restart_required": True, "backup": str(backup)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("enable", "disable"))
    parser.add_argument("--owner", required=True)
    args = parser.parse_args()
    try:
        result = configure(args.owner, args.action == "enable")
        import json
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        print("error=" + type(exc).__name__, file=__import__("sys").stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
