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
