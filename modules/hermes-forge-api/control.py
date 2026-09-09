#!/usr/bin/env python3
"""Root-only bounded control daemon for Hermes Forge Mini App/API."""
from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
import pwd
import re
import secrets
import socket
import socketserver
import struct
import subprocess
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

import yaml

MANAGER_ROOT = Path("/opt/proai-hermes-manager")
STATE_FILE = MANAGER_ROOT / "state/state.json"
SECRET_FILE = Path("/etc/proai-hermes-manager.env")
PROFILE_ROOT = Path("/opt/vektor/profiles")
HOME_ROOT = Path("/home")
SOCKET_PATH = Path("/run/proai-hermes-forge/control.sock")
API_USER = "www-data"
MAX_REQUEST_BYTES = 64 * 1024
SESSION_TTL_SECONDS = 30 * 60
INITDATA_MAX_AGE_SECONDS = 60 * 60
TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{20,}$")
PROFILE_RE = re.compile(r"^[a-z0-9_-]{2,40}$")
SAFE_VERSION_RE = re.compile(r"^[A-Za-z0-9_.+-]{1,80}$")
SAFE_SECRET_RE = re.compile(r"^[\x21-\x7e]{20,512}$")
ALLOWED_PERSONAL_SECRETS = frozenset({"MCP_MATON_API_KEY"})
CAPABILITY_IDS = (
    "finance", "github", "google-workspace", "image-studio", "maton",
    "telegram-secretary", "video-editor", "web-search",
)
PLANNED_CAPABILITIES = frozenset({"finance", "github", "google-workspace"})
CAPABILITY_TOGGLE_TOOLSETS = {
    "image-studio": "image_gen",
    "video-editor": "video_editor",
    "web-search": "web",
}
CAPABILITY_ACTIONS = frozenset({"enable", "disable"})
ACTION_STATE_ROOT = Path(os.environ.get("FORGE_ACTION_STATE_ROOT", "/var/lib/proai-hermes-forge"))
ACTION_JOBS_DIR = ACTION_STATE_ROOT / "jobs"
ACTION_DESIRED_DIR = ACTION_STATE_ROOT / "desired"
ACTION_AUDIT_DIR = ACTION_STATE_ROOT / "audit"
ACTION_AUDIT_FILE = ACTION_AUDIT_DIR / "capability-actions.jsonl"
ACTION_AUDIT_MAX_BYTES = 8 * 1024 * 1024
ACTION_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_ACTION_LOCK = threading.RLock()
CAPABILITY_HEALTH = frozenset({"healthy", "degraded", "unknown", "disabled", "planned"})
CAPABILITY_REASONS = frozenset({
    "ready", "disabled", "not_installed", "config_mismatch",
    "runtime_unavailable", "shared_dependency_unavailable",
    "external_check_required", "planned",
})
MATON_HOST = "api.maton.ai"
MATON_PATH = "/connections?limit=1"


class ControlError(RuntimeError):
    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code = code
        self.status = status


class SessionStore:
    def __init__(self, ttl: int = SESSION_TTL_SECONDS) -> None:
        self.ttl = ttl
        self._lock = threading.RLock()
        self._items: dict[str, tuple[int, float]] = {}

    def create(self, user_id: int, now: float | None = None) -> tuple[str, int]:
        current = time.time() if now is None else now
        token = secrets.token_urlsafe(32)
        expires = current + self.ttl
        with self._lock:
            self._prune(current)
            self._items[token] = (int(user_id), expires)
        return token, int(expires)

    def resolve(self, token: str, now: float | None = None) -> int:
        current = time.time() if now is None else now
        with self._lock:
            self._prune(current)
            item = self._items.get(token)
            if item is None or item[1] <= current:
                self._items.pop(token, None)
                raise ControlError("session_invalid", 401)
            return item[0]

    def _prune(self, now: float) -> None:
        for token, (_, expires) in list(self._items.items()):
            if expires <= now:
                self._items.pop(token, None)


SESSIONS = SessionStore()


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"\'')
    return values


def _manager_token() -> str:
    if SECRET_FILE.is_symlink() or not SECRET_FILE.is_file():
        raise ControlError("manager_secret_missing", 503)
    info = SECRET_FILE.stat()
    if info.st_uid != 0 or info.st_mode & 0o077:
        raise ControlError("manager_secret_unsafe", 503)
    for value in _read_env(SECRET_FILE).values():
        if TOKEN_RE.fullmatch(value):
            return value
    raise ControlError("manager_token_missing", 503)


