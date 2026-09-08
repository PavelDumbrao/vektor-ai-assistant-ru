#!/usr/bin/env python3
"""Install the focus_assistant plugin into one Hermes home."""

from __future__ import annotations

import argparse
import os
import shutil
import tempfile
import time
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent
PLUGIN_SOURCE = ROOT / "plugin"
WATCH_SOURCE = ROOT / "scripts" / "fathom_watch.py"
AGENTS_TEMPLATE = ROOT / "templates" / "AGENTS.md"


def _atomic_yaml(path: Path, data: dict) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=".config.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def install(home: Path) -> dict:
    home = home.expanduser().resolve()
    if not home.is_dir() or not (home / "config.yaml").is_file():
        raise RuntimeError("Hermes home/config.yaml is missing")
    required = [
        PLUGIN_SOURCE / "__init__.py",
        PLUGIN_SOURCE / "ledger.py",
        PLUGIN_SOURCE / "fathom.py",
        PLUGIN_SOURCE / "integrations.py",
        PLUGIN_SOURCE / "owner.py",
        PLUGIN_SOURCE / "intake.py",
        PLUGIN_SOURCE / "workspace.py",
        PLUGIN_SOURCE / "brief.py",
        PLUGIN_SOURCE / "meetings.py",
        PLUGIN_SOURCE / "voice.py",
        PLUGIN_SOURCE / "mail.py",
        PLUGIN_SOURCE / "projects.py",
        PLUGIN_SOURCE / "weekly.py",
        PLUGIN_SOURCE / "plugin.yaml",
        WATCH_SOURCE,
    ]
    if not all(path.is_file() for path in required):
        raise RuntimeError("focus_assistant source is incomplete")

    backup = home / "backups" / f"focus-assistant-install-{time.strftime('%Y%m%dT%H%M%S%z')}"
    backup.mkdir(parents=True, exist_ok=False)
    target = home / "plugins" / "focus_assistant"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.copytree(target, backup / "plugin")
    shutil.copy2(home / "config.yaml", backup / "config.yaml")

    staged = Path(tempfile.mkdtemp(prefix=".focus-assistant.", dir=target.parent))
    try:
        for source in PLUGIN_SOURCE.iterdir():
            if source.is_file():
                shutil.copy2(source, staged / source.name)
        if target.exists():
            archived = backup / "plugin-live"
            os.replace(target, archived)
        os.replace(staged, target)
    finally:
        if staged.exists():
            shutil.rmtree(staged)

    config_path = home / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    plugins = config.setdefault("plugins", {})
    enabled = plugins.setdefault("enabled", [])
    if "focus_assistant" not in enabled:
        enabled.append("focus_assistant")
    _atomic_yaml(config_path, config)

    focus = home / "focus"
    scripts = home / "scripts"
    focus.mkdir(parents=True, exist_ok=True)
    scripts.mkdir(parents=True, exist_ok=True)
    focus.chmod(0o700)
    scripts.chmod(0o700)
    if not (focus / "AGENTS.md").exists():
        shutil.copy2(AGENTS_TEMPLATE, focus / "AGENTS.md")
    watch_target = scripts / "fathom_watch.py"
    if watch_target.exists():
        shutil.copy2(watch_target, backup / "fathom_watch.py")
    shutil.copy2(WATCH_SOURCE, watch_target)
    watch_target.chmod(0o700)
    for path in target.iterdir():
        if path.is_file():
            path.chmod(0o600)
    target.chmod(0o700)
    return {"plugin": str(target), "watcher": str(watch_target), "backup": str(backup)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-home", required=True)
    args = parser.parse_args()
    result = install(Path(args.hermes_home))
    for key, value in result.items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
