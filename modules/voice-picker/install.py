#!/usr/bin/env python3
"""Install the standalone voice-picker plugin into one Hermes profile."""

from __future__ import annotations

import argparse
import os
import shutil
import tempfile
import time
from pathlib import Path

import yaml


PLUGIN_NAME = "voice-picker"


def _atomic_write(
    path: Path, text: str, mode: int = 0o600, uid: int | None = None, gid: int | None = None
) -> None:
    if path.is_symlink():
        raise RuntimeError(f"unsafe symlink: {path}")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        if uid is not None and gid is not None:
            try:
                os.fchown(fd, uid, gid)
            except (PermissionError, OSError):
                pass
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        os.chmod(path, mode)
        if uid is not None and gid is not None:
            try:
                os.chown(path, uid, gid)
            except (PermissionError, OSError):
                pass
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _safe_chown(path: Path, uid: int, gid: int) -> None:
    try:
        os.chown(path, uid, gid)
    except (PermissionError, OSError):
        pass


def install(hermes_home: Path) -> Path:
    home = hermes_home.expanduser().resolve()
    config_path = home / "config.yaml"
    if not home.is_dir() or config_path.is_symlink() or not config_path.is_file():
        raise RuntimeError("Hermes profile is missing or unsafe")

    source = Path(__file__).resolve().parent / "plugin"
    if not (source / "__init__.py").is_file() or not (source / "plugin.yaml").is_file():
        raise RuntimeError("voice-picker plugin source is incomplete")

    home_stat = home.stat()
    home_uid = home_stat.st_uid
    home_gid = home_stat.st_gid

    plugin_root = home / "plugins"
    plugin_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(plugin_root, 0o700)
    _safe_chown(plugin_root, home_uid, home_gid)

    target = plugin_root / PLUGIN_NAME
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise RuntimeError("voice-picker target is unsafe")

    backup = home / "backups" / f"voice-picker-install-{time.time_ns()}"
    backup.mkdir(mode=0o700, parents=True)
    _safe_chown(backup, home_uid, home_gid)

    shutil.copy2(config_path, backup / "config.yaml")
    os.chmod(backup / "config.yaml", 0o600)
    _safe_chown(backup / "config.yaml", home_uid, home_gid)

    if target.is_dir():
        shutil.copytree(target, backup / "plugin")

    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(config, dict):
        raise RuntimeError("config.yaml must be a mapping")

    plugins = config.setdefault("plugins", {})
    if not isinstance(plugins, dict):
        raise RuntimeError("plugins must be a mapping")

    enabled = plugins.setdefault("enabled", [])
    if not isinstance(enabled, list):
        raise RuntimeError("plugins.enabled must be a list")
    if PLUGIN_NAME not in enabled:
        enabled.append(PLUGIN_NAME)

    disabled = plugins.setdefault("disabled", [])
    if isinstance(disabled, list):
        plugins["disabled"] = [name for name in disabled if name != PLUGIN_NAME]

    staged = Path(tempfile.mkdtemp(prefix=".voice-picker.", dir=plugin_root))
    try:
        for filename in ("__init__.py", "plugin.yaml"):
            dest = staged / filename
            shutil.copy2(source / filename, dest)
            os.chmod(dest, 0o600)
            _safe_chown(dest, home_uid, home_gid)
        os.chmod(staged, 0o700)
        _safe_chown(staged, home_uid, home_gid)

        if target.exists():
            shutil.rmtree(target)
        os.replace(staged, target)
        os.chmod(target, 0o700)
        _safe_chown(target, home_uid, home_gid)

        for filename in ("__init__.py", "plugin.yaml"):
            dest = target / filename
            if dest.is_file():
                os.chmod(dest, 0o600)
                _safe_chown(dest, home_uid, home_gid)

        _atomic_write(
            config_path,
            yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
            mode=0o600,
            uid=home_uid,
            gid=home_gid,
        )
    except Exception:
        if staged.exists():
            shutil.rmtree(staged, ignore_errors=True)
        shutil.copy2(backup / "config.yaml", config_path)
        os.chmod(config_path, 0o600)
        _safe_chown(config_path, home_uid, home_gid)
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
    print("plugin=voice-picker")
    print("telegram_command=/golos")
    print("restart_required=true")
    print(f"backup={backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
