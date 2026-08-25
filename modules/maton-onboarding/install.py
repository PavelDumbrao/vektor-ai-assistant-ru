#!/usr/bin/env python3
"""Install the standalone Maton onboarding plugin into one Hermes profile."""

from __future__ import annotations

import argparse
import os
import shutil
import tempfile
import time
from pathlib import Path

import yaml


PLUGIN_NAME = "maton-onboarding"
DEFAULT_MATON = {
    "enabled": False,
    "url": "https://mcp.maton.ai",
    "headers": {"Authorization": "Bearer ${MCP_MATON_API_KEY}"},
    "connect_timeout": 60,
    "tools": {
        "include": [
            "whoami",
            "search_apps",
            "search_actions",
            "get_action",
            "create_connection",
            "get_connection",
            "list_connections",
            "run_action",
        ]
    },
}


def _atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    if path.is_symlink():
        raise RuntimeError(f"unsafe symlink: {path}")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        os.chmod(path, mode)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _profile_has_key(home: Path) -> bool:
    env_path = home / ".env"
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("MCP_MATON_API_KEY=") and line.partition("=")[2].strip():
                return True
    except OSError:
        pass
    return bool(os.environ.get("MCP_MATON_API_KEY", "").strip())


def install(hermes_home: Path) -> Path:
    home = hermes_home.expanduser().resolve()
    config_path = home / "config.yaml"
    if not home.is_dir() or config_path.is_symlink() or not config_path.is_file():
        raise RuntimeError("Hermes profile is missing or unsafe")

    source = Path(__file__).resolve().parent / "plugin"
    if not (source / "__init__.py").is_file() or not (source / "plugin.yaml").is_file():
        raise RuntimeError("Maton onboarding plugin source is incomplete")

    plugin_root = home / "plugins"
    plugin_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(plugin_root, 0o700)
    target = plugin_root / PLUGIN_NAME
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise RuntimeError("Maton onboarding target is unsafe")

    backup = home / "backups" / f"maton-onboarding-install-{time.time_ns()}"
    backup.mkdir(mode=0o700, parents=True)
    shutil.copy2(config_path, backup / "config.yaml")
    os.chmod(backup / "config.yaml", 0o600)
    if target.is_dir():
        shutil.copytree(target, backup / "plugin")

    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(config, dict):
        raise RuntimeError("config.yaml must be a mapping")
    plugins = config.setdefault("plugins", {})
    enabled = plugins.setdefault("enabled", [])
    if not isinstance(enabled, list):
        raise RuntimeError("plugins.enabled must be a list")
    if PLUGIN_NAME not in enabled:
        enabled.append(PLUGIN_NAME)
    disabled = plugins.setdefault("disabled", [])
    if isinstance(disabled, list):
        plugins["disabled"] = [name for name in disabled if name != PLUGIN_NAME]

    servers = config.setdefault("mcp_servers", {})
    if not isinstance(servers, dict):
        raise RuntimeError("mcp_servers must be a mapping")
    existing = servers.get("maton")
    if existing is None:
        servers["maton"] = {
            **DEFAULT_MATON,
            "enabled": _profile_has_key(home),
        }
    elif isinstance(existing, dict):
        merged = DEFAULT_MATON.copy()
        merged.update(existing)
        if not _profile_has_key(home):
            merged["enabled"] = False
        servers["maton"] = merged
    else:
        raise RuntimeError("mcp_servers.maton must be a mapping")

    platforms = config.setdefault("platforms", {})
    telegram = platforms.setdefault("telegram", {})
    extra = telegram.setdefault("extra", {})
    command_menu = extra.setdefault("command_menu", {})
    priority = command_menu.setdefault("priority", [])
    if not isinstance(priority, list):
        raise RuntimeError("platforms.telegram.extra.command_menu.priority must be a list")
    command_menu["priority"] = ["maton", *[item for item in priority if item != "maton"]]

    staged = Path(tempfile.mkdtemp(prefix=".maton-onboarding.", dir=plugin_root))
    try:
        for filename in ("__init__.py", "plugin.yaml"):
            shutil.copy2(source / filename, staged / filename)
            os.chmod(staged / filename, 0o600)
        os.chmod(staged, 0o700)
        if target.exists():
            shutil.rmtree(target)
        os.replace(staged, target)
        _atomic_write(
            config_path,
            yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
        )
    except Exception:
        if staged.exists():
            shutil.rmtree(staged, ignore_errors=True)
        shutil.copy2(backup / "config.yaml", config_path)
        os.chmod(config_path, 0o600)
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        if (backup / "plugin").is_dir():
            shutil.copytree(backup / "plugin", target)
        raise
    return backup


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-home", required=True)
    args = parser.parse_args()
    home = Path(args.hermes_home).expanduser().resolve()
    backup = install(home)
    print("installed=true")
    print("plugin=maton-onboarding")
    print(f"maton_mcp_enabled={str(_profile_has_key(home)).lower()}")
    print("telegram_command=/maton")
    print(f"backup={backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
