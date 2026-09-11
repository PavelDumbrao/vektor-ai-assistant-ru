#!/usr/bin/env python3
"""Enroll validated Hermes profiles into per-tenant SharedMetrics export timers."""
from __future__ import annotations

import json
import os
import pwd
import re
import subprocess
from pathlib import Path

PROFILE_ROOT = Path("/opt/vektor/profiles")
SYSTEMCTL = "/usr/bin/systemctl"
UNIT_PREFIX = "proai-hermes-shared-metrics-export@"
OWNER_RE = re.compile(r"^[a-z][a-z0-9_-]{1,39}$")
MAX_PROFILES = 5000


class EnrollError(RuntimeError):
    pass


def profile_owners() -> list[str]:
    if PROFILE_ROOT.is_symlink() or not PROFILE_ROOT.is_dir():
        raise EnrollError("profile_root_unsafe")
    result: list[str] = []
    for path in sorted(PROFILE_ROOT.glob("*.json")):
        if path.is_symlink() or not path.is_file():
            raise EnrollError("profile_registry_unsafe")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            raise EnrollError("profile_registry_invalid") from None
        owner = str(payload.get("owner") or "") if isinstance(payload, dict) else ""
        if not OWNER_RE.fullmatch(owner) or path.stem != owner:
            raise EnrollError("profile_owner_invalid")
        try:
            entry = pwd.getpwnam(owner)
        except KeyError:
            raise EnrollError("profile_account_missing") from None
        if entry.pw_uid <= 0 or Path(entry.pw_dir) != Path("/home") / owner:
            raise EnrollError("profile_account_invalid")
        result.append(owner)
        if len(result) > MAX_PROFILES:
            raise EnrollError("profile_limit_exceeded")
    return result


def timer_unit(owner: str) -> str:
    if not OWNER_RE.fullmatch(owner):
        raise EnrollError("profile_owner_invalid")
    return f"{UNIT_PREFIX}{owner}.timer"

def is_enabled(unit: str) -> bool:
    result = subprocess.run(
        [SYSTEMCTL, "is-enabled", "--quiet", unit],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=15, check=False,
    )
    return result.returncode == 0


def enroll() -> dict[str, int]:
    if os.geteuid() != 0:
        raise EnrollError("root_required")
    owners = profile_owners()
    enrolled = already = failed = 0
    for owner in owners:
        unit = timer_unit(owner)
        if is_enabled(unit):
            already += 1
            continue
        result = subprocess.run(
            [SYSTEMCTL, "enable", "--now", unit],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=30, check=False,
        )
        if result.returncode == 0:
            enrolled += 1
        else:
            failed += 1
    return {"profiles": len(owners), "enrolled": enrolled, "already": already, "failed": failed}

def main() -> int:
    try:
        result = enroll()
        print(json.dumps({"ok": result["failed"] == 0, **result}, sort_keys=True))
        return 0 if result["failed"] == 0 else 1
    except EnrollError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
