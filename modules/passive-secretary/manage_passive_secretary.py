#!/usr/bin/env python3
"""Fail-closed operator lifecycle for Hermes Passive Secretary."""

from __future__ import annotations

import argparse
import copy
import fcntl
import io
import importlib
import importlib.util
import json
import os
import pwd
import re
import runpy
import stat
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import yaml

from passive_secretary_plugin.archive import PostgresArchive
from passive_secretary_plugin.settings import Settings, load_settings


PLUGIN_NAME = "passive-secretary"
BLOCKED_MODE = "blocked"
PASSIVE_MODE = "passive"
SAFE_FILE_MODE = 0o600
PURGE_STATEMENT_TIMEOUT_MS = 10_000
PURGE_LOCK_TIMEOUT_MS = 3_000
SERVICE_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,200}\.service$")
RETENTION_BUNDLE_DIR = Path("/opt/hermes-passive-secretary")
OPENROUTER_API_KEY_ENV = "OPENROUTER_API_KEY"
MEDIA_ASR_PROVIDERS = frozenset({"local", "openrouter"})


class LifecycleError(RuntimeError):
    """Public-safe lifecycle error represented only by a stable code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class RuntimePaths:
    owner: str
    uid: int
    gid: int
    hermes_home: Path
    config_path: Path
    plugin_dir: Path
    settings_path: Path
    env_path: Path
    inbox_dir: Path
    backups_dir: Path


@dataclass(frozen=True)
class LifecycleContext:
    paths: RuntimePaths
    config: dict[str, Any]
    settings_raw: dict[str, Any]
    settings: Settings

    @property
    def telegram_extra(self) -> dict[str, Any]:
        return self.config["platforms"]["telegram"]["extra"]

    @property
    def business_mode(self) -> str:
        return str(self.telegram_extra.get("business_updates_mode") or "")


def _owner_entry(owner: str):
    try:
        return pwd.getpwnam(owner)
    except KeyError as exc:
        raise LifecycleError("owner_unknown") from exc


def _reject_symlink_components(path: Path) -> None:
    if not path.is_absolute():
        raise LifecycleError("path_not_absolute")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise LifecycleError("symlink_path_rejected")


def _safe_regular_file(path: Path, code: str) -> None:
    _reject_symlink_components(path)
    if path.is_symlink() or not path.is_file():
        raise LifecycleError(code)


def _resolve_paths_common(
    owner: str,
    hermes_home_raw: str,
    *,
    require_plugin_state: bool,
) -> RuntimePaths:
    entry = _owner_entry(owner)
    if os.geteuid() != int(entry.pw_uid):
        raise LifecycleError("owner_required")
    linux_home = Path(entry.pw_dir).resolve()
    expected = linux_home / ".hermes"
    provided = Path(hermes_home_raw)
    if not provided.is_absolute() or provided != expected:
        raise LifecycleError("noncanonical_hermes_home")
    _reject_symlink_components(provided)
    if not provided.is_dir():
        raise LifecycleError("hermes_home_missing")

    plugin_dir = provided / "plugins" / PLUGIN_NAME
    config_path = provided / "config.yaml"
    settings_path = plugin_dir / "settings.json"
    _safe_regular_file(config_path, "config_missing_or_unsafe")
    if require_plugin_state:
        _reject_symlink_components(plugin_dir)
        if plugin_dir.is_symlink() or not plugin_dir.is_dir():
            raise LifecycleError("plugin_missing_or_unsafe")
        _safe_regular_file(settings_path, "settings_missing_or_unsafe")
    return RuntimePaths(
        owner=owner,
        uid=int(entry.pw_uid),
        gid=int(entry.pw_gid),
        hermes_home=provided,
        config_path=config_path,
        plugin_dir=plugin_dir,
        settings_path=settings_path,
        env_path=provided / ".env",
        inbox_dir=provided / "passive-secretary" / "inbox",
        backups_dir=provided / "backups",
    )


def _resolve_paths(owner: str, hermes_home_raw: str) -> RuntimePaths:
    return _resolve_paths_common(
        owner,
        hermes_home_raw,
        require_plugin_state=True,
    )


def _resolve_deactivation_paths(owner: str, hermes_home_raw: str) -> RuntimePaths:
    """Resolve only the state required to close the Telegram transport gate.

    The emergency path deliberately does not inspect the plugin directory or
    settings file. A missing, symlinked, or corrupt plugin settings file must
    never keep a previously enabled Telegram transport open.
    """

    return _resolve_paths_common(
        owner,
        hermes_home_raw,
        require_plugin_state=False,
    )


def _read_mapping(path: Path, *, json_file: bool, code: str) -> dict[str, Any]:
    _safe_regular_file(path, code)
    try:
        text = path.read_text(encoding="utf-8")
        raw = json.loads(text) if json_file else (yaml.safe_load(text) or {})
    except Exception as exc:
        raise LifecycleError(code) from exc
    if not isinstance(raw, dict):
        raise LifecycleError(code)
    return raw


def _telegram_extra(config: dict[str, Any]) -> dict[str, Any]:
    platforms = config.get("platforms")
    if not isinstance(platforms, dict):
        raise LifecycleError("config_structure_invalid")
    telegram = platforms.get("telegram")
    if not isinstance(telegram, dict):
        raise LifecycleError("config_structure_invalid")
    extra = telegram.get("extra")
    if not isinstance(extra, dict):
        raise LifecycleError("config_structure_invalid")
    return extra


def _config_owner_ids(extra: dict[str, Any]) -> tuple[str, ...] | None:
    raw = extra.get("business_owner_ids")
    if not isinstance(raw, list) or not raw:
        return None
    normalized: list[str] = []
    for value in raw:
        if isinstance(value, bool):
            return None
        text = str(value)
        if not text.isdigit() or not 0 < int(text) <= 2**63 - 1:
            return None
        normalized.append(text)
    if len(set(normalized)) != len(normalized):
        return None
    return tuple(sorted(normalized, key=int))


def _plugin_enabled(config: dict[str, Any]) -> bool:
    plugins = config.get("plugins")
    if not isinstance(plugins, dict):
        return False
    enabled = plugins.get("enabled")
    disabled = plugins.get("disabled")
    return bool(
        isinstance(enabled, list)
        and PLUGIN_NAME in enabled
        and not (isinstance(disabled, list) and PLUGIN_NAME in disabled)
    )


def _search_tool_enabled(config: dict[str, Any]) -> bool:
    agent = config.get("agent")
    if not isinstance(agent, dict):
        return True
    disabled = agent.get("disabled_toolsets")
    if not isinstance(disabled, list):
        return True
    return "passive_secretary" not in disabled


def _set_search_tool_enabled(config: dict[str, Any], *, enabled: bool) -> None:
    agent = config.setdefault("agent", {})
    if not isinstance(agent, dict):
        raise LifecycleError("config_structure_invalid")
    disabled = agent.setdefault("disabled_toolsets", [])
    if not isinstance(disabled, list):
        raise LifecycleError("config_structure_invalid")
    if enabled:
        agent["disabled_toolsets"] = [
            item for item in disabled if item != "passive_secretary"
        ]
    elif "passive_secretary" not in disabled:
        disabled.append("passive_secretary")


def _outbound_tool_enabled(config: dict[str, Any]) -> bool:
    agent = config.get("agent")
    if not isinstance(agent, dict):
        return True
    disabled = agent.get("disabled_toolsets")
    if not isinstance(disabled, list):
        return True
    return "passive_secretary_outbound" not in disabled


def _set_outbound_tool_enabled(config: dict[str, Any], *, enabled: bool) -> None:
    agent = config.setdefault("agent", {})
    if not isinstance(agent, dict):
        raise LifecycleError("config_structure_invalid")
    disabled = agent.setdefault("disabled_toolsets", [])
    if not isinstance(disabled, list):
        raise LifecycleError("config_structure_invalid")
    if enabled:
        agent["disabled_toolsets"] = [
            item for item in disabled if item != "passive_secretary_outbound"
        ]
    elif "passive_secretary_outbound" not in disabled:
        disabled.append("passive_secretary_outbound")


def _business_reply_enabled(config: dict[str, Any]) -> bool:
    return _telegram_extra(config).get("business_reply_enabled") is True


def _business_reply_disabled(config: dict[str, Any]) -> bool:
    return _telegram_extra(config).get("business_reply_enabled") is False


def _passive_media_enabled(config: dict[str, Any]) -> bool:
    return _telegram_extra(config).get("passive_media_enabled") is True


def _passive_media_disabled(config: dict[str, Any]) -> bool:
    return _telegram_extra(config).get("passive_media_enabled") is False


def _passive_media_asr_provider(config: dict[str, Any]) -> str:
    raw = _telegram_extra(config).get("passive_media_asr_provider", "local")
    if not isinstance(raw, str) or raw not in MEDIA_ASR_PROVIDERS:
        raise LifecycleError("media_asr_provider_invalid")
    return raw


def _openrouter_key_valid(value: str) -> bool:
    return bool(
        20 <= len(value) <= 512
        and not any(character.isspace() or ord(character) < 32 for character in value)
    )


def _load_context(paths: RuntimePaths) -> LifecycleContext:
    config = _read_mapping(
        paths.config_path, json_file=False, code="config_invalid"
    )
    _telegram_extra(config)
    settings_raw = _read_mapping(
        paths.settings_path, json_file=True, code="settings_invalid"
    )
    try:
        settings = load_settings(paths.settings_path)
    except Exception as exc:
        raise LifecycleError("settings_invalid") from exc
    return LifecycleContext(
        paths=paths,
        config=config,
        settings_raw=settings_raw,
        settings=settings,
    )


@contextmanager
def _lifecycle_lock(paths: RuntimePaths, *, exclusive: bool) -> Iterator[None]:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(paths.hermes_home, flags)
    except OSError as exc:
        raise LifecycleError("lifecycle_lock_failed") from exc
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _secret_file_is_safe(paths: RuntimePaths) -> bool:
    try:
        _read_private_env_text(paths)
    except LifecycleError:
        return False
    return True


def _read_private_env_text(paths: RuntimePaths) -> str:
    path = paths.env_path
    _reject_symlink_components(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise LifecycleError("secret_file_unsafe") from exc
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != SAFE_FILE_MODE
            or info.st_uid != paths.uid
        ):
            raise LifecycleError("secret_file_unsafe")
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            return handle.read()
    except LifecycleError:
        raise
    except Exception as exc:
        raise LifecycleError("secret_file_unsafe") from exc
    finally:
        if fd >= 0:
            os.close(fd)


def _parse_private_env(paths: RuntimePaths) -> dict[str, str]:
    try:
        from dotenv import dotenv_values
    except Exception as exc:
        raise LifecycleError("dotenv_unavailable") from exc
    try:
        parsed = dotenv_values(
            stream=io.StringIO(_read_private_env_text(paths)),
            interpolate=True,
        )
    except Exception as exc:
        raise LifecycleError("secret_file_unsafe") from exc
    # Hermes uses pinned python-dotenv with override=True for this same file.
    # dotenv_values shares its quoting, inline-comment, escape, interpolation,
    # and last-assignment semantics without mutating the operator environment.
    return {
        str(key): "" if value is None else str(value)
        for key, value in parsed.items()
        if key
    }


def _effective_secrets(ctx: LifecycleContext) -> dict[str, str]:
    file_values = _parse_private_env(ctx.paths)
    result: dict[str, str] = {}
    for name in (ctx.settings.postgres_dsn_env, ctx.settings.source_ref_key_env):
        value = file_values.get(name, "")
        if not value.strip():
            raise LifecycleError("required_secret_missing")
        process_value = os.environ.get(name, "")
        if process_value and process_value != value:
            raise LifecycleError("secret_source_mismatch")
        result[name] = value
    if len(result[ctx.settings.source_ref_key_env]) < 32:
        raise LifecycleError("source_ref_key_invalid")
    return result


@contextmanager
def _temporary_secret_env(values: dict[str, str]) -> Iterator[None]:
    missing = object()
    previous: dict[str, object | str] = {
        key: os.environ.get(key, missing) for key in values
    }
    try:
        for key, value in values.items():
            os.environ[key] = value
        yield
    finally:
        for key, value in previous.items():
            if value is missing:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)


def _require_psycopg() -> None:
    try:
        importlib.import_module("psycopg")
    except Exception as exc:
        raise LifecycleError("psycopg_unavailable") from exc


def _database_health(settings: Settings, secrets: dict[str, str]) -> None:
    _require_psycopg()
    try:
        with _temporary_secret_env(secrets):
            PostgresArchive(settings).ensure_schema()
    except LifecycleError:
        raise
    except Exception as exc:
        raise LifecycleError("database_preflight_failed") from exc


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write_bytes(path: Path, content: bytes, paths: RuntimePaths) -> None:
    _reject_symlink_components(path.parent)
    if path.is_symlink():
        raise LifecycleError("atomic_target_unsafe")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, SAFE_FILE_MODE)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if os.geteuid() == 0:
            os.chown(temporary, paths.uid, paths.gid)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_settings(ctx: LifecycleContext, raw: dict[str, Any]) -> None:
    _atomic_write_bytes(
        ctx.paths.settings_path,
        (json.dumps(raw, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        ctx.paths,
    )


def _write_settings_for_paths(paths: RuntimePaths, raw: dict[str, Any]) -> None:
    _atomic_write_bytes(
        paths.settings_path,
        (json.dumps(raw, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        paths,
    )


def _write_config(ctx: LifecycleContext, raw: dict[str, Any]) -> None:
    _atomic_write_bytes(
        ctx.paths.config_path,
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False).encode("utf-8"),
        ctx.paths,
    )


def _write_config_for_paths(paths: RuntimePaths, raw: dict[str, Any]) -> None:
    _atomic_write_bytes(
        paths.config_path,
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False).encode("utf-8"),
        paths,
    )


def _make_backup(ctx: LifecycleContext) -> tuple[Path, bytes, bytes]:
    original_config = ctx.paths.config_path.read_bytes()
    original_settings = ctx.paths.settings_path.read_bytes()
    backups = ctx.paths.backups_dir
    _reject_symlink_components(backups)
    if backups.exists() and not backups.is_dir():
        raise LifecycleError("backup_root_unsafe")
    backups.mkdir(parents=True, exist_ok=True)
    if backups.is_symlink():
        raise LifecycleError("backup_root_unsafe")
    backup = backups / f"passive-secretary-lifecycle-{time.time_ns()}"
    backup.mkdir(mode=0o700)
    os.chmod(backup, 0o700)
    if os.geteuid() == 0:
        os.chown(backup, ctx.paths.uid, ctx.paths.gid)
    _atomic_write_bytes(backup / "config.yaml", original_config, ctx.paths)
    _atomic_write_bytes(backup / "settings.json", original_settings, ctx.paths)
    _fsync_directory(backups)
    return backup, original_config, original_settings


def _restore_activation(
    ctx: LifecycleContext, original_config: bytes, original_settings: bytes
) -> None:
    # Restore the transport gate first so a partially restored state stays blocked.
    _atomic_write_bytes(ctx.paths.config_path, original_config, ctx.paths)
    _atomic_write_bytes(ctx.paths.settings_path, original_settings, ctx.paths)


def _scope_matches(ctx: LifecycleContext) -> bool:
    expected = tuple(sorted(ctx.settings.owner_telegram_user_ids, key=int))
    return _config_owner_ids(ctx.telegram_extra) == expected


def _validate_activation_scope(ctx: LifecycleContext, args: argparse.Namespace) -> None:
    if ctx.business_mode not in {BLOCKED_MODE, PASSIVE_MODE}:
        raise LifecycleError("business_mode_unsafe")
    if not _plugin_enabled(ctx.config):
        raise LifecycleError("plugin_not_enabled")
    if not _scope_matches(ctx):
        raise LifecycleError("owner_scope_mismatch")
    requested_model_access = bool(
        getattr(args, "enable_search_tool", False)
        or getattr(args, "enable_auto_context", False)
        or getattr(args, "enable_outbound_replies", False)
    )
    if requested_model_access and not getattr(args, "confirm_transcript_policy", False):
        raise LifecycleError("transcript_policy_confirmation_required")
    requested_outbound = bool(getattr(args, "enable_outbound_replies", False))
    confirmed_outbound = bool(getattr(args, "confirm_outbound_policy", False))
    if requested_outbound and not confirmed_outbound:
        raise LifecycleError("outbound_policy_confirmation_required")
    if confirmed_outbound and not requested_outbound:
        raise LifecycleError("outbound_policy_confirmation_without_enable")
    _spool_count, _spool_bytes, _spool_oldest_age, spool_safe = _spool_stats(ctx)
    if not spool_safe:
        raise LifecycleError("spool_path_unsafe")
    configured_test = ctx.settings.test_run_id
    if args.production:
        if configured_test:
            raise LifecycleError("production_requires_empty_test_run")
        if not args.confirm_retention_and_scope:
            raise LifecycleError("production_confirmation_required")
    else:
        if args.confirm_retention_and_scope:
            raise LifecycleError("production_confirmation_without_production")
        if not configured_test or args.test_run_id != configured_test:
            raise LifecycleError("test_run_scope_mismatch")


def _configured_inbox_dir(ctx: LifecycleContext) -> Path:
    raw = ctx.telegram_extra.get("passive_spool_dir", "")
    if raw is None:
        raw = ""
    if not isinstance(raw, str):
        raise LifecycleError("spool_path_unsafe")
    configured = raw.strip()
    candidate = (
        Path(configured)
        if configured
        else Path("passive-secretary") / "inbox"
    )
    if not candidate.is_absolute():
        candidate = ctx.paths.hermes_home / candidate
    _reject_symlink_components(candidate)
    resolved = candidate.resolve()
    try:
        resolved.relative_to(ctx.paths.hermes_home.resolve())
    except ValueError as exc:
        raise LifecycleError("spool_path_unsafe") from exc
    return resolved


def _spool_stats(ctx: LifecycleContext) -> tuple[int, int, int, bool]:
    try:
        inbox = _configured_inbox_dir(ctx)
    except LifecycleError:
        return 0, 0, 0, False
    if not inbox.exists():
        return 0, 0, 0, True
    try:
        _reject_symlink_components(inbox)
        if inbox.is_symlink() or not inbox.is_dir():
            return 0, 0, 0, False
        count = 0
        size = 0
        oldest_mtime_ns: int | None = None
        stack = [inbox]
        inspected = 0
        while stack:
            directory = stack.pop()
            directory_info = directory.stat(follow_symlinks=False)
            if (
                not stat.S_ISDIR(directory_info.st_mode)
                or directory_info.st_uid != ctx.paths.uid
                or stat.S_IMODE(directory_info.st_mode) & 0o022
            ):
                return count, size, 0, False
            for item in directory.iterdir():
                inspected += 1
                if inspected > 100_000:
                    return count, size, 0, False
                info = item.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    return count, size, 0, False
                if stat.S_ISDIR(info.st_mode):
                    stack.append(item)
                    continue
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != ctx.paths.uid
                    or stat.S_IMODE(info.st_mode) & 0o077
                ):
                    return count, size, 0, False
                count += 1
                size += max(0, int(info.st_size))
                oldest_mtime_ns = (
                    int(info.st_mtime_ns)
                    if oldest_mtime_ns is None
                    else min(oldest_mtime_ns, int(info.st_mtime_ns))
                )
        oldest_age_seconds = (
            0
            if oldest_mtime_ns is None
            else max(0, (time.time_ns() - oldest_mtime_ns) // 1_000_000_000)
        )
        return count, size, int(oldest_age_seconds), True
    except (LifecycleError, OSError):
        return 0, 0, 0, False


def _status_values(ctx: LifecycleContext) -> dict[str, bool | int | str]:
    mode = ctx.business_mode
    env_values: dict[str, str] = {}
    secret_file_safe = _secret_file_is_safe(ctx.paths)
    if secret_file_safe:
        try:
            env_values = _parse_private_env(ctx.paths)
        except LifecycleError:
            secret_file_safe = False
    dsn = env_values.get(ctx.settings.postgres_dsn_env, "")
    ref_key = env_values.get(ctx.settings.source_ref_key_env, "")
    secret_sources_match = secret_file_safe and all(
        not os.environ.get(name, "")
        or os.environ.get(name, "") == env_values.get(name, "")
        for name in (ctx.settings.postgres_dsn_env, ctx.settings.source_ref_key_env)
    )
    spool_count, spool_bytes, spool_oldest_age, spool_safe = _spool_stats(ctx)
    config_owners = _config_owner_ids(ctx.telegram_extra)
    business_reply_flag_valid = isinstance(
        ctx.telegram_extra.get("business_reply_enabled"), bool
    )
    business_reply_enabled = _business_reply_enabled(ctx.config)
    passive_media_flag_valid = isinstance(
        ctx.telegram_extra.get("passive_media_enabled"), bool
    )
    passive_media_enabled = _passive_media_enabled(ctx.config)
    group_passive_flag_valid = isinstance(
        ctx.telegram_extra.get("group_passive_enabled", False), bool
    )
    group_passive_enabled = ctx.telegram_extra.get("group_passive_enabled") is True
    raw_group_ids = ctx.telegram_extra.get("group_passive_chat_ids", [])
    group_passive_scope_valid = (
        isinstance(raw_group_ids, list)
        and all(
            isinstance(item, int) and not isinstance(item, bool) and item < 0
            for item in raw_group_ids
        )
        and len(set(raw_group_ids)) == len(raw_group_ids)
    )
    try:
        media_asr_provider = _passive_media_asr_provider(ctx.config)
        media_asr_provider_valid = True
    except LifecycleError:
        media_asr_provider = "invalid"
        media_asr_provider_valid = False
    openrouter_key = env_values.get(OPENROUTER_API_KEY_ENV, "")
    openrouter_key_valid = _openrouter_key_valid(openrouter_key)
    outbound_replies_enabled = ctx.settings.outbound_replies_enabled
    outbound_tool_enabled = _outbound_tool_enabled(ctx.config)
    outbound_state_agrees = business_reply_flag_valid and (
        business_reply_enabled
        == outbound_replies_enabled
        == outbound_tool_enabled
    )
    return {
        "business_reply_enabled": business_reply_enabled,
        "business_reply_flag_valid": business_reply_flag_valid,
        "business_updates_blocked": mode == BLOCKED_MODE,
        "business_updates_passive": mode == PASSIVE_MODE,
        "business_mode_supported": mode in {BLOCKED_MODE, PASSIVE_MODE},
        "auto_context_enabled": ctx.settings.auto_context_enabled,
        "capture_enabled": ctx.settings.capture_enabled,
        "config_owner_count": len(config_owners or ()),
        "configured_active": (
            mode == PASSIVE_MODE
            and ctx.settings.capture_enabled
            and outbound_state_agrees
            and passive_media_flag_valid
            and group_passive_flag_valid
            and group_passive_scope_valid
            and media_asr_provider_valid
            and (
                not passive_media_enabled
                or media_asr_provider != "openrouter"
                or openrouter_key_valid
            )
        ),
        "configured_disabled": (
            mode == BLOCKED_MODE
            and not ctx.settings.capture_enabled
            and not ctx.settings.auto_context_enabled
            and not _search_tool_enabled(ctx.config)
            and not outbound_replies_enabled
            and _business_reply_disabled(ctx.config)
            and not outbound_tool_enabled
            and _passive_media_disabled(ctx.config)
            and not group_passive_enabled
            and group_passive_flag_valid
            and group_passive_scope_valid
            and media_asr_provider_valid
        ),
        "owner_scope_matches": _scope_matches(ctx),
        "outbound_replies_enabled": outbound_replies_enabled,
        "outbound_state_agrees": outbound_state_agrees,
        "outbound_tool_enabled": outbound_tool_enabled,
        "passive_media_enabled": passive_media_enabled,
        "passive_media_flag_valid": passive_media_flag_valid,
        "group_passive_enabled": group_passive_enabled,
        "group_passive_flag_valid": group_passive_flag_valid,
        "group_passive_scope_valid": group_passive_scope_valid,
        "passive_media_asr_provider": media_asr_provider,
        "passive_media_asr_provider_valid": media_asr_provider_valid,
        "openrouter_key_valid": openrouter_key_valid,
        "plugin_enabled": _plugin_enabled(ctx.config),
        "postgres_env_present": bool(dsn.strip()),
        "psycopg_available": importlib.util.find_spec("psycopg") is not None,
        "secret_file_mode_safe": secret_file_safe,
        "secret_sources_match": secret_sources_match,
        "search_tool_enabled": _search_tool_enabled(ctx.config),
        "source_ref_key_valid": len(ref_key) >= 32,
        "spool_bytes": spool_bytes,
        "spool_oldest_age_seconds": spool_oldest_age,
        "spool_pending_count": spool_count,
        "spool_path_safe": spool_safe,
        "test_scope_nonempty": bool(ctx.settings.test_run_id),
        "owner_count": len(ctx.settings.owner_telegram_user_ids),
    }


def status(args: argparse.Namespace) -> dict[str, bool | int | str]:
    paths = _resolve_paths(args.owner, args.hermes_home)
    with _lifecycle_lock(paths, exclusive=False):
        return _status_values(_load_context(paths))


def _systemd_properties(
    unit: str,
    properties: tuple[str, ...],
    *,
    code: str,
) -> dict[str, str]:
    try:
        result = subprocess.run(
            ["systemctl", "show", unit]
            + [f"--property={name}" for name in properties]
            + ["--no-pager"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            check=False,
            env={
                "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                "LANG": "C",
                "LC_ALL": "C",
                "SYSTEMD_COLORS": "0",
                "SYSTEMD_PAGER": "cat",
            },
        )
    except Exception as exc:
        raise LifecycleError(code) from exc
    if result.returncode != 0 or len(result.stdout) > 65_536:
        raise LifecycleError(code)
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in properties and key not in values:
            values[key] = value
    if set(values) != set(properties):
        raise LifecycleError(code)
    return values


def _require_retention_timer_ready(paths: RuntimePaths) -> None:
    stem = f"{paths.owner}-hermes-passive-secretary-retention"
    service = f"{stem}.service"
    timer = f"{stem}.timer"
    service_path = Path("/etc/systemd/system") / service
    timer_path = Path("/etc/systemd/system") / timer
    python_bin = paths.hermes_home / "hermes-agent" / "venv" / "bin" / "python"
    expected_exec = (
        f"{{ path={python_bin} ; argv[]={python_bin} -B -E -s "
        f"{RETENTION_BUNDLE_DIR / 'run_retention.py'} --settings "
        f"{paths.settings_path} ; "
    )
    service_values = _systemd_properties(
        service,
        (
            "LoadState",
            "ActiveState",
            "Result",
            "ExecMainStatus",
            "Type",
            "User",
            "Group",
            "ExecStart",
            "WorkingDirectory",
            "EnvironmentFiles",
            "FragmentPath",
            "DropInPaths",
        ),
        code="retention_service_not_ready",
    )
    if (
        service_values["LoadState"] != "loaded"
        or service_values["ActiveState"] != "inactive"
        or service_values["Result"] != "success"
        or service_values["ExecMainStatus"] != "0"
        or service_values["Type"] != "oneshot"
        or service_values["User"] != paths.owner
        or service_values["Group"] != str(paths.gid)
        or not service_values["ExecStart"].startswith(expected_exec)
        or service_values["WorkingDirectory"] != str(paths.hermes_home)
        or service_values["EnvironmentFiles"]
        != f"{paths.env_path} (ignore_errors=no)"
        or service_values["FragmentPath"] != str(service_path)
        or service_values["DropInPaths"]
    ):
        raise LifecycleError("retention_service_not_ready")
    _require_trusted_unit_fragment(service_values["FragmentPath"], service)

    timer_values = _systemd_properties(
        timer,
        (
            "LoadState",
            "ActiveState",
            "SubState",
            "UnitFileState",
            "FragmentPath",
            "DropInPaths",
        ),
        code="retention_timer_not_ready",
    )
    if timer_values != {
        "LoadState": "loaded",
        "ActiveState": "active",
        "SubState": "waiting",
        "UnitFileState": "enabled",
        "FragmentPath": str(timer_path),
        "DropInPaths": "",
    }:
        raise LifecycleError("retention_timer_not_ready")
    _require_trusted_unit_fragment(timer_values["FragmentPath"], timer)


def activate(args: argparse.Namespace) -> dict[str, Any]:
    paths = _resolve_paths(args.owner, args.hermes_home)
    with _lifecycle_lock(paths, exclusive=True):
        ctx = _load_context(paths)
        _validate_activation_scope(ctx, args)
        if args.production:
            _require_retention_timer_ready(paths)
        requested_media = bool(getattr(args, "enable_media_processing", False))
        requested_provider = getattr(args, "media_asr_provider", None)
        if requested_provider is None:
            requested_provider = _passive_media_asr_provider(ctx.config)
        elif requested_provider not in MEDIA_ASR_PROVIDERS:
            raise LifecycleError("media_asr_provider_invalid")
        if requested_provider and not requested_media and getattr(
            args, "media_asr_provider", None
        ) is not None:
            raise LifecycleError("media_provider_without_media")

        secrets = _effective_secrets(ctx)
        if requested_media and requested_provider == "openrouter":
            env_values = _parse_private_env(paths)
            openrouter_key = env_values.get(OPENROUTER_API_KEY_ENV, "")
            if not _openrouter_key_valid(openrouter_key):
                raise LifecycleError("openrouter_key_invalid")
            process_key = os.environ.get(OPENROUTER_API_KEY_ENV, "")
            if process_key and process_key != openrouter_key:
                raise LifecycleError("secret_source_mismatch")
        # DDL health and real connectivity are deliberately completed before
        # either desired-state file can change.
        _database_health(ctx.settings, secrets)

        requested_auto_context = bool(getattr(args, "enable_auto_context", False))
        requested_search_tool = bool(getattr(args, "enable_search_tool", False))
        requested_outbound = bool(getattr(args, "enable_outbound_replies", False))
        requested_group_passive = bool(
            getattr(args, "enable_group_passive", False)
        )
        already_active = (
            ctx.business_mode == PASSIVE_MODE
            and ctx.settings.capture_enabled
            and ctx.settings.auto_context_enabled == requested_auto_context
            and _search_tool_enabled(ctx.config) == requested_search_tool
            and ctx.settings.outbound_replies_enabled == requested_outbound
            and _business_reply_enabled(ctx.config) == requested_outbound
            and _outbound_tool_enabled(ctx.config) == requested_outbound
            and _passive_media_enabled(ctx.config) == requested_media
            and isinstance(ctx.telegram_extra.get("passive_media_enabled"), bool)
            and _passive_media_asr_provider(ctx.config) == requested_provider
            and ctx.telegram_extra.get("group_passive_enabled")
            is requested_group_passive
        )
        if already_active:
            return {"changed": False, "restart_required": True}

        new_settings = copy.deepcopy(ctx.settings_raw)
        new_settings["capture_enabled"] = True
        new_settings["auto_context_enabled"] = requested_auto_context
        new_settings["outbound_replies_enabled"] = requested_outbound
        new_config = copy.deepcopy(ctx.config)
        new_extra = _telegram_extra(new_config)
        new_extra["business_updates_mode"] = PASSIVE_MODE
        new_extra["business_reply_enabled"] = requested_outbound
        new_extra["passive_media_enabled"] = requested_media
        new_extra["passive_media_asr_provider"] = requested_provider
        new_extra["group_passive_enabled"] = requested_group_passive
        _set_search_tool_enabled(new_config, enabled=requested_search_tool)
        _set_outbound_tool_enabled(new_config, enabled=requested_outbound)
        backup, original_config, original_settings = _make_backup(ctx)
        try:
            # Safe activation order: plugin can accept/commit before core emits.
            _write_settings(ctx, new_settings)
            _write_config(ctx, new_config)
        except BaseException as exc:
            try:
                _restore_activation(ctx, original_config, original_settings)
            except Exception as rollback_exc:
                raise LifecycleError("activation_rollback_failed") from rollback_exc
            if isinstance(exc, LifecycleError):
                raise
            raise LifecycleError("activation_failed_rolled_back") from exc
        return {
            "backup": str(backup),
            "changed": True,
            "restart_required": True,
        }


def deactivate(args: argparse.Namespace) -> dict[str, Any]:
    paths = _resolve_deactivation_paths(args.owner, args.hermes_home)
    with _lifecycle_lock(paths, exclusive=True):
        # Read and atomically close the core transport gate before touching any
        # plugin state. This is the emergency boundary: corrupt/missing settings
        # cannot keep Telegram Business updates flowing into the plugin.
        config = _read_mapping(
            paths.config_path,
            json_file=False,
            code="config_invalid",
        )
        extra = _telegram_extra(config)
        transport_changed = (
            str(extra.get("business_updates_mode") or "") != BLOCKED_MODE
            or extra.get("group_passive_enabled") is True
        )
        if transport_changed:
            new_config = copy.deepcopy(config)
            _telegram_extra(new_config)["business_updates_mode"] = BLOCKED_MODE
            _telegram_extra(new_config)["group_passive_enabled"] = False
            try:
                _write_config_for_paths(paths, new_config)
            except BaseException as exc:
                if isinstance(exc, LifecycleError):
                    raise
                raise LifecycleError("deactivation_block_write_failed") from exc
            config = new_config

        search_tool_disabled = not _search_tool_enabled(config)
        outbound_tool_disabled = not _outbound_tool_enabled(config)
        business_reply_disabled = _business_reply_disabled(config)
        media_processing_disabled = _passive_media_disabled(config)
        action_config_changed = False
        if not (
            search_tool_disabled
            and outbound_tool_disabled
            and business_reply_disabled
            and media_processing_disabled
        ):
            try:
                new_config = copy.deepcopy(config)
                _set_search_tool_enabled(new_config, enabled=False)
                _set_outbound_tool_enabled(new_config, enabled=False)
                _telegram_extra(new_config)["business_reply_enabled"] = False
                _telegram_extra(new_config)["passive_media_enabled"] = False
                _telegram_extra(new_config)["group_passive_enabled"] = False
                _write_config_for_paths(paths, new_config)
                config = new_config
                search_tool_disabled = True
                outbound_tool_disabled = True
                business_reply_disabled = True
                media_processing_disabled = True
                action_config_changed = True
            except Exception:
                search_tool_disabled = not _search_tool_enabled(config)
                outbound_tool_disabled = not _outbound_tool_enabled(config)
                business_reply_disabled = _business_reply_disabled(config)
                media_processing_disabled = _passive_media_disabled(config)

        # Disabling plugin capture is defense in depth only. Do it after the
        # transport is durably blocked and report its outcome without reopening
        # the gate or failing the emergency command.
        settings_disabled = False
        settings_changed = False
        try:
            settings_raw = _read_mapping(
                paths.settings_path,
                json_file=True,
                code="settings_invalid",
            )
            settings_disabled = (
                settings_raw.get("capture_enabled") is False
                and settings_raw.get("auto_context_enabled") is False
                and settings_raw.get("outbound_replies_enabled") is False
            )
            if not settings_disabled:
                new_settings = copy.deepcopy(settings_raw)
                new_settings["capture_enabled"] = False
                new_settings["auto_context_enabled"] = False
                new_settings["outbound_replies_enabled"] = False
                _write_settings_for_paths(paths, new_settings)
                settings_disabled = True
                settings_changed = True
        except Exception:
            settings_disabled = False

        return {
            "business_reply_disabled": business_reply_disabled,
            "changed": transport_changed or action_config_changed or settings_changed,
            "deactivation_complete": (
                settings_disabled
                and search_tool_disabled
                and outbound_tool_disabled
                and business_reply_disabled
                and media_processing_disabled
            ),
            "media_processing_disabled": media_processing_disabled,
            "outbound_tool_disabled": outbound_tool_disabled,
            "restart_required": True,
            "settings_disabled": settings_disabled,
            "search_tool_disabled": search_tool_disabled,
            "transport_blocked": True,
        }


def _fetch_count(cursor: Any) -> int:
    row = cursor.fetchone()
    if not isinstance(row, (tuple, list)) or not row:
        raise LifecycleError("purge_count_invalid")
    return max(0, int(row[0]))


def _purge_test_database(
    settings: Settings, secrets: dict[str, str]
) -> dict[str, int]:
    if not settings.test_run_id:
        raise LifecycleError("production_scope_purge_refused")
    _require_psycopg()
    scope = (
        settings.tenant_id,
        [int(value) for value in settings.owner_telegram_user_ids],
        settings.source_id,
        settings.test_run_id,
    )
    conn = None
    cursor = None
    try:
        with _temporary_secret_env(secrets):
            archive = PostgresArchive(settings)
            archive.ensure_schema()
            conn = archive._connect()
            cursor = conn.cursor()
            cursor.execute(
                f"SET LOCAL statement_timeout = '{PURGE_STATEMENT_TIMEOUT_MS}ms'"
            )
            cursor.execute(f"SET LOCAL lock_timeout = '{PURGE_LOCK_TIMEOUT_MS}ms'")
            # Freeze every table participating in the scoped invariant before
            # either the pre-count or a delete. SHARE ROW EXCLUSIVE conflicts
            # with concurrent INSERT/UPDATE/DELETE while still permitting
            # unrelated readers, so a writer cannot refill the scope between
            # the final emptiness checks and commit.
            cursor.execute(
                """
                LOCK TABLE passive_secretary.archive_events,
                           passive_secretary.business_connections,
                           passive_secretary.media_enrichments,
                           passive_secretary.message_versions,
                           passive_secretary.messages,
                           passive_secretary.outbound_intents
                IN SHARE ROW EXCLUSIVE MODE
                """
            )
            cursor.execute(
                """
                SELECT COUNT(*) FROM passive_secretary.message_versions
                WHERE tenant_id=%s AND tenant_owner_id = ANY(%s)
                  AND source_id=%s AND test_run_id=%s
                """,
                scope,
            )
            versions = _fetch_count(cursor)
            cursor.execute(
                """
                SELECT COUNT(*) FROM passive_secretary.media_enrichments
                WHERE tenant_id=%s AND tenant_owner_id = ANY(%s)
                  AND source_id=%s AND test_run_id=%s
                """,
                scope,
            )
            media_enrichments = _fetch_count(cursor)
            cursor.execute(
                """
                SELECT COUNT(*) FROM passive_secretary.outbound_intents
                WHERE tenant_id=%s AND tenant_owner_id = ANY(%s)
                  AND source_id=%s AND test_run_id=%s
                """,
                scope,
            )
            outbound_intents = _fetch_count(cursor)

            deleted: dict[str, int] = {
                "message_versions_deleted": versions,
                "media_enrichments_deleted": media_enrichments,
                "outbound_intents_deleted": outbound_intents,
            }
            for key, sql in (
                (
                    "archive_events_deleted",
                    """
                    DELETE FROM passive_secretary.archive_events
                    WHERE tenant_id=%s AND tenant_owner_id = ANY(%s)
                      AND source_id=%s AND test_run_id=%s
                    """,
                ),
                (
                    "connections_deleted",
                    """
                    DELETE FROM passive_secretary.business_connections
                    WHERE tenant_id=%s AND tenant_owner_id = ANY(%s)
                      AND source_id=%s AND test_run_id=%s
                    """,
                ),
                (
                    "media_enrichments_deleted",
                    """
                    DELETE FROM passive_secretary.media_enrichments
                    WHERE tenant_id=%s AND tenant_owner_id = ANY(%s)
                      AND source_id=%s AND test_run_id=%s
                    """,
                ),
                (
                    "outbound_intents_deleted",
                    """
                    DELETE FROM passive_secretary.outbound_intents
                    WHERE tenant_id=%s AND tenant_owner_id = ANY(%s)
                      AND source_id=%s AND test_run_id=%s
                    """,
                ),
                (
                    "messages_deleted",
                    """
                    DELETE FROM passive_secretary.messages
                    WHERE tenant_id=%s AND tenant_owner_id = ANY(%s)
                      AND source_id=%s AND test_run_id=%s
                    """,
                ),
            ):
                cursor.execute(sql, scope)
                rowcount = max(0, int(cursor.rowcount or 0))
                if key == "media_enrichments_deleted" and rowcount != media_enrichments:
                    raise LifecycleError("purge_count_invalid")
                if key == "outbound_intents_deleted" and rowcount != outbound_intents:
                    raise LifecycleError("purge_count_invalid")
                deleted[key] = rowcount

            for table in (
                "archive_events",
                "business_connections",
                "media_enrichments",
                "messages",
                "message_versions",
                "outbound_intents",
            ):
                cursor.execute(
                    f"""
                    SELECT COUNT(*) FROM passive_secretary.{table}
                    WHERE tenant_id=%s AND tenant_owner_id = ANY(%s)
                      AND source_id=%s AND test_run_id=%s
                    """,
                    scope,
                )
                if _fetch_count(cursor) != 0:
                    raise LifecycleError("purge_scope_not_empty")
            conn.commit()
            return deleted
    except LifecycleError:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    except Exception as exc:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        raise LifecycleError("purge_database_failed") from exc
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _canonical_existing_directory(path_text: str) -> Path | None:
    if not isinstance(path_text, str) or not path_text or "\x00" in path_text:
        return None
    candidate = Path(path_text)
    if not candidate.is_absolute():
        return None
    try:
        _reject_symlink_components(candidate)
        resolved = candidate.resolve(strict=True)
    except (LifecycleError, OSError):
        return None
    return resolved if resolved.is_dir() else None


def _exec_start_path(raw: str) -> Path | None:
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        return None
    match = re.search(r"(?:^|[\s{;])path=([^\s;}]+)", raw)
    if match is None:
        return None
    path_text = match.group(1)
    # systemctl escapes whitespace and other unusual bytes in this property.
    # The canonical deployment path contains none; reject rather than decode a
    # second grammar in a destructive precondition.
    if "\\" in path_text:
        return None
    candidate = Path(os.path.normpath(path_text))
    return candidate if candidate.is_absolute() else None


def _lexically_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return candidate != root


def _is_checkout_venv_python(argument: bytes, checkout: bytes) -> bool:
    prefix = re.escape(checkout + b"/venv/bin/python")
    return re.fullmatch(prefix + rb"(?:\d+(?:\.\d+)*)?", argument) is not None


def _invokes_hermes_gateway_module(arguments: list[bytes]) -> bool:
    for index, argument in enumerate(arguments[:-1]):
        if argument != b"-m":
            continue
        module = arguments[index + 1]
        if module == b"gateway.run":
            return True
        if module != b"hermes_cli.main":
            continue
        tail = arguments[index + 2 :]
        if any(
            tail[position : position + 2] == [b"gateway", b"run"]
            for position in range(len(tail) - 1)
        ):
            return True
    return False


def _require_no_checkout_process(
    paths: RuntimePaths,
    *,
    proc_root: Path = Path("/proc"),
) -> None:
    checkout_path = paths.hermes_home / "hermes-agent"
    checkout = str(checkout_path).encode("utf-8")
    try:
        entries = list(proc_root.iterdir())
    except OSError as exc:
        raise LifecycleError("process_state_unavailable") from exc
    for entry in entries:
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            info = entry.stat(follow_symlinks=False)
            if info.st_uid != paths.uid:
                continue
            command = (entry / "cmdline").read_bytes()[:1_048_577]
            try:
                cwd = (entry / "cwd").resolve(strict=True)
            except (FileNotFoundError, ProcessLookupError):
                cwd = None
            except PermissionError as exc:
                raise LifecycleError("process_state_unavailable") from exc
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError as exc:
            raise LifecycleError("process_state_unavailable") from exc
        except OSError as exc:
            raise LifecycleError("process_state_unavailable") from exc
        if len(command) > 1_048_576:
            raise LifecycleError("process_state_unavailable")
        arguments = [argument for argument in command.split(b"\x00") if argument]
        command_matches = any(
            argument.startswith(checkout + b"/")
            and not (index == 0 and _is_checkout_venv_python(argument, checkout))
            for index, argument in enumerate(arguments)
        ) or _invokes_hermes_gateway_module(arguments)
        cwd_matches = cwd == checkout_path or (
            cwd is not None and cwd.is_relative_to(checkout_path)
        )
        if command_matches or cwd_matches:
            raise LifecycleError("gateway_process_still_running")


def _require_trusted_unit_fragment(
    fragment_raw: str,
    service: str,
    *,
    unit_root: Path = Path("/etc/systemd/system"),
) -> None:
    """Bind a deterministic service name to a root-provisioned unit file.

    ``unit_root`` exists only so the trust predicate can be exercised against
    an isolated filesystem in regression tests. Production callers always use
    the canonical system unit directory.
    """

    expected = unit_root / service
    if fragment_raw != str(expected):
        raise LifecycleError("service_identity_mismatch")
    try:
        _reject_symlink_components(expected)
        info = expected.lstat()
    except (LifecycleError, OSError) as exc:
        raise LifecycleError("service_identity_mismatch") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != 0
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise LifecycleError("service_identity_mismatch")


def _require_service_stopped(service: str, paths: RuntimePaths) -> None:
    if not isinstance(service, str) or not SERVICE_RE.fullmatch(service):
        raise LifecycleError("service_name_invalid")
    if service != f"{paths.owner}-hermes.service":
        raise LifecycleError("service_identity_mismatch")
    try:
        result = subprocess.run(
            [
                "systemctl",
                "show",
                service,
                "--property=LoadState",
                "--property=ActiveState",
                "--property=MainPID",
                "--property=User",
                "--property=ExecStart",
                "--property=WorkingDirectory",
                "--property=FragmentPath",
                "--no-pager",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            check=False,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"},
        )
    except Exception as exc:
        raise LifecycleError("service_state_unavailable") from exc
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    if result.returncode != 0 or values.get("LoadState") != "loaded":
        raise LifecycleError("service_state_unavailable")
    if values.get("ActiveState") not in {"inactive", "failed"}:
        raise LifecycleError("purge_requires_service_stopped")

    # A caller-provided inactive unit name is not proof that the Hermes gateway
    # is stopped. Bind the loaded system unit to this exact Linux identity and
    # canonical Hermes checkout, and require systemd to report no main process.
    if values.get("MainPID") != "0" or values.get("User") != paths.owner:
        raise LifecycleError("service_identity_mismatch")
    _require_trusted_unit_fragment(values.get("FragmentPath", ""), service)

    expected_workdir = paths.hermes_home / "hermes-agent"
    try:
        helper = runpy.run_path(str(Path(__file__).with_name("shared_runtime_layout.py")))
        shared = helper["load_shared_runtime"](paths.owner, paths.hermes_home, paths.uid)
        if shared is None:
            _reject_symlink_components(expected_workdir)
        expected_workdir.resolve(strict=True)
    except (RuntimeError, OSError, ValueError) as exc:
        raise LifecycleError("service_identity_mismatch") from exc
    actual_workdir = _canonical_existing_directory(values.get("WorkingDirectory", ""))
    hermes_home_resolved = paths.hermes_home.resolve(strict=True)
    if actual_workdir != hermes_home_resolved:
        raise LifecycleError("service_identity_mismatch")

    exec_path = _exec_start_path(values.get("ExecStart", ""))
    expected_lexical = Path(os.path.normpath(str(expected_workdir)))
    if exec_path is None or not _lexically_within(exec_path, expected_lexical):
        raise LifecycleError("service_identity_mismatch")
    if not exec_path.is_file():
        raise LifecycleError("service_identity_mismatch")
    _require_no_checkout_process(paths)


def purge_test(args: argparse.Namespace) -> dict[str, bool | int]:
    paths = _resolve_paths(args.owner, args.hermes_home)
    with _lifecycle_lock(paths, exclusive=True):
        ctx = _load_context(paths)
        if (
            ctx.business_mode != BLOCKED_MODE
            or ctx.settings.capture_enabled
            or ctx.settings.auto_context_enabled
            or _search_tool_enabled(ctx.config)
            or ctx.settings.outbound_replies_enabled
            or not _business_reply_disabled(ctx.config)
            or _outbound_tool_enabled(ctx.config)
            or not _passive_media_disabled(ctx.config)
        ):
            raise LifecycleError("purge_requires_configured_disabled")
        if not ctx.settings.test_run_id:
            raise LifecycleError("production_scope_purge_refused")
        if args.test_run_id != ctx.settings.test_run_id:
            raise LifecycleError("test_run_scope_mismatch")
        if not _scope_matches(ctx):
            raise LifecycleError("owner_scope_mismatch")
        if not args.confirm_service_stopped:
            raise LifecycleError("service_stop_confirmation_required")
        _require_service_stopped(args.service, paths)
        spool_count, _spool_bytes, _spool_oldest_age, spool_safe = _spool_stats(ctx)
        if not spool_safe:
            raise LifecycleError("spool_path_unsafe")
        if spool_count:
            raise LifecycleError("spool_not_empty")
        secrets = _effective_secrets(ctx)
        counts = _purge_test_database(ctx.settings, secrets)
        return {"purged": True, **counts}


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--owner", required=True)
    parser.add_argument("--hermes-home", required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    status_parser = commands.add_parser("status", help="Show safe desired-state flags.")
    _add_common_arguments(status_parser)
    status_parser.set_defaults(handler=status)

    activate_parser = commands.add_parser("activate", help="Prepare passive capture activation.")
    _add_common_arguments(activate_parser)
    activation_scope = activate_parser.add_mutually_exclusive_group(required=True)
    activation_scope.add_argument("--test-run-id")
    activation_scope.add_argument("--production", action="store_true")
    activate_parser.add_argument(
        "--confirm-retention-and-scope",
        action="store_true",
        help="Required only for an explicit production activation.",
    )
    activate_parser.add_argument(
        "--enable-auto-context",
        action="store_true",
        help="Explicitly allow automatic 24-hour archive context injection.",
    )
    activate_parser.add_argument(
        "--enable-search-tool",
        action="store_true",
        help="Explicitly expose archive search to the owner model session.",
    )
    activate_parser.add_argument(
        "--enable-outbound-replies",
        action="store_true",
        help="Enable owner-confirmed Telegram Business replies.",
    )
    activate_parser.add_argument(
        "--enable-media-processing",
        action="store_true",
        help="Explicitly enable media download and transcription.",
    )
    activate_parser.add_argument(
        "--enable-group-passive",
        action="store_true",
        help="Ask the configured owner before passively capturing a newly added group.",
    )
    activate_parser.add_argument(
        "--media-asr-provider",
        choices=tuple(sorted(MEDIA_ASR_PROVIDERS)),
        help="ASR provider to use with --enable-media-processing.",
    )
    activate_parser.add_argument(
        "--confirm-outbound-policy",
        action="store_true",
        help="Confirm owner-only, one-shot approval and 24-hour reply policy.",
    )
    activate_parser.add_argument(
        "--confirm-transcript-policy",
        action="store_true",
        help="Confirm provider and Hermes transcript retention for archive text.",
    )
    activate_parser.set_defaults(handler=activate)

    deactivate_parser = commands.add_parser("deactivate", help="Prepare fail-closed deactivation.")
    _add_common_arguments(deactivate_parser)
    deactivate_parser.set_defaults(handler=deactivate)

    purge_parser = commands.add_parser("purge-test", help="Delete one exact test scope.")
    _add_common_arguments(purge_parser)
    purge_parser.add_argument("--test-run-id", required=True)
    purge_parser.add_argument("--service", required=True)
    purge_parser.add_argument(
        "--confirm-service-stopped",
        action="store_true",
        help="Confirm the named gateway unit was deliberately stopped for purge.",
    )
    purge_parser.set_defaults(handler=purge_test)
    return parser


def _print_result(result: dict[str, Any]) -> None:
    for key in sorted(result):
        value = result[key]
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, int):
            rendered = str(value)
        else:
            rendered = str(value)
        print(f"{key}={rendered}")


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = args.handler(args)
    except LifecycleError as exc:
        print(f"error_code={exc.code}", file=__import__("sys").stderr)
        return 1
    except Exception:
        # Never echo exception strings: database drivers can include a DSN.
        print("error_code=unexpected_failure", file=__import__("sys").stderr)
        return 1
    _print_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
