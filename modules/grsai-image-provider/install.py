#!/usr/bin/env python3
"""Install the profile-local GRSAI GPT Image 2.5 backend."""

from __future__ import annotations

import argparse
import os
import pwd
import shutil
import tempfile
import time
from pathlib import Path

import yaml

PLUGIN_KEY = "image_gen/grsai"
PROVIDER = "grsai"
MODEL = "gpt-image-2.5"


def _private_env_has_key(path: Path, uid: int) -> bool:
    info = path.lstat()
    if not path.is_file() or path.is_symlink() or info.st_uid != uid:
        raise RuntimeError("unsafe_env")
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("GRSAI_API_KEY=") and line.partition("=")[2].strip():
            return True
    return False

def _atomic_yaml(path: Path, data: dict, uid: int, gid: int) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=".config.", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(data, handle, allow_unicode=True, sort_keys=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.chown(temp, uid, gid)
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def install(owner: str) -> None:
    if os.geteuid() != 0:
        raise RuntimeError("root_required")
    entry = pwd.getpwnam(owner)
    if entry.pw_uid <= 0:
        raise RuntimeError("invalid_owner")
    home = Path(entry.pw_dir)
    hermes = home / ".hermes"
    config_path = hermes / "config.yaml"
    env_path = hermes / ".env"
    if not hermes.is_dir() or not config_path.is_file() or config_path.is_symlink():
        raise RuntimeError("profile_missing_or_unsafe")
    if not _private_env_has_key(env_path, entry.pw_uid):
        raise RuntimeError("grsai_key_missing")
    source = Path(__file__).resolve().parent / "plugin"
    if not (source / "__init__.py").is_file() or not (source / "plugin.yaml").is_file():
        raise RuntimeError("plugin_source_incomplete")

    plugin_root = hermes / "plugins" / "image_gen"
    plugin_root.mkdir(parents=True, exist_ok=True)
    os.chown(plugin_root, entry.pw_uid, entry.pw_gid)
    os.chmod(plugin_root, 0o700)
    target = plugin_root / "grsai"
    backup = hermes / "backups" / f"grsai-image-provider-install-{time.time_ns()}"
    backup.mkdir(parents=True, mode=0o700)
    shutil.copy2(config_path, backup / "config.yaml")
    if target.is_dir() and not target.is_symlink():
        shutil.copytree(target, backup / "plugin")
    elif target.exists() or target.is_symlink():
        raise RuntimeError("plugin_target_unsafe")

    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(config, dict):
        raise RuntimeError("config_invalid")
    agent = config.setdefault("agent", {})
    disabled = agent.setdefault("disabled_toolsets", [])
    if not isinstance(disabled, list):
        raise RuntimeError("disabled_toolsets_invalid")
    agent["disabled_toolsets"] = [item for item in disabled if item != "image_gen"]
    config["image_gen"] = {"provider": PROVIDER, "model": MODEL}
    plugins = config.setdefault("plugins", {})
    enabled = plugins.setdefault("enabled", [])
    if not isinstance(enabled, list):
        raise RuntimeError("plugins_enabled_invalid")
    if PLUGIN_KEY not in enabled:
        enabled.append(PLUGIN_KEY)

    staged = Path(tempfile.mkdtemp(prefix=".grsai.", dir=plugin_root))
    try:
        for name in ("__init__.py", "plugin.yaml"):
            shutil.copy2(source / name, staged / name)
            os.chown(staged / name, entry.pw_uid, entry.pw_gid)
            os.chmod(staged / name, 0o600)
        os.chown(staged, entry.pw_uid, entry.pw_gid)
        os.chmod(staged, 0o700)
        if target.is_dir():
            shutil.rmtree(target)
        os.replace(staged, target)
        _atomic_yaml(config_path, config, entry.pw_uid, entry.pw_gid)
    except Exception:
        if staged.exists():
            shutil.rmtree(staged, ignore_errors=True)
        shutil.copy2(backup / "config.yaml", config_path)
        os.chown(config_path, entry.pw_uid, entry.pw_gid)
        os.chmod(config_path, 0o600)
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        if (backup / "plugin").is_dir():
            shutil.copytree(backup / "plugin", target)
        raise
    for directory in (plugin_root, target, backup):
        os.chown(directory, entry.pw_uid, entry.pw_gid)
        os.chmod(directory, 0o700)
    print("installed=true")
    print(f"owner={owner}")
    print(f"provider={PROVIDER}")
    print(f"model={MODEL}")
    print("secret_printed=false")
    print("restart_required=true")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", required=True)
    args = parser.parse_args()
    try:
        install(args.owner)
    except Exception as exc:
        print(f"error={type(exc).__name__}", file=__import__("sys").stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
