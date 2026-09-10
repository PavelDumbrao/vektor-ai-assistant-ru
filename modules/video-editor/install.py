#!/usr/bin/env python3
"""Install the safe shared video-editor toolset into one Hermes profile."""
from __future__ import annotations

import argparse
import os
import pwd
import re
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path

import yaml

PLUGIN_KEY = "video-editor"
TOOLSET = "video_editor"
RUNTIME = Path("/opt/vektor/video-editor")
ENGINE_COMMIT = "e8ea406bc2440ca8fc8d1b239c8758e9de112388"
PROFILE_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")


def safe_owned_dir(path: Path, uid: int, gid: int, *, create: bool = False) -> Path:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if not create:
            raise RuntimeError("profile_directory_missing")
        path.mkdir(mode=0o700)
        os.chown(path, uid, gid)
        os.chmod(path, 0o700)
        info = path.lstat()
    if path.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid != uid:
        raise RuntimeError("profile_directory_unsafe")
    return path


def safe_owned_file(path: Path, uid: int) -> Path:
    try:
        info = path.lstat()
    except OSError as exc:
        raise RuntimeError("profile_file_missing") from exc
    if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid != uid or (info.st_mode & 0o022):
        raise RuntimeError("profile_file_unsafe")
    return path


def atomic_yaml(path: Path, data: dict, uid: int, gid: int) -> None:
    fd, name = tempfile.mkstemp(prefix=".config-", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(data, handle, allow_unicode=True, sort_keys=False)
            handle.flush(); os.fsync(handle.fileno())
        os.chown(temp, uid, gid); os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def copy_owned(source: Path, target: Path, uid: int, gid: int) -> None:
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_dir():
            raise RuntimeError("unsafe_existing_target")
        shutil.rmtree(target)
    shutil.copytree(source, target)
    for item in [target, *target.rglob("*")]:
        os.chown(item, uid, gid)
        if item.is_dir():
            item.chmod(0o700)
        elif item.is_file():
            item.chmod(0o600)


def require_profile_pillow(hermes: Path) -> None:
    python = hermes / "hermes-agent" / "venv" / "bin" / "python"
    if not python.is_file():
        raise RuntimeError("hermes_runtime_python_missing")
    try:
        proc = subprocess.run(
            [str(python), "-c", "from PIL import Image, ImageDraw, ImageFont, ImageOps"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("hermes_pillow_preflight_failed") from exc
    if proc.returncode != 0:
        raise RuntimeError("hermes_pillow_missing")


def install(owner: str) -> None:
    if os.geteuid() != 0:
        raise RuntimeError("root_required")
    if not PROFILE_RE.fullmatch(owner):
        raise RuntimeError("invalid_owner")
    entry = pwd.getpwnam(owner)
    if entry.pw_uid <= 0 or Path(entry.pw_dir).name != owner:
        raise RuntimeError("invalid_owner")
    if not (RUNTIME / "engine" / ENGINE_COMMIT / "scripts" / "render.py").is_file():
        raise RuntimeError("video_runtime_missing")
    if not (RUNTIME / "bin" / "whisper-cli").exists():
        raise RuntimeError("video_runtime_incomplete")
    if not all((RUNTIME / "models" / name).is_file() for name in ("ggml-small.bin", "ggml-medium.bin")):
        raise RuntimeError("video_runtime_models_incomplete")

    home = safe_owned_dir(Path(entry.pw_dir), entry.pw_uid, entry.pw_gid)
    hermes = safe_owned_dir(home / ".hermes", entry.pw_uid, entry.pw_gid)
    require_profile_pillow(hermes)
    config_path = safe_owned_file(hermes / "config.yaml", entry.pw_uid)
    plugin_source = Path(__file__).resolve().parent / "plugin"
    skill_source = Path(__file__).resolve().parent / "skill"
    if not (plugin_source / "plugin.yaml").is_file() or not (skill_source / "SKILL.md").is_file():
        raise RuntimeError("module_source_incomplete")

    plugin_root = safe_owned_dir(hermes / "plugins", entry.pw_uid, entry.pw_gid, create=True)
    skill_root = safe_owned_dir(hermes / "skills", entry.pw_uid, entry.pw_gid, create=True)
    backups_root = safe_owned_dir(hermes / "backups", entry.pw_uid, entry.pw_gid, create=True)
    plugin_target = plugin_root / PLUGIN_KEY
    skill_target = skill_root / PLUGIN_KEY
    backup = backups_root / f"video-editor-install-{time.time_ns()}"
    backup.mkdir(mode=0o700)
    os.chown(backup, entry.pw_uid, entry.pw_gid)
    shutil.copy2(config_path, backup / "config.yaml")
    if plugin_target.is_dir() and not plugin_target.is_symlink():
        shutil.copytree(plugin_target, backup / "plugin")
    if skill_target.is_dir() and not skill_target.is_symlink():
        shutil.copytree(skill_target, backup / "skill")

    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(config, dict):
        raise RuntimeError("config_invalid")
    plugins = config.setdefault("plugins", {})
    enabled = plugins.setdefault("enabled", [])
    if not isinstance(enabled, list):
        raise RuntimeError("plugins_enabled_invalid")
    if PLUGIN_KEY not in enabled:
        enabled.append(PLUGIN_KEY)
    agent = config.setdefault("agent", {})
    disabled = agent.setdefault("disabled_toolsets", [])
    if not isinstance(disabled, list):
        raise RuntimeError("disabled_toolsets_invalid")
    agent["disabled_toolsets"] = [item for item in disabled if item != TOOLSET]

    try:
        copy_owned(plugin_source, plugin_target, entry.pw_uid, entry.pw_gid)
        copy_owned(skill_source, skill_target, entry.pw_uid, entry.pw_gid)
        atomic_yaml(config_path, config, entry.pw_uid, entry.pw_gid)
    except Exception:
        shutil.copy2(backup / "config.yaml", config_path)
        os.chown(config_path, entry.pw_uid, entry.pw_gid); os.chmod(config_path, 0o600)
        for target in (plugin_target, skill_target):
            if target.is_dir() and not target.is_symlink(): shutil.rmtree(target)
        if (backup / "plugin").is_dir(): shutil.copytree(backup / "plugin", plugin_target)
        if (backup / "skill").is_dir(): shutil.copytree(backup / "skill", skill_target)
        raise
    for d in (plugin_root, skill_root, backup):
        os.chown(d, entry.pw_uid, entry.pw_gid)
    print("installed=true")
    print(f"owner={owner}")
    print(f"plugin={PLUGIN_KEY}")
    print("terminal_access_added=false")
    print("restart_required=true")


def main() -> int:
    ap=argparse.ArgumentParser(); ap.add_argument("--owner", required=True); args=ap.parse_args()
    try: install(args.owner)
    except Exception as exc:
        print(f"error={type(exc).__name__}:{exc}", file=__import__("sys").stderr); return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
