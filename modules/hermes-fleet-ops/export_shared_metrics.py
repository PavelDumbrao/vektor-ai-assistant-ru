#!/usr/bin/env python3
"""Export one tenant's native privacy-safe SharedMetrics package to local outbox."""
from __future__ import annotations

import json
import os
import pwd
import re
from pathlib import Path
from typing import Any

import yaml

OWNER_RE = re.compile(r"^[a-z][a-z0-9_-]{1,39}$")


class ExportError(RuntimeError):
    pass


def tenant_paths() -> tuple[str, int, Path, Path, Path]:
    uid = os.geteuid()
    if uid == 0:
        raise ExportError("root_refused")
    entry = pwd.getpwuid(uid)
    owner = entry.pw_name
    if not OWNER_RE.fullmatch(owner) or Path(entry.pw_dir) != Path("/home") / owner:
        raise ExportError("tenant_identity_invalid")
    hermes = Path(entry.pw_dir) / ".hermes"
    configured = Path(os.environ.get("HERMES_HOME", ""))
    if configured != hermes or hermes.is_symlink() or not hermes.is_dir():
        raise ExportError("hermes_home_invalid")
    config = hermes / "config.yaml"
    database = hermes / "telemetry/shared_metrics/metrics.sqlite3"
    outbox = hermes / "telemetry/shared_metrics/outbox"
    return owner, uid, hermes, config, database


def telemetry_enabled(config: Path, uid: int) -> bool:
    if config.is_symlink() or not config.is_file():
        raise ExportError("config_invalid")
    info = config.stat()
    if info.st_uid != uid or info.st_mode & 0o077:
        raise ExportError("config_unsafe")
    try:
        payload = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    except Exception:
        raise ExportError("config_invalid") from None
    if not isinstance(payload, dict):
        raise ExportError("config_invalid")
    shared = (payload.get("telemetry") or {}).get("shared_metrics") or {}
    return isinstance(shared, dict) and shared.get("enabled") is True


def database_ready(database: Path, uid: int) -> bool:
    if not database.exists():
        return False
    if database.is_symlink() or not database.is_file():
        raise ExportError("metrics_database_unsafe")
    info = database.stat()
    if info.st_uid != uid or info.st_mode & 0o077:
        raise ExportError("metrics_database_unsafe")
    return True


def export_once() -> dict[str, Any]:
    _, uid, hermes, config, database = tenant_paths()
    if not telemetry_enabled(config, uid):
        return {"status": "disabled", "created": False, "outbox_files": 0}
    if not database_ready(database, uid):
        return {"status": "no_state", "created": False, "outbox_files": 0}

    from hermes_cli.observability.shared_metrics import SharedMetricsStore

    store = SharedMetricsStore()
    created = store.create_and_export_package_if_due()
    outbox = hermes / "telemetry/shared_metrics/outbox"
    files = 0
    if outbox.is_dir() and not outbox.is_symlink():
        files = sum(1 for path in outbox.iterdir() if path.is_file() and not path.is_symlink())
    return {"status": "ok", "created": created is not None, "outbox_files": files}


def main() -> int:
    os.umask(0o077)
    try:
        result = export_once()
        print(json.dumps(result, sort_keys=True))
        return 0
    except ExportError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, sort_keys=True))
        return 1
    except Exception as exc:
        print(json.dumps({"status": "error", "error": type(exc).__name__}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