def validate_init_data(init_data: str, *, bot_token: str | None = None, now: int | None = None) -> dict[str, Any]:
    if not isinstance(init_data, str) or not init_data or len(init_data) > 16_384:
        raise ControlError("init_data_invalid", 401)
    try:
        pairs = urllib.parse.parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise ControlError("init_data_invalid", 401) from None
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ControlError("init_data_duplicate_key", 401)
    values = dict(pairs)
    supplied_hash = values.pop("hash", "")
    if not re.fullmatch(r"[0-9a-f]{64}", supplied_hash):
        raise ControlError("init_data_hash_invalid", 401)
    data_check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    token = bot_token or _manager_token()
    secret_key = hmac.new(b"WebAppData", token.encode("utf-8"), hashlib.sha256).digest()
    expected = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, supplied_hash):
        raise ControlError("init_data_signature_invalid", 401)
    try:
        auth_date = int(values["auth_date"])
    except (KeyError, TypeError, ValueError):
        raise ControlError("init_data_auth_date_invalid", 401) from None
    current = int(time.time()) if now is None else int(now)
    if auth_date > current + 30 or current - auth_date > INITDATA_MAX_AGE_SECONDS:
        raise ControlError("init_data_expired", 401)
    try:
        user = json.loads(values["user"])
        user_id = int(user["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise ControlError("init_data_user_invalid", 401) from None
    if user_id <= 0:
        raise ControlError("init_data_user_invalid", 401)
    return {"user_id": user_id, "user": {"id": user_id, "first_name": str(user.get("first_name") or "")[:64]}}


def _safe_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _service_state(profile: str) -> str:
    result = subprocess.run(
        ["/usr/bin/systemctl", "is-active", f"{profile}-hermes.service"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    value = result.stdout.strip().lower()
    return value if value in {"active", "inactive", "failed", "activating", "deactivating"} else "unknown"


def _profile_release(profile: str) -> str:
    payload = _safe_json(PROFILE_ROOT / f"{profile}.json")
    release = str(payload.get("release_id") or "")
    return release if SAFE_VERSION_RE.fullmatch(release) else "unknown"


def _telemetry_enabled(profile: str) -> bool:
    path = HOME_ROOT / profile / ".hermes/config.yaml"
    if path.is_symlink() or not path.is_file():
        return False
    try:
        config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return False
    return ((config.get("telemetry") or {}).get("shared_metrics") or {}).get("enabled") is True


def _health(profile: str) -> dict[str, Any]:
    gateway = _safe_json(HOME_ROOT / profile / ".hermes/gateway_state.json")
    telegram = ((gateway.get("platforms") or {}).get("telegram") or {})
    telegram_state = str(telegram.get("state") or "unknown")
    if telegram_state not in {"connected", "disconnected", "connecting", "error", "unknown"}:
        telegram_state = "unknown"
    active_agents = gateway.get("active_agents", 0)
    active_agents = int(active_agents) if isinstance(active_agents, int) and not isinstance(active_agents, bool) and active_agents >= 0 else 0
    code_version = str(gateway.get("code_version") or "unknown")[:64]
    if not SAFE_VERSION_RE.fullmatch(code_version):
        code_version = "unknown"
    service = _service_state(profile)
    return {
        "service": service,
        "telegram": telegram_state,
        "active_agents": active_agents,
        "code_version": code_version,
        "release_id": _profile_release(profile),
        "telemetry_enabled": _telemetry_enabled(profile),
        "healthy": service == "active" and telegram_state == "connected",
    }


def _load_registry_state() -> dict[str, Any]:
    if STATE_FILE.is_symlink() or not STATE_FILE.is_file():
        raise ControlError("forge_state_missing", 503)
    info = STATE_FILE.stat()
    if info.st_uid != 0 or info.st_mode & 0o077:
        raise ControlError("forge_state_unsafe", 503)
    payload = _safe_json(STATE_FILE)
    if not payload:
        raise ControlError("forge_state_invalid", 503)
    return payload


def _owned_items(user_id: int) -> list[dict[str, Any]]:
    state = _load_registry_state()
    chosen: dict[str, dict[str, Any]] = {}
    for source in ("imported", "managed"):
        rows = state.get(source) or {}
        if not isinstance(rows, dict):
            continue
        for raw in rows.values():
            if not isinstance(raw, dict) or int(raw.get("owner_user_id") or 0) != int(user_id):
                continue
            profile = str(raw.get("profile") or "")
            if not PROFILE_RE.fullmatch(profile):
                continue
            item = {
                "profile": profile,
                "bot_id": int(raw.get("bot_id") or 0),
                "bot_username": str(raw.get("username") or "")[:64],
                "bot_name": str(raw.get("name") or "")[:64],
                "status": str(raw.get("profile_status") or "unknown")[:40],
                "source": source,
            }
            # Managed entries supersede imported metadata for the same profile.
            chosen[profile] = item
    return [chosen[key] for key in sorted(chosen)]


def _owned_item(user_id: int, profile: str) -> dict[str, Any]:
    if not PROFILE_RE.fullmatch(profile or ""):
        raise ControlError("profile_invalid", 400)
    for item in _owned_items(user_id):
        if item["profile"] == profile:
            return item
    raise ControlError("profile_not_found", 404)


def _public_hermes(item: dict[str, Any]) -> dict[str, Any]:
    profile = item["profile"]
    return {**item, "health": _health(profile)}


def _profile_paths(profile: str) -> tuple[pwd.struct_passwd, Path, Path]:
    if not PROFILE_RE.fullmatch(profile):
        raise ControlError("profile_invalid", 400)
    try:
        entry = pwd.getpwnam(profile)
    except KeyError:
        raise ControlError("profile_account_missing", 409) from None
    expected_home = HOME_ROOT / profile
    if Path(entry.pw_dir) != expected_home or entry.pw_uid <= 0:
        raise ControlError("profile_account_invalid", 409)
    hermes = expected_home / ".hermes"
    env_path = hermes / ".env"
    config_path = hermes / "config.yaml"
    if hermes.is_symlink() or not hermes.is_dir():
        raise ControlError("profile_home_unsafe", 409)
    return entry, env_path, config_path


def _secret_value(profile: str, name: str) -> str:
    if name not in ALLOWED_PERSONAL_SECRETS:
        raise ControlError("secret_not_allowed", 400)
    _, env_path, _ = _profile_paths(profile)
    if env_path.is_symlink() or not env_path.is_file():
        return ""
    return _read_env(env_path).get(name, "")


def _secret_status(profile: str, name: str) -> dict[str, Any]:
    value = _secret_value(profile, name)
    return {"name": name, "configured": bool(value), "last4": value[-4:] if value else ""}


def _atomic_text(path: Path, text: str, entry: pwd.struct_passwd) -> None:
    if path.is_symlink():
        raise ControlError("profile_file_unsafe", 409)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.forge.", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chown(temp, entry.pw_uid, entry.pw_gid)
        os.chmod(temp, 0o600)
        os.replace(temp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temp.exists():
            temp.unlink()


def _backup_secret_files(profile: str, entry: pwd.struct_passwd, env_path: Path, config_path: Path) -> Path:
    root = Path(entry.pw_dir) / ".hermes/backups" / f"forge-secret-{time.time_ns()}"
    root.mkdir(parents=True, mode=0o700)
    os.chown(root, entry.pw_uid, entry.pw_gid)
    os.chmod(root, 0o700)
    presence: dict[str, bool] = {}
    for source in (env_path, config_path):
        existed = source.is_file() and not source.is_symlink()
        presence[source.name] = existed
        if existed:
            target = root / source.name
            target.write_bytes(source.read_bytes())
            os.chown(target, entry.pw_uid, entry.pw_gid)
            os.chmod(target, 0o600)
    marker = root / "presence.json"
    marker.write_text(json.dumps(presence, sort_keys=True), encoding="utf-8")
    os.chown(marker, entry.pw_uid, entry.pw_gid)
    os.chmod(marker, 0o600)
    return root


def _restore_secret_backup(profile: str, backup: Path) -> None:
    entry, env_path, config_path = _profile_paths(profile)
    try:
        presence = json.loads((backup / "presence.json").read_text(encoding="utf-8"))
    except Exception:
        raise ControlError("secret_rollback_failed", 500) from None
    for target in (env_path, config_path):
        source = backup / target.name
        if presence.get(target.name) is True:
            if not source.is_file():
                raise ControlError("secret_rollback_failed", 500)
            _atomic_text(target, source.read_text(encoding="utf-8"), entry)
        elif target.exists():
            if target.is_symlink() or not target.is_file():
                raise ControlError("secret_rollback_failed", 500)
            target.unlink()


def _set_env_secret(profile: str, name: str, value: str) -> Path:
    if name not in ALLOWED_PERSONAL_SECRETS or not SAFE_SECRET_RE.fullmatch(value or ""):
        raise ControlError("secret_value_invalid", 400)
    entry, env_path, config_path = _profile_paths(profile)
    backup = _backup_secret_files(profile, entry, env_path, config_path)
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.is_file() and not env_path.is_symlink() else []
    output: list[str] = []
    replaced = False
    for line in lines:
        if line.startswith(name + "="):
            if not replaced:
                output.append(name + "=" + value)
                replaced = True
            continue
        output.append(line)
    if not replaced:
        output.append(name + "=" + value)
    _atomic_text(env_path, "\n".join(output).rstrip() + "\n", entry)
    return backup


def _delete_env_secret(profile: str, name: str) -> Path:
    if name not in ALLOWED_PERSONAL_SECRETS:
        raise ControlError("secret_not_allowed", 400)
    entry, env_path, config_path = _profile_paths(profile)
    backup = _backup_secret_files(profile, entry, env_path, config_path)
    if env_path.is_file() and not env_path.is_symlink():
        lines = [line for line in env_path.read_text(encoding="utf-8").splitlines() if not line.startswith(name + "=")]
        _atomic_text(env_path, "\n".join(lines).rstrip() + "\n", entry)
    return backup


def _set_maton_enabled(profile: str, enabled: bool) -> None:
    entry, _, config_path = _profile_paths(profile)
    if config_path.is_symlink() or not config_path.is_file():
        raise ControlError("config_missing", 409)
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:
        raise ControlError("config_invalid", 409) from None
    if not isinstance(config, dict):
        raise ControlError("config_invalid", 409)
    servers = config.setdefault("mcp_servers", {})
    if not isinstance(servers, dict):
        raise ControlError("config_invalid", 409)
    maton = servers.setdefault("maton", {})
    if not isinstance(maton, dict):
        raise ControlError("config_invalid", 409)
    maton["enabled"] = bool(enabled)
    _atomic_text(config_path, yaml.safe_dump(config, allow_unicode=True, sort_keys=False), entry)


def _maton_validate(value: str) -> str:
    if not SAFE_SECRET_RE.fullmatch(value or ""):
        return "invalid"
    connection = http.client.HTTPSConnection(MATON_HOST, timeout=15)
    try:
        connection.request("GET", MATON_PATH, headers={"Authorization": "Bearer " + value, "Accept": "application/json", "User-Agent": "Hermes-Forge/1.0"})
        response = connection.getresponse()
        status = int(response.status)
        response.read(2048)
        if 200 <= status < 300:
            return "valid"
        return "invalid" if status == 401 else "temporary"
    except (OSError, TimeoutError, http.client.HTTPException):
        return "temporary"
    finally:
        connection.close()


def _maton_status(profile: str, *, validate: bool = False) -> dict[str, Any]:
    value = _secret_value(profile, "MCP_MATON_API_KEY")
    configured = bool(value)
    _, _, config_path = _profile_paths(profile)
    enabled = False
    if config_path.is_file() and not config_path.is_symlink():
        try:
            config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            enabled = (((config.get("mcp_servers") or {}).get("maton") or {}).get("enabled") is True)
        except Exception:
            enabled = False
    result = {"id": "maton", "configured": configured, "enabled": enabled, "last4": value[-4:] if value else ""}
    if validate:
        result["check"] = _maton_validate(value) if value else "missing"
    return result


def _unit_active(unit: str) -> bool:
    result = subprocess.run(
        ["/usr/bin/systemctl", "is-active", "--quiet", unit],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False,
    )
    return result.returncode == 0


def _unit_enabled(unit: str) -> bool:
    result = subprocess.run(
        ["/usr/bin/systemctl", "is-enabled", "--quiet", unit],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False,
    )
    return result.returncode == 0


def _env_key_present(path: Path, name: str) -> bool:
    if path.is_symlink() or not path.is_file() or name not in ALLOWED_PERSONAL_SECRETS:
        return False
    try:
        with path.open("r", encoding="utf-8") as handle:
            for raw in handle:
                if raw.startswith(name + "=") and raw.partition("=")[2].strip():
                    return True
    except OSError:
        return False
    return False


def _capability_row(capability_id: str, installed: bool, enabled: bool, health: str, reason: str) -> dict[str, Any]:
    if capability_id not in CAPABILITY_IDS or health not in CAPABILITY_HEALTH or reason not in CAPABILITY_REASONS:
        raise ControlError("capability_state_invalid", 500)
    return {
        "id": capability_id,
        "installed": bool(installed),
        "enabled": bool(enabled),
        "health": health,
        "reason": reason,
    }


def _capability_states(profile: str) -> dict[str, Any]:
    entry, env_path, config_path = _profile_paths(profile)
    if config_path.is_symlink() or not config_path.is_file():
        raise ControlError("config_missing", 409)
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:
        raise ControlError("config_invalid", 409) from None
    if not isinstance(config, dict):
        raise ControlError("config_invalid", 409)

    hermes = Path(entry.pw_dir) / ".hermes"
    plugins_raw = (config.get("plugins") or {}).get("enabled", [])
    plugins = {str(item) for item in plugins_raw if isinstance(item, str)} if isinstance(plugins_raw, list) else set()
    disabled_raw = (config.get("agent") or {}).get("disabled_toolsets", [])
    disabled = {str(item) for item in disabled_raw if isinstance(item, str)} if isinstance(disabled_raw, list) else set()
    service_active = _service_state(profile) == "active"

    rows = [_capability_row(item, False, False, "planned", "planned") for item in sorted(PLANNED_CAPABILITIES)]

    image_cfg = config.get("image_gen") or {}
    image_provider = str(image_cfg.get("provider") or "") if isinstance(image_cfg, dict) else ""
    image_marker = hermes / "plugins/image_gen/grsai/plugin.yaml"
    image_installed = image_marker.is_file() and not image_marker.is_symlink()
    image_signals = image_installed and "image_gen/grsai" in plugins and image_provider == "grsai"
    image_enabled = image_signals and "image_gen" not in disabled
    if image_enabled:
        rows.append(_capability_row("image-studio", True, True, "healthy" if service_active else "degraded", "ready" if service_active else "runtime_unavailable"))
    elif image_installed and ("image_gen/grsai" in plugins or image_provider == "grsai") and "image_gen" not in disabled:
        rows.append(_capability_row("image-studio", True, False, "degraded", "config_mismatch"))
    else:
        rows.append(_capability_row("image-studio", image_installed, False, "disabled", "disabled" if image_installed else "not_installed"))

    mcp = config.get("mcp_servers") or {}
    maton_cfg = mcp.get("maton") if isinstance(mcp, dict) else None
    maton_installed = isinstance(maton_cfg, dict)
    maton_config_enabled = bool(maton_cfg.get("enabled") is True) if maton_installed else False
    maton_secret = _env_key_present(env_path, "MCP_MATON_API_KEY")
    maton_enabled = maton_installed and maton_config_enabled and maton_secret
    if maton_enabled:
        rows.append(_capability_row("maton", True, True, "unknown", "external_check_required"))
    elif maton_installed and maton_config_enabled != maton_secret:
        rows.append(_capability_row("maton", True, False, "degraded", "config_mismatch"))
    else:
        rows.append(_capability_row("maton", maton_installed, False, "disabled", "disabled" if maton_installed else "not_installed"))

    secretary_plugin = hermes / "plugins/passive-secretary/plugin.yaml"
    secretary_settings = hermes / "plugins/passive-secretary/settings.json"
    secretary_installed = all(path.is_file() and not path.is_symlink() for path in (secretary_plugin, secretary_settings))
    secretary_enabled = secretary_installed and "passive-secretary" in plugins and "passive_secretary" not in disabled
    secretary_maintenance = _unit_enabled(f"{profile}-hermes-passive-secretary-retention.timer") if secretary_enabled else False
    if secretary_enabled and service_active and secretary_maintenance:
        rows.append(_capability_row("telegram-secretary", True, True, "healthy", "ready"))
    elif secretary_enabled:
        reason = "runtime_unavailable" if not service_active else "shared_dependency_unavailable"
        rows.append(_capability_row("telegram-secretary", True, True, "degraded", reason))
    else:
        rows.append(_capability_row("telegram-secretary", secretary_installed, False, "disabled", "disabled" if secretary_installed else "not_installed"))

    video_plugin = hermes / "plugins/video-editor/plugin.yaml"
    video_skill = hermes / "skills/video-editor/SKILL.md"
    video_installed = all(path.is_file() and not path.is_symlink() for path in (video_plugin, video_skill))
    video_configured = video_installed and "video-editor" in plugins
    video_enabled = video_configured and "video_editor" not in disabled
    video_broker = _unit_active("vektor-video-asr-broker.service") if video_enabled else False
    if video_enabled and service_active and video_broker:
        rows.append(_capability_row("video-editor", True, True, "healthy", "ready"))
    elif video_enabled:
        reason = "runtime_unavailable" if not service_active else "shared_dependency_unavailable"
        rows.append(_capability_row("video-editor", True, True, "degraded", reason))
    elif video_configured:
        rows.append(_capability_row("video-editor", True, False, "disabled", "disabled"))
    elif video_installed:
        rows.append(_capability_row("video-editor", True, False, "degraded", "config_mismatch"))
    else:
        rows.append(_capability_row("video-editor", False, False, "disabled", "not_installed"))

    search_cfg = config.get("search") or {}
    search_provider = str(search_cfg.get("provider") or "") if isinstance(search_cfg, dict) else ""
    provider_safe = bool(re.fullmatch(r"[a-z0-9_-]{1,40}", search_provider))
    search_registry = hermes / "hermes-agent/agent/web_search_registry.py"
    search_plugin = hermes / "hermes-agent/plugins/web" / search_provider / "plugin.yaml" if provider_safe else Path("/nonexistent")
    search_installed = search_registry.is_file() and search_plugin.is_file()
    search_enabled = search_installed and "web" not in disabled
    if search_enabled:
        rows.append(_capability_row("web-search", True, True, "healthy" if service_active else "degraded", "ready" if service_active else "runtime_unavailable"))
    elif provider_safe and search_registry.is_file():
        rows.append(_capability_row("web-search", search_installed, False, "degraded" if not search_installed else "disabled", "not_installed" if not search_installed else "disabled"))
    else:
        rows.append(_capability_row("web-search", False, False, "disabled", "not_installed"))

    ordered = {row["id"]: row for row in rows}
    return {"schema": "hermes.capability-state/v1", "items": [ordered[item] for item in CAPABILITY_IDS]}


def _active_sessions(profile: str) -> int:
    payload = _safe_json(HOME_ROOT / profile / ".hermes/runtime/active_sessions.json")
    entries = payload.get("entries")
    return len(entries) if isinstance(entries, list) else 0


def _restart(profile: str) -> dict[str, Any]:
    before = _health(profile)
    if before["active_agents"] != 0 or _active_sessions(profile) != 0:
        raise ControlError("profile_busy", 409)
    old_pid = str(_safe_json(HOME_ROOT / profile / ".hermes/gateway_state.json").get("pid") or "")
    result = subprocess.run(["/usr/bin/systemctl", "restart", f"{profile}-hermes.service"], capture_output=True, text=True, timeout=60, check=False)
    if result.returncode:
        raise ControlError("restart_failed", 503)
    deadline = time.monotonic() + 75
    while time.monotonic() < deadline:
        current = _health(profile)
        gateway = _safe_json(HOME_ROOT / profile / ".hermes/gateway_state.json")
        pid = str(gateway.get("pid") or "")
        if current["healthy"] and pid and pid != old_pid:
            return current
        time.sleep(1)
    raise ControlError("restart_health_timeout", 503)


def _ensure_action_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise ControlError("action_state_unsafe", 500)
    os.chmod(path, 0o700)
    if os.geteuid() == 0:
        os.chown(path, 0, 0)


def _ensure_action_state() -> None:
    for path in (ACTION_STATE_ROOT, ACTION_JOBS_DIR, ACTION_DESIRED_DIR, ACTION_AUDIT_DIR):
        _ensure_action_dir(path)


def _atomic_root_json(path: Path, payload: dict[str, Any]) -> None:
    _ensure_action_dir(path.parent)
    if path.is_symlink():
        raise ControlError("action_state_unsafe", 500)
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, 0o600)
        if os.geteuid() == 0:
            os.chown(temp, 0, 0)
        os.replace(temp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temp.exists():
            temp.unlink()


def _rotate_audit_if_needed() -> None:
    path = ACTION_AUDIT_FILE
    if not path.exists():
        return
    if path.is_symlink() or not path.is_file():
        raise ControlError("audit_state_unsafe", 500)
    if path.stat().st_size < ACTION_AUDIT_MAX_BYTES:
        return
    rotated = path.with_suffix(path.suffix + ".1")
    if rotated.exists():
        if rotated.is_symlink() or not rotated.is_file():
            raise ControlError("audit_state_unsafe", 500)
        rotated.unlink()
    os.replace(path, rotated)
    os.chmod(rotated, 0o600)


def _append_action_audit(job: dict[str, Any], outcome: str, error_code: str = "") -> None:
    _ensure_action_state()
    event = {
        "schema": "hermes.capability-audit/v1",
        "action_id": job["action_id"],
        "actor_user_id": int(job["actor_user_id"]),
        "profile": job["profile"],
        "capability_id": job["capability_id"],
        "action": job["action"],
        "outcome": outcome,
        "error_code": str(error_code or "")[:64],
        "release_id": job["release_id"],
        "timestamp": int(time.time()),
    }
    raw = (json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if len(raw) > 4096:
        raise ControlError("audit_event_invalid", 500)
    with _ACTION_LOCK:
        _rotate_audit_if_needed()
        if ACTION_AUDIT_FILE.is_symlink():
            raise ControlError("audit_state_unsafe", 500)
        fd = os.open(ACTION_AUDIT_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.fchmod(fd, 0o600)
            if os.geteuid() == 0:
                os.fchown(fd, 0, 0)
            os.write(fd, raw)
            os.fsync(fd)
        finally:
            os.close(fd)


def _action_job(actor_user_id: int, profile: str, capability_id: str, action: str) -> dict[str, Any]:
    action_id = secrets.token_hex(16)
    if not ACTION_ID_RE.fullmatch(action_id):
        raise ControlError("action_id_invalid", 500)
    timestamp = int(time.time())
    return {
        "schema": "hermes.capability-action/v1",
        "action_id": action_id,
        "actor_user_id": int(actor_user_id),
        "profile": profile,
        "capability_id": capability_id,
        "action": action,
        "target_enabled": action == "enable",
        "release_id": _profile_release(profile),
        "state": "requested",
        "error_code": "",
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def _write_action_job(job: dict[str, Any]) -> None:
    action_id = str(job.get("action_id") or "")
    if not ACTION_ID_RE.fullmatch(action_id):
        raise ControlError("action_id_invalid", 500)
    _atomic_root_json(ACTION_JOBS_DIR / f"capability-{action_id}.json", job)


def _finish_action_job(job: dict[str, Any], state: str, error_code: str = "") -> None:
    job["state"] = state
    job["error_code"] = str(error_code or "")[:64]
    job["updated_at"] = int(time.time())
    _write_action_job(job)
    try:
        _append_action_audit(job, state, job["error_code"])
        job["audit_recorded"] = True
    except Exception:
        job["audit_recorded"] = False
    _write_action_job(job)


def _desired_path(profile: str) -> Path:
    if not PROFILE_RE.fullmatch(profile):
        raise ControlError("profile_invalid", 400)
    return ACTION_DESIRED_DIR / f"{profile}.json"


def _load_desired(profile: str) -> dict[str, Any]:
    path = _desired_path(profile)
    if not path.exists():
        return {"schema": "hermes.capability-desired/v1", "profile": profile, "capabilities": {}}
    if path.is_symlink() or not path.is_file():
        raise ControlError("desired_state_unsafe", 500)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        raise ControlError("desired_state_invalid", 500) from None
    if not isinstance(payload, dict) or payload.get("schema") != "hermes.capability-desired/v1" or payload.get("profile") != profile:
        raise ControlError("desired_state_invalid", 500)
    capabilities = payload.get("capabilities")
    if not isinstance(capabilities, dict):
        raise ControlError("desired_state_invalid", 500)
    for key, value in capabilities.items():
        if key not in CAPABILITY_IDS or not isinstance(value, dict) or type(value.get("enabled")) is not bool:
            raise ControlError("desired_state_invalid", 500)
    return payload


def _persist_desired(profile: str, capability_id: str, enabled: bool, action_id: str) -> None:
    payload = _load_desired(profile)
    capabilities = dict(payload.get("capabilities") or {})
    capabilities[capability_id] = {
        "enabled": bool(enabled),
        "action_id": action_id,
        "updated_at": int(time.time()),
    }
    payload["capabilities"] = capabilities
    payload["updated_at"] = int(time.time())
    _atomic_root_json(_desired_path(profile), payload)


def _backup_capability_config(profile: str, action_id: str) -> Path:
    if not ACTION_ID_RE.fullmatch(action_id):
        raise ControlError("action_id_invalid", 500)
    entry, _, config_path = _profile_paths(profile)
    if config_path.is_symlink() or not config_path.is_file():
        raise ControlError("config_missing", 409)
    root = Path(entry.pw_dir) / ".hermes/backups" / f"forge-capability-{action_id}"
    if root.exists() or root.is_symlink():
        raise ControlError("capability_backup_conflict", 500)
    root.mkdir(parents=True, mode=0o700)
    os.chown(root, entry.pw_uid, entry.pw_gid)
    os.chmod(root, 0o700)
    target = root / "config.yaml"
    target.write_bytes(config_path.read_bytes())
    os.chown(target, entry.pw_uid, entry.pw_gid)
    os.chmod(target, 0o600)
    return root


def _restore_capability_config(profile: str, backup: Path) -> None:
    entry, _, config_path = _profile_paths(profile)
    expected_root = Path(entry.pw_dir) / ".hermes/backups"
    try:
        resolved = backup.resolve(strict=True)
        if resolved.parent != expected_root.resolve(strict=True) or not resolved.name.startswith("forge-capability-"):
            raise ValueError
    except Exception:
        raise ControlError("capability_rollback_failed", 500) from None
    source = resolved / "config.yaml"
    if source.is_symlink() or not source.is_file():
        raise ControlError("capability_rollback_failed", 500)
    _atomic_text(config_path, source.read_text(encoding="utf-8"), entry)


def _set_capability_toolset(profile: str, capability_id: str, enabled: bool) -> bool:
    toolset = CAPABILITY_TOGGLE_TOOLSETS.get(capability_id)
    if not toolset:
        raise ControlError("capability_action_not_supported", 409)
    entry, _, config_path = _profile_paths(profile)
    if config_path.is_symlink() or not config_path.is_file():
        raise ControlError("config_missing", 409)
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:
        raise ControlError("config_invalid", 409) from None
    if not isinstance(config, dict):
        raise ControlError("config_invalid", 409)
    agent = config.setdefault("agent", {})
    if not isinstance(agent, dict):
        raise ControlError("config_invalid", 409)
    raw = agent.setdefault("disabled_toolsets", [])
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise ControlError("config_invalid", 409)
    disabled = list(raw)
    before = list(disabled)
    if enabled:
        disabled = [item for item in disabled if item != toolset]
    elif toolset not in disabled:
        disabled.append(toolset)
    if disabled == before:
        return False
    agent["disabled_toolsets"] = disabled
    _atomic_text(config_path, yaml.safe_dump(config, allow_unicode=True, sort_keys=False), entry)
    return True


def _capability_by_id(profile: str, capability_id: str) -> dict[str, Any]:
    rows = _capability_states(profile)["items"]
    for row in rows:
        if row["id"] == capability_id:
            return row
    raise ControlError("capability_state_missing", 500)


def _assert_action_target(capability_id: str, action: str, current: dict[str, Any]) -> None:
    if capability_id not in CAPABILITY_IDS or action not in CAPABILITY_ACTIONS:
        raise ControlError("capability_action_invalid", 400)
    if capability_id in PLANNED_CAPABILITIES:
        raise ControlError("capability_planned", 409)
    if capability_id == "maton":
        raise ControlError("capability_requires_connection", 409)
    if capability_id == "telegram-secretary":
        raise ControlError("capability_requires_consent", 409)
    if capability_id not in CAPABILITY_TOGGLE_TOOLSETS:
        raise ControlError("capability_action_not_supported", 409)
    if not current.get("installed"):
        raise ControlError("capability_not_installed", 409)
    if current.get("reason") == "config_mismatch":
        raise ControlError("capability_config_mismatch", 409)


def _assert_profile_actionable(profile: str) -> None:
    health = _health(profile)
    if not health.get("healthy"):
        raise ControlError("profile_unhealthy", 409)
    if int(health.get("active_agents") or 0) != 0 or _active_sessions(profile) != 0:
        raise ControlError("profile_busy", 409)


def _verify_capability_target(profile: str, capability_id: str, enabled: bool) -> dict[str, Any]:
    state = _capability_by_id(profile, capability_id)
    if bool(state.get("enabled")) != bool(enabled):
        raise ControlError("capability_verification_failed", 503)
    if enabled and state.get("health") != "healthy":
        raise ControlError("capability_verification_failed", 503)
    if not enabled and state.get("health") != "disabled":
        raise ControlError("capability_verification_failed", 503)
    return state


def _capability_action(actor_user_id: int, profile: str, capability_id: str, action: str) -> dict[str, Any]:
    if capability_id not in CAPABILITY_IDS or action not in CAPABILITY_ACTIONS:
        raise ControlError("capability_action_invalid", 400)
    with _ACTION_LOCK:
        _ensure_action_state()
        _load_desired(profile)  # Fail closed before any tenant mutation if persistent state is corrupt.
        job = _action_job(actor_user_id, profile, capability_id, action)
        _write_action_job(job)
        _append_action_audit(job, "requested")
        current = _capability_by_id(profile, capability_id)
        try:
            _assert_action_target(capability_id, action, current)
        except ControlError as exc:
            _finish_action_job(job, "rejected", exc.code)
            raise
        target_enabled = action == "enable"
        if bool(current.get("enabled")) == target_enabled:
            _persist_desired(profile, capability_id, target_enabled, job["action_id"])
            _finish_action_job(job, "unchanged")
            return {"action_id": job["action_id"], "outcome": "unchanged", "capability": current, "restarted": False}
        try:
            _assert_profile_actionable(profile)
        except ControlError as exc:
            _finish_action_job(job, "blocked", exc.code)
            raise

        backup: Path | None = None
        mutated = False
        try:
            job["state"] = "running"
            job["updated_at"] = int(time.time())
            _write_action_job(job)
            backup = _backup_capability_config(profile, job["action_id"])
            mutated = _set_capability_toolset(profile, capability_id, target_enabled)
            if not mutated:
                raise ControlError("capability_mutation_noop", 500)
            _restart(profile)
            final_state = _verify_capability_target(profile, capability_id, target_enabled)
            _persist_desired(profile, capability_id, target_enabled, job["action_id"])
            _finish_action_job(job, "success")
            return {"action_id": job["action_id"], "outcome": "success", "capability": final_state, "restarted": True}
        except Exception as exc:
            code = exc.code if isinstance(exc, ControlError) else "capability_action_failed"
            if backup is not None and mutated:
                try:
                    _restore_capability_config(profile, backup)
                    _restart(profile)
                    _finish_action_job(job, "rolled_back", code)
                except Exception:
                    _finish_action_job(job, "failed", "capability_rollback_failed")
                    raise ControlError("capability_rollback_failed", 500) from None
            else:
                _finish_action_job(job, "failed", code)
            if isinstance(exc, ControlError):
                raise
            raise ControlError("capability_action_failed", 500) from None


def _connections(profile: str) -> list[dict[str, Any]]:
    health = _health(profile)
    return [
        {"id": "telegram", "configured": True, "status": health["telegram"]},
        _maton_status(profile),
    ]


def _authenticate(payload: dict[str, Any]) -> dict[str, Any]:
    auth = validate_init_data(str(payload.get("init_data") or ""))
    token, expires_at = SESSIONS.create(auth["user_id"])
    return {"session": token, "expires_at": expires_at, "user": auth["user"]}


def _require_session(payload: dict[str, Any]) -> int:
    token = str(payload.get("session") or "")
    if not token or len(token) > 128:
        raise ControlError("session_invalid", 401)
    return SESSIONS.resolve(token)


def dispatch(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ControlError("request_invalid", 400)
    op = str(payload.get("op") or "")
    if op == "authenticate":
        return _authenticate(payload)
    user_id = _require_session(payload)
    if op == "list_hermes":
        return {"items": [_public_hermes(item) for item in _owned_items(user_id)]}
    profile = str(payload.get("profile") or "")
    item = _owned_item(user_id, profile)
    if op == "get_hermes":
        return _public_hermes(item)
    if op in {"health", "health_check"}:
        result = _health(profile)
        if op == "health_check" and _secret_value(profile, "MCP_MATON_API_KEY"):
            result["maton"] = _maton_status(profile, validate=True)
        return result
    if op == "restart":
        return _restart(profile)
    if op == "list_connections":
        return {"items": _connections(profile)}
    if op == "list_capabilities":
        return _capability_states(profile)
    if op == "capability_action":
        return _capability_action(
            user_id, profile, str(payload.get("capability_id") or ""), str(payload.get("action") or ""),
        )
    if op == "list_secrets":
        return {"items": [_secret_status(profile, name) for name in sorted(ALLOWED_PERSONAL_SECRETS)]}
    if op == "set_secret":
        name = str(payload.get("name") or "")
        value = str(payload.get("value") or "")
        if name != "MCP_MATON_API_KEY":
            raise ControlError("secret_not_allowed", 400)
        validation = _maton_validate(value)
        if validation == "invalid":
            raise ControlError("maton_key_invalid", 400)
        if validation != "valid":
            raise ControlError("maton_temporarily_unavailable", 503)
        backup = _set_env_secret(profile, name, value)
        try:
            _set_maton_enabled(profile, True)
        except Exception:
            _restore_secret_backup(profile, backup)
            raise
        return {"secret": _secret_status(profile, name), "connection": _maton_status(profile), "restart_required": True}
    if op == "delete_secret":
        name = str(payload.get("name") or "")
        backup = _delete_env_secret(profile, name)
        try:
            if name == "MCP_MATON_API_KEY":
                _set_maton_enabled(profile, False)
        except Exception:
            _restore_secret_backup(profile, backup)
            raise
        return {"secret": _secret_status(profile, name), "restart_required": True}
    if op == "test_connection":
        connection_id = str(payload.get("connection_id") or "")
        if connection_id != "maton":
            raise ControlError("connection_not_allowed", 400)
        return _maton_status(profile, validate=True)
    raise ControlError("operation_not_allowed", 404)


def _peer_allowed(sock: socket.socket) -> bool:
    try:
        _, uid, _ = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
        api_uid = pwd.getpwnam(API_USER).pw_uid
    except Exception:
        return False
    return uid in {0, api_uid}


class Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        if not _peer_allowed(self.request):
            return
        line = self.rfile.readline(MAX_REQUEST_BYTES + 1)
        if not line or len(line) > MAX_REQUEST_BYTES or not line.endswith(b"\n"):
            self._write({"ok": False, "error": "request_too_large", "status": 413})
            return
        try:
            payload = json.loads(line.decode("utf-8"))
            result = dispatch(payload)
            self._write({"ok": True, "result": result})
        except ControlError as exc:
            self._write({"ok": False, "error": exc.code, "status": exc.status})
        except Exception:
            self._write({"ok": False, "error": "internal_error", "status": 500})

    def _write(self, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        self.wfile.write(raw)


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False


def serve() -> None:
    if os.geteuid() != 0:
        raise SystemExit("root required")
    SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    api_entry = pwd.getpwnam(API_USER)
    os.chown(SOCKET_PATH.parent, 0, api_entry.pw_gid)
    os.chmod(SOCKET_PATH.parent, 0o750)
    if SOCKET_PATH.is_symlink():
        raise SystemExit("unsafe control socket")
    if SOCKET_PATH.exists():
        SOCKET_PATH.unlink()
    server = Server(str(SOCKET_PATH), Handler)
    os.chown(SOCKET_PATH, 0, api_entry.pw_gid)
    os.chmod(SOCKET_PATH, 0o660)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        try:
            SOCKET_PATH.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    serve()
