#!/usr/bin/env python3
"""Idempotent Maton MCP provisioning for one Hermes client profile.

The API key is accepted only through an interactive hidden prompt. It is never
accepted as a command-line argument or environment variable, so it does not
land in shell history, process listings, or deployment logs.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import pwd
import shutil
import stat
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


MATON_URL = "https://mcp.maton.ai"
MATON_VALIDATION_URL = "https://api.maton.ai/connections"
ENV_KEY = "MCP_MATON_API_KEY"
SAFE_TOOLS = [
    "whoami",
    "search_apps",
    "search_actions",
    "get_action",
    "create_connection",
    "get_connection",
    "list_connections",
    "run_action",
]


class ProvisionError(RuntimeError):
    pass


def _reject_symlink(path: Path) -> None:
    if path.is_symlink():
        raise ProvisionError(f"Refusing to write through symlink: {path}")


def _read_yaml(path: Path) -> dict[str, Any]:
    _reject_symlink(path)
    if not path.exists():
        return {}
    value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(value, dict):
        raise ProvisionError(f"Expected a YAML mapping in {path}")
    return value


def _owner_ids(owner: str | None) -> tuple[int, int] | None:
    if not owner:
        return None
    try:
        entry = pwd.getpwnam(owner)
    except KeyError as exc:
        raise ProvisionError(f"Unknown Linux user: {owner}") from exc
    return entry.pw_uid, entry.pw_gid


def _atomic_write(path: Path, content: str, mode: int, owner: str | None) -> None:
    _reject_symlink(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temp = Path(temp_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        ids = _owner_ids(owner)
        if ids and os.geteuid() == 0:
            os.chown(temp, *ids)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def _backup(path: Path, backup_dir: Path, owner: str | None) -> Path | None:
    if not path.exists():
        return None
    _reject_symlink(path)
    backup_root = backup_dir.parent
    _reject_symlink(backup_root)
    backup_root.mkdir(parents=True, exist_ok=True)
    ids = _owner_ids(owner)
    if ids and os.geteuid() == 0:
        os.chown(backup_root, *ids)
    if not os.access(backup_root, os.W_OK | os.X_OK):
        raise ProvisionError(f"Backup directory is not writable: {backup_root}")
    backup_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(backup_dir, 0o700)
    if ids and os.geteuid() == 0:
        os.chown(backup_dir, *ids)
    target = backup_dir / path.name
    shutil.copy2(path, target)
    if ids and os.geteuid() == 0:
        os.chown(target, *ids)
    return target


def _server_entry(*, enabled: bool) -> dict[str, Any]:
    return {
        "enabled": enabled,
        "url": MATON_URL,
        "headers": {"Authorization": f"Bearer ${{{ENV_KEY}}}"},
        "connect_timeout": 60,
        "tools": {"include": list(SAFE_TOOLS)},
    }


def _dump_yaml(data: dict[str, Any]) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


def _set_maton_config(config_path: Path, *, enabled: bool, owner: str | None) -> None:
    config = _read_yaml(config_path)
    servers = config.setdefault("mcp_servers", {})
    if not isinstance(servers, dict):
        raise ProvisionError("config.yaml mcp_servers must be a mapping")
    servers["maton"] = _server_entry(enabled=enabled)
    _atomic_write(config_path, _dump_yaml(config), 0o600, owner)


def _parse_env(path: Path) -> list[tuple[str | None, str]]:
    _reject_symlink(path)
    if not path.exists():
        return []
    parsed: list[tuple[str | None, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if stripped and not stripped.startswith("#") and "=" in raw:
            key = raw.split("=", 1)[0].strip()
            parsed.append((key, raw))
        else:
            parsed.append((None, raw))
    return parsed


def _env_has_key(path: Path) -> bool:
    for key, raw in _parse_env(path):
        if key == ENV_KEY:
            return bool(raw.split("=", 1)[1].strip())
    return False


def _set_env_key(path: Path, value: str, owner: str | None) -> None:
    lines = _parse_env(path)
    output: list[str] = []
    replaced = False
    for key, raw in lines:
        if key == ENV_KEY:
            if not replaced:
                output.append(f"{ENV_KEY}={value}")
                replaced = True
            continue
        output.append(raw)
    if not replaced:
        if output and output[-1] != "":
            output.append("")
        output.append(f"{ENV_KEY}={value}")
    _atomic_write(path, "\n".join(output).rstrip("\n") + "\n", 0o600, owner)


def _validate_key(key: str, timeout: float = 15.0) -> None:
    if not key or not 20 <= len(key) <= 8192 or any(ch.isspace() for ch in key):
        raise ProvisionError("The Maton API key format is invalid")
    request = urllib.request.Request(
        MATON_VALIDATION_URL,
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(262_145)
            if len(raw) > 262_144:
                raise ProvisionError("Maton returned an oversized validation response")
            payload = json.loads(raw)
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise ProvisionError("Maton rejected the API key") from exc
        raise ProvisionError(f"Maton validation failed with HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProvisionError("Maton validation endpoint is unavailable or invalid") from exc
    if not isinstance(payload, (dict, list)):
        raise ProvisionError("Maton returned an invalid validation response")


def activate_with_key(
    *,
    hermes_home: str | Path,
    owner: str | None,
    key: str,
    timeout: float = 15.0,
) -> Path:
    """Validate and persist a Maton key received through a trusted channel.

    This function is intentionally not exposed as a command-line key argument.
    It is used by the local HTTPS onboarding portal after the browser request
    has passed its one-time-token and CSRF checks.
    """
    home = Path(hermes_home).resolve()
    config = home / "config.yaml"
    env_path = home / ".env"
    _validate_key(key, timeout=timeout)
    backups = _backup_dir(home)
    _backup(config, backups, owner)
    _backup(env_path, backups, owner)
    try:
        _set_env_key(env_path, key, owner)
        _set_maton_config(config, enabled=True, owner=owner)
        _install_skill(Path(__file__).resolve().parent, home, owner)
    except Exception as exc:
        restore_from_backup(hermes_home=home, backup_dir=backups, owner=owner)
        if isinstance(exc, ProvisionError):
            raise
        raise ProvisionError("Failed to persist the Maton configuration") from exc
    return backups


def restore_from_backup(
    *, hermes_home: str | Path, backup_dir: str | Path, owner: str | None
) -> None:
    """Restore the private config and env snapshot created before activation."""
    home = Path(hermes_home).resolve()
    backup = Path(backup_dir).resolve()
    expected_parent = (home / "backups").resolve()
    if backup.parent != expected_parent or not backup.is_dir():
        raise ProvisionError("Refusing to restore from an unexpected backup path")
    for name in ("config.yaml", ".env"):
        source = backup / name
        if source.is_file():
            _atomic_write(home / name, source.read_text(encoding="utf-8"), 0o600, owner)


def _install_skill(module_dir: Path, hermes_home: Path, owner: str | None) -> Path:
    source = module_dir / "skill" / "SKILL.md"
    if not source.is_file():
        raise ProvisionError(f"Bundled skill is missing: {source}")
    target = hermes_home / "skills" / "integrations" / "maton-client-integrations" / "SKILL.md"
    content = source.read_text(encoding="utf-8")
    _reject_symlink(target)

    # Activation runs as the isolated client user, while the initial prepare
    # command may run as root.  Do not require write access to the directory
    # merely to reinstall an already identical skill: tempfile.mkstemp() would
    # otherwise fail when an older installer left the directory root-owned.
    if target.is_file() and target.read_text(encoding="utf-8") == content and os.geteuid() != 0:
        return target

    directories = (
        hermes_home / "skills",
        hermes_home / "skills" / "integrations",
        target.parent,
    )
    ids = _owner_ids(owner)
    for directory in directories:
        _reject_symlink(directory)
        directory.mkdir(parents=True, exist_ok=True)
        if not directory.is_dir():
            raise ProvisionError(f"Expected a skill directory: {directory}")
        if ids and os.geteuid() == 0:
            os.chown(directory, *ids)

    if target.is_file() and target.read_text(encoding="utf-8") == content:
        if ids and os.geteuid() == 0:
            os.chown(target, *ids)
        return target

    _atomic_write(target, content, 0o600, owner)
    return target


def _backup_dir(hermes_home: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return hermes_home / "backups" / f"maton-client-{stamp}"


def prepare(args: argparse.Namespace) -> None:
    home = Path(args.hermes_home).resolve()
    config = home / "config.yaml"
    backups = _backup_dir(home)
    _backup(config, backups, args.owner)
    _set_maton_config(config, enabled=False, owner=args.owner)
    skill = _install_skill(Path(__file__).resolve().parent, home, args.owner)
    print(f"prepared=true\nactive=false\nskill={skill}\nbackup={backups}")


def activate(args: argparse.Namespace) -> None:
    key = getpass.getpass("Maton API key (hidden): ").strip()
    backups = activate_with_key(
        hermes_home=args.hermes_home,
        owner=args.owner,
        key=key,
        timeout=args.timeout,
    )
    key = ""
    print(f"validated=true\nactive=true\nbackup={backups}")


def deactivate(args: argparse.Namespace) -> None:
    home = Path(args.hermes_home).resolve()
    config = home / "config.yaml"
    backups = _backup_dir(home)
    _backup(config, backups, args.owner)
    _set_maton_config(config, enabled=False, owner=args.owner)
    print(f"active=false\nkey_retained=true\nbackup={backups}")


def status(args: argparse.Namespace) -> None:
    home = Path(args.hermes_home).resolve()
    config_path = home / "config.yaml"
    env_path = home / ".env"
    config = _read_yaml(config_path)
    entry = (config.get("mcp_servers") or {}).get("maton") or {}
    skill = home / "skills" / "integrations" / "maton-client-integrations" / "SKILL.md"
    print(f"prepared={bool(entry)}")
    print(f"active={entry.get('enabled') is True}")
    print(f"key_present={_env_has_key(env_path)}")
    print(f"skill_present={skill.is_file()}")
    print(f"safe_tool_filter={((entry.get('tools') or {}).get('include') == SAFE_TOOLS)}")
    if env_path.exists():
        mode = stat.S_IMODE(env_path.stat().st_mode)
        print(f"env_mode={mode:04o}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "activate", "status", "deactivate"))
    parser.add_argument("--hermes-home", required=True)
    parser.add_argument("--owner", help="Linux owner for generated client files")
    parser.add_argument("--timeout", type=float, default=15.0)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        {"prepare": prepare, "activate": activate, "status": status, "deactivate": deactivate}[args.action](args)
    except ProvisionError as exc:
        print(f"error={exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
