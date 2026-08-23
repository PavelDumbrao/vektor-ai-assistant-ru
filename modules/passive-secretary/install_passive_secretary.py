#!/usr/bin/env python3
"""Install a disabled, tenant-scoped Hermes Passive Secretary module."""

from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import yaml


PLUGIN_NAME = "passive-secretary"
ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,127}$")
TELEGRAM_ID_MAX = 2**63 - 1
PLUGIN_FILES = (
    "__init__.py",
    "archive.py",
    "controller.py",
    "normalizer.py",
    "owner_intent.py",
    "outbound.py",
    "plugin.yaml",
    "retrieval.py",
    "schema.sql",
    "settings.py",
)
OPERATOR_FILES = (
    "history_backfill.py",
    "legacy_media_seed.py",
)


class InstallError(RuntimeError):
    pass


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_regular_file(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise InstallError("Durability target is unsafe")
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_tree(path: Path) -> None:
    directories: list[Path] = []
    for root, _directory_names, filenames in os.walk(path, followlinks=False):
        directory = Path(root)
        directories.append(directory)
        for filename in filenames:
            candidate = directory / filename
            try:
                info = candidate.lstat()
            except OSError as exc:
                raise InstallError("Backup durability verification failed") from exc
            if stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                _fsync_regular_file(candidate)
    for directory in reversed(directories):
        _fsync_directory(directory)
    _fsync_directory(path.parent)


def _require_root_owned_path(path: Path) -> None:
    """Reject launch/source paths writable by the target account."""
    if not path.is_absolute():
        raise InstallError("Installer path must be absolute")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise InstallError("Installer path is missing or unsafe") from exc
    candidates = [path, resolved]
    for candidate in dict.fromkeys(candidates):
        current = Path(candidate.anchor)
        for part in candidate.parts[1:]:
            current /= part
            try:
                info = current.lstat()
            except OSError as exc:
                raise InstallError("Installer path is missing or unsafe") from exc
            if info.st_uid != 0:
                raise InstallError("Installer must run from a root-owned path")
            if stat.S_ISLNK(info.st_mode):
                continue
            if info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
                raise InstallError("Installer must run from a root-owned path")


def _require_root_owned_launcher() -> None:
    _require_root_owned_path(Path(sys.executable))
    _require_root_owned_path(Path(__file__).resolve())


def _require_root_owned_module_sources(module_dir: Path) -> None:
    _require_root_owned_path(module_dir)
    _require_root_owned_path(module_dir / "requirements.txt")
    source_plugin = module_dir / "passive_secretary_plugin"
    _require_root_owned_path(source_plugin)
    for filename in PLUGIN_FILES:
        _require_root_owned_path(source_plugin / filename)
    for filename in OPERATOR_FILES:
        _require_root_owned_path(module_dir / filename)


def _owner_entry(owner: str):
    try:
        return pwd.getpwnam(owner)
    except KeyError as exc:
        raise InstallError(f"Unknown Linux user: {owner}") from exc


def _require_owner_identity(owner: str):
    entry = _owner_entry(owner)
    if (
        int(entry.pw_uid) <= 0
        or os.geteuid() != int(entry.pw_uid)
        or os.getegid() != int(entry.pw_gid)
        or os.getgroups()
    ):
        raise InstallError("Owner apply identity mismatch")
    return entry


def _reject_symlink_components(path: Path) -> None:
    if not path.is_absolute():
        raise InstallError("Deployment path must be absolute")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise InstallError("Deployment path is missing or unsafe") from exc
        if stat.S_ISLNK(info.st_mode):
            raise InstallError("Deployment path is missing or unsafe")


def _require_owner_node(path: Path, *, uid: int, directory: bool) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise InstallError("Hermes runtime layout is missing or unsafe") from exc
    valid_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (
        not valid_type
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != uid
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise InstallError("Hermes runtime layout is missing or unsafe")


def _require_owner_layout(hermes_home_raw: Path, entry: Any) -> Path:
    linux_home = Path(entry.pw_dir)
    expected = linux_home / ".hermes"
    if (
        not linux_home.is_absolute()
        or not hermes_home_raw.is_absolute()
        or hermes_home_raw != expected
    ):
        raise InstallError("hermes-home must be the owner's .hermes directory")
    _reject_symlink_components(linux_home)
    _reject_symlink_components(expected)
    try:
        if linux_home.resolve(strict=True) != linux_home or expected.resolve(strict=True) != expected:
            raise InstallError("Hermes runtime layout is missing or unsafe")
    except OSError as exc:
        raise InstallError("Hermes runtime layout is missing or unsafe") from exc
    uid = int(entry.pw_uid)
    agent_dir = expected / "hermes-agent"
    config_path = expected / "config.yaml"
    for directory in (linux_home, expected, agent_dir):
        _require_owner_node(directory, uid=uid, directory=True)
    _require_owner_node(config_path, uid=uid, directory=False)
    for optional in (expected / "plugins", expected / "backups"):
        if optional.exists() or optional.is_symlink():
            _require_owner_node(optional, uid=uid, directory=True)
    return expected


def _atomic_write(path: Path, content: str, mode: int, owner: str) -> None:
    if path.is_symlink():
        raise InstallError(f"Refusing to write through symlink: {path}")
    _require_owner_identity(owner)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(temp_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        _fsync_directory(path.parent)
    finally:
        if temp.exists():
            temp.unlink()


def _private_dir(path: Path, owner: str) -> None:
    _require_owner_identity(owner)
    if path.is_symlink():
        raise InstallError(f"Refusing symlinked deployment path: {path}")
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        if current == current.parent:
            raise InstallError("Deployment path is missing or unsafe")
        current = current.parent
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    if not missing:
        _fsync_directory(path)
    for directory in reversed(missing):
        os.chmod(directory, 0o700)
        _fsync_directory(directory)
        _fsync_directory(directory.parent)


def _read_config(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise InstallError("Hermes config.yaml is missing or unsafe")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise InstallError("Hermes config.yaml is invalid") from exc
    if not isinstance(raw, dict):
        raise InstallError("Hermes config.yaml must be a mapping")
    return raw


def _prepare_config(config: dict[str, Any]) -> None:
    plugins = config.setdefault("plugins", {})
    if not isinstance(plugins, dict):
        raise InstallError("config.yaml plugins must be a mapping")
    enabled = plugins.setdefault("enabled", [])
    if not isinstance(enabled, list):
        raise InstallError("config.yaml plugins.enabled must be a list")
    if PLUGIN_NAME not in enabled:
        enabled.append(PLUGIN_NAME)
    disabled = plugins.get("disabled")
    if isinstance(disabled, list):
        plugins["disabled"] = [item for item in disabled if item != PLUGIN_NAME]

    platforms = config.setdefault("platforms", {})
    if not isinstance(platforms, dict):
        raise InstallError("config.yaml platforms must be a mapping")
    telegram = platforms.setdefault("telegram", {})
    if not isinstance(telegram, dict):
        raise InstallError("config.yaml platforms.telegram must be a mapping")
    extra = telegram.setdefault("extra", {})
    if not isinstance(extra, dict):
        raise InstallError("config.yaml platforms.telegram.extra must be a mapping")
    # This is the non-negotiable install posture. Activation is a separate review step.
    extra["business_updates_mode"] = "blocked"
    # Sending on behalf of the Business account is a separate, narrower action
    # edge.  It must remain disabled even when capture is later activated.
    extra["business_reply_enabled"] = False
    # Media download/transcription is a separate resource-using capability.
    # Installation must never enable it implicitly.
    extra["passive_media_enabled"] = False
    # Ordinary group capture is a separate exact-allowlist capability.  It is
    # enabled only after a concrete group has been created and reviewed.
    extra["group_passive_enabled"] = False
    extra["group_passive_chat_ids"] = []
    # The prepared target provider is OpenRouter Whisper Turbo.  Keeping media
    # disabled means installation does not require or transmit the secret.
    extra["passive_media_asr_provider"] = "openrouter"
    # Core refuses passive capture without this owner allowlist. The installer
    # fills the concrete values after this structural helper returns.

    agent = config.setdefault("agent", {})
    if not isinstance(agent, dict):
        raise InstallError("config.yaml agent must be a mapping")
    disabled_toolsets = agent.setdefault("disabled_toolsets", [])
    if not isinstance(disabled_toolsets, list):
        raise InstallError("config.yaml agent.disabled_toolsets must be a list")
    # Loading the plugin is required for receive-only archival, but its search
    # tool would place archive text into the model request and durable Hermes
    # transcript. Keep that separate capability disabled until explicit consent.
    if "passive_secretary" not in disabled_toolsets:
        disabled_toolsets.append("passive_secretary")
    if "passive_secretary_outbound" not in disabled_toolsets:
        disabled_toolsets.append("passive_secretary_outbound")


def _install_dependency(
    module_dir: Path, hermes_home: Path, python_bin: Path, owner: str
) -> None:
    entry = _require_owner_identity(owner)
    user_uv = hermes_home / "bin" / "uv"
    uv_bin = user_uv if user_uv.is_file() and os.access(user_uv, os.X_OK) else None
    if uv_bin is None:
        system_uv_raw = shutil.which("uv")
        if system_uv_raw:
            system_uv = Path(system_uv_raw).resolve()
            try:
                info = system_uv.stat()
            except OSError:
                info = None
            if (
                info is not None
                and info.st_uid == 0
                and not (info.st_mode & 0o022)
                and os.access(system_uv, os.X_OK)
            ):
                uv_bin = system_uv
    if uv_bin is None:
        raise InstallError("uv is required to install the pinned PostgreSQL driver")
    requirements = module_dir / "requirements.txt"
    if not requirements.is_file() or requirements.is_symlink():
        raise InstallError("Pinned dependency manifest is missing or unsafe")
    fd, staged_name = tempfile.mkstemp(prefix="hermes-passive-requirements-", dir="/tmp")
    staged = Path(staged_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(requirements.read_text(encoding="utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        child_env = {
            "HOME": entry.pw_dir,
            "USER": owner,
            "LOGNAME": owner,
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONNOUSERSITE": "1",
        }
        common = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "timeout": 180,
            "check": False,
            "cwd": entry.pw_dir,
            "env": child_env,
            "start_new_session": True,
        }
        result = subprocess.run(
            [
                str(uv_bin),
                "pip",
                "install",
                "--python",
                str(python_bin),
                "--requirement",
                str(staged),
            ],
            **common,
        )
        if result.returncode != 0:
            raise InstallError("uv could not install the PostgreSQL driver")
        verify = subprocess.run(
            [str(python_bin), "-c", "import psycopg"],
            **{**common, "timeout": 30},
        )
        if verify.returncode != 0:
            raise InstallError("PostgreSQL driver verification failed")
    finally:
        try:
            staged.unlink()
        except FileNotFoundError:
            pass


def _validate_exact_plugin_path(plugin_dir: Path, hermes_home: Path) -> None:
    expected = hermes_home / "plugins" / PLUGIN_NAME
    if plugin_dir != expected:
        raise InstallError("Refusing a non-canonical plugin deployment path")
    plugins_root = expected.parent
    if plugins_root.is_symlink():
        raise InstallError("Refusing symlinked Hermes plugins directory")
    if plugin_dir.is_symlink():
        raise InstallError("Refusing symlinked passive secretary plugin path")
    if plugin_dir.exists() and not plugin_dir.is_dir():
        raise InstallError("Passive secretary plugin path is not a directory")


def _backup_current_state(
    *,
    backup_dir: Path,
    config_path: Path,
    plugin_dir: Path,
    owner: str,
) -> bool:
    """Backup config and the complete pre-existing plugin before mutation."""
    plugin_existed = plugin_dir.is_dir()
    _private_dir(backup_dir, owner)
    config_backup = backup_dir / "config.yaml"
    shutil.copy2(config_path, config_backup)
    os.chmod(config_backup, 0o600)
    _fsync_regular_file(config_backup)
    _fsync_directory(backup_dir)
    if plugin_existed:
        plugin_backup = backup_dir / "plugin"
        shutil.copytree(plugin_dir, plugin_backup, symlinks=True)
        _fsync_tree(plugin_backup)
    _atomic_write(
        backup_dir / "state.json",
        json.dumps({"plugin_existed": plugin_existed}, separators=(",", ":")) + "\n",
        0o600,
        owner,
    )
    return plugin_existed


def _remove_exact_plugin_path(
    plugin_dir: Path,
    hermes_home: Path,
    owner: str,
) -> None:
    """Remove only the validated passive-secretary path, never a broad parent."""
    _require_owner_identity(owner)
    expected = hermes_home / "plugins" / PLUGIN_NAME
    if plugin_dir != expected:
        raise InstallError("Refusing to remove a non-canonical plugin path")
    if plugin_dir.is_symlink():
        plugin_dir.unlink()
    elif plugin_dir.is_dir():
        shutil.rmtree(plugin_dir)
    elif plugin_dir.exists():
        plugin_dir.unlink()
    if plugin_dir.parent.is_dir() and not plugin_dir.parent.is_symlink():
        _fsync_directory(plugin_dir.parent)


def _restore_previous_state(
    *,
    backup_dir: Path,
    config_path: Path,
    plugin_dir: Path,
    hermes_home: Path,
    plugin_existed: bool,
    owner: str,
) -> None:
    config_backup = backup_dir / "config.yaml"
    if not config_backup.is_file() or config_backup.is_symlink():
        raise InstallError("Rollback config backup is missing or unsafe")
    _atomic_write(
        config_path,
        config_backup.read_text(encoding="utf-8"),
        0o600,
        owner,
    )
    _remove_exact_plugin_path(plugin_dir, hermes_home, owner)
    if plugin_existed:
        plugin_backup = backup_dir / "plugin"
        if not plugin_backup.is_dir() or plugin_backup.is_symlink():
            raise InstallError("Rollback plugin backup is missing or unsafe")
        shutil.copytree(plugin_backup, plugin_dir, symlinks=True)
        _fsync_tree(plugin_dir)


def _install_plugin_files(
    *,
    source_plugin: Path,
    plugin_dir: Path,
    settings: dict[str, Any],
    owner: str,
) -> None:
    _private_dir(plugin_dir, owner)
    for filename in PLUGIN_FILES:
        source = source_plugin / filename
        _atomic_write(
            plugin_dir / filename,
            source.read_text(encoding="utf-8"),
            0o600,
            owner,
        )
    _atomic_write(
        plugin_dir / "settings.json",
        json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
        0o600,
        owner,
    )


def _commit_config(config_path: Path, config: dict[str, Any], owner: str) -> None:
    _atomic_write(
        config_path,
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
        0o600,
        owner,
    )


def _transactional_mutation(
    *,
    source_plugin: Path,
    hermes_home: Path,
    config_path: Path,
    plugin_dir: Path,
    state_dir: Path,
    backup_dir: Path,
    settings: dict[str, Any],
    config: dict[str, Any],
    owner: str,
) -> None:
    _require_owner_identity(owner)
    plugin_existed = _backup_current_state(
        backup_dir=backup_dir,
        config_path=config_path,
        plugin_dir=plugin_dir,
        owner=owner,
    )
    mutation_started = False
    try:
        mutation_started = True
        _remove_exact_plugin_path(plugin_dir, hermes_home, owner)
        _install_plugin_files(
            source_plugin=source_plugin,
            plugin_dir=plugin_dir,
            settings=settings,
            owner=owner,
        )
        _commit_config(config_path, config, owner)
        # Core owns this directory and its replay files. Never remove it on rollback.
        _private_dir(state_dir, owner)
    except BaseException as exc:
        if mutation_started:
            try:
                _restore_previous_state(
                    backup_dir=backup_dir,
                    config_path=config_path,
                    plugin_dir=plugin_dir,
                    hermes_home=hermes_home,
                    plugin_existed=plugin_existed,
                    owner=owner,
                )
            except Exception as rollback_exc:
                raise InstallError(
                    f"Installation failed and rollback failed; backup={backup_dir}"
                ) from rollback_exc
        if isinstance(exc, InstallError):
            raise
        raise InstallError("Installation failed; previous config/plugin restored") from exc


def _validate_args(args: argparse.Namespace) -> None:
    for label, value in (("client", args.client), ("tenant-id", args.tenant_id), ("source-id", args.source_id)):
        if not ID_RE.fullmatch(value):
            raise InstallError(f"Invalid {label}")
    if args.test_run_id and not ID_RE.fullmatch(args.test_run_id):
        raise InstallError("Invalid test-run-id")
    if not 1 <= args.retention_days <= 3650:
        raise InstallError("retention-days must be between 1 and 3650")
    if not args.owner_user_id or any(
        not value.isdigit() or not 0 < int(value) <= TELEGRAM_ID_MAX
        for value in args.owner_user_id
    ):
        raise InstallError("At least one positive numeric owner-user-id is required")
    if any(not ENV_RE.fullmatch(value) for value in (args.postgres_dsn_env, args.source_ref_key_env)):
        raise InstallError("Secret environment-variable names are invalid")


def _install_as_owner(args: argparse.Namespace) -> dict[str, str]:
    _validate_args(args)
    entry = _require_owner_identity(args.owner)
    module_dir = Path(__file__).resolve().parent
    _require_root_owned_module_sources(module_dir)
    hermes_home = _require_owner_layout(Path(args.hermes_home), entry)
    agent_dir = hermes_home / "hermes-agent"
    python_bin = agent_dir / "venv" / "bin" / "python"
    config_path = hermes_home / "config.yaml"
    if not agent_dir.is_dir() or not python_bin.is_file():
        raise InstallError("Hermes runtime is missing")

    plugin_dir = hermes_home / "plugins" / PLUGIN_NAME
    state_dir = hermes_home / "passive-secretary"
    _validate_exact_plugin_path(plugin_dir, hermes_home)
    source_plugin = module_dir / "passive_secretary_plugin"
    for filename in PLUGIN_FILES:
        source = source_plugin / filename
        if not source.is_file():
            raise InstallError(f"Module source is incomplete: {filename}")

    settings = {
        "tenant_id": args.tenant_id,
        "source_id": args.source_id,
        "test_run_id": args.test_run_id or "",
        "owner_telegram_user_ids": sorted(set(args.owner_user_id)),
        "postgres_dsn_env": args.postgres_dsn_env,
        "source_ref_key_env": args.source_ref_key_env,
        "retention_days": args.retention_days,
        "capture_enabled": False,
        "timezone": "Europe/Moscow",
        "auto_context_enabled": False,
        "outbound_replies_enabled": False,
        "outbound_reply_max_chars": 2000,
        "outbound_intent_ttl_seconds": 300,
        "auto_context_hours": 24,
        "auto_context_max_messages": 40,
        "auto_context_max_chars": 10000,
        "per_message_max_chars": 600,
        "worker_batch_size": 25,
        "worker_poll_seconds": 1.0,
        "retry_max_seconds": 300,
        "query_timeout_seconds": 3,
        "session_authorization_ttl_seconds": 3600,
    }

    config = _read_config(config_path)
    _prepare_config(config)
    config["platforms"]["telegram"]["extra"]["business_owner_ids"] = [
        int(value) for value in sorted(set(args.owner_user_id))
    ]

    # Dependency installation mutates only the venv and must finish before the
    # config/plugin transaction begins.
    if not args.skip_deps:
        _install_dependency(module_dir, hermes_home, python_bin, args.owner)

    backup_dir = hermes_home / "backups" / f"passive-secretary-install-{time.time_ns()}"
    _transactional_mutation(
        source_plugin=source_plugin,
        hermes_home=hermes_home,
        config_path=config_path,
        plugin_dir=plugin_dir,
        state_dir=state_dir,
        backup_dir=backup_dir,
        settings=settings,
        config=config,
        owner=args.owner,
    )
    return {"state": str(state_dir), "backup": str(backup_dir)}


def _stage_module(module_dir: Path, stage: Path) -> Path:
    os.chmod(stage, 0o755)
    runner = stage / "install_passive_secretary.py"
    shutil.copyfile(Path(__file__), runner)
    os.chmod(runner, 0o555)
    requirements = stage / "requirements.txt"
    shutil.copyfile(module_dir / "requirements.txt", requirements)
    os.chmod(requirements, 0o444)
    source = stage / "passive_secretary_plugin"
    source.mkdir(mode=0o755)
    for filename in PLUGIN_FILES:
        destination = source / filename
        shutil.copyfile(module_dir / "passive_secretary_plugin" / filename, destination)
        os.chmod(destination, 0o444)
    os.chmod(source, 0o555)
    for filename in OPERATOR_FILES:
        destination = stage / filename
        shutil.copyfile(module_dir / filename, destination)
        os.chmod(destination, 0o444)
    return runner


def _owner_environment(owner: str, entry: Any) -> dict[str, str]:
    return {
        "HOME": str(entry.pw_dir),
        "USER": owner,
        "LOGNAME": owner,
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONNOUSERSITE": "1",
    }


def _internal_command(args: argparse.Namespace, runner: Path) -> list[str]:
    command = [
        "/usr/bin/python3",
        str(runner),
        "install",
        "--internal-apply",
        "--client",
        args.client,
        "--owner",
        args.owner,
        "--hermes-home",
        args.hermes_home,
        "--tenant-id",
        args.tenant_id,
        "--source-id",
        args.source_id,
        "--test-run-id",
        args.test_run_id or "",
        "--retention-days",
        str(args.retention_days),
        "--postgres-dsn-env",
        args.postgres_dsn_env,
        "--source-ref-key-env",
        args.source_ref_key_env,
    ]
    for owner_user_id in args.owner_user_id:
        command.extend(("--owner-user-id", owner_user_id))
    if args.skip_deps:
        command.append("--skip-deps")
    return command


def _run_owner_install(
    args: argparse.Namespace,
    *,
    runner: Path,
    entry: Any,
) -> dict[str, str]:
    result = subprocess.run(
        _internal_command(args, runner),
        cwd="/",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=300,
        check=False,
        env=_owner_environment(args.owner, entry),
        user=int(entry.pw_uid),
        group=int(entry.pw_gid),
        extra_groups=(),
        umask=0o077,
    )
    if result.returncode != 0:
        raise InstallError("Unprivileged installer apply failed")
    try:
        payload = json.loads(result.stdout)
    except Exception as exc:
        raise InstallError("Unprivileged installer result invalid") from exc
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise InstallError("Unprivileged installer result invalid")
    expected_home = Path(entry.pw_dir) / ".hermes"
    state = Path(str(payload.get("state") or ""))
    backup = Path(str(payload.get("backup") or ""))
    if (
        state != expected_home / "passive-secretary"
        or backup.parent != expected_home / "backups"
        or not backup.name.startswith("passive-secretary-install-")
    ):
        raise InstallError("Unprivileged installer result invalid")
    return {"state": str(state), "backup": str(backup)}


def install(args: argparse.Namespace) -> None:
    if os.geteuid() != 0:
        raise InstallError("Installation must run as root")
    _validate_args(args)
    _require_root_owned_launcher()
    module_dir = Path(__file__).resolve().parent
    _require_root_owned_module_sources(module_dir)
    entry = _owner_entry(args.owner)
    if int(entry.pw_uid) <= 0:
        raise InstallError("Installation owner must be unprivileged")
    _require_owner_layout(Path(args.hermes_home), entry)

    # Root writes only an immutable staging bundle under /tmp. Dependency,
    # backup, plugin/config mutation, rollback, and verification all happen in
    # the child after uid/gid/groups are irreversibly dropped by Popen.
    with tempfile.TemporaryDirectory(prefix="hermes-passive-stage-", dir="/tmp") as raw:
        runner = _stage_module(module_dir, Path(raw))
        result = _run_owner_install(args, runner=runner, entry=entry)

    print(
        f"installed=true\nplugin={PLUGIN_NAME}\nstate={result['state']}\n"
        f"backup={result['backup']}\ncapture_enabled=false\n"
        "business_updates_mode=blocked\nbusiness_reply_enabled=false\n"
        "passive_media_enabled=false\n"
        "passive_media_asr_provider=openrouter\n"
        "outbound_replies_enabled=false\nrestart_required=true"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("install", choices=("install",))
    parser.add_argument("--internal-apply", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--client", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--hermes-home", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--test-run-id", default="")
    parser.add_argument("--owner-user-id", action="append", required=True)
    parser.add_argument("--retention-days", type=int, required=True)
    parser.add_argument(
        "--postgres-dsn-env", default="PASSIVE_SECRETARY_DATABASE_URL"
    )
    parser.add_argument(
        "--source-ref-key-env", default="PASSIVE_SECRETARY_SOURCE_REF_KEY"
    )
    parser.add_argument(
        "--skip-deps",
        action="store_true",
        help="Prepare files without installing psycopg (offline image builds only).",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.internal_apply:
            result = _install_as_owner(args)
            print(json.dumps({"ok": True, **result}))
        else:
            install(args)
    except InstallError as exc:
        print(f"error={exc}", file=__import__("sys").stderr)
        return 1
    except Exception:
        print("error=unexpected_failure", file=__import__("sys").stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
