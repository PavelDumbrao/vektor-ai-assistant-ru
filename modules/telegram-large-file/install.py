#!/usr/bin/env python3
"""Provision one Hermes profile for isolated Telegram Bot API local-mode files."""
from __future__ import annotations

import argparse
import grp
import hashlib
import os
import pwd
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

DATA_ROOT = Path("/opt/telegram-transcriber-bot/data/bot-api")
PRIVATE_ALIAS_ROOT = Path("/opt/vektor/telegram-ingress")
BOT_API_BASE = "http://127.0.0.1:8082"
SERVER_FILE_ROOT = "/var/lib/telegram-bot-api"
MOUNT_TARGET = "/run/hermes-telegram-local"
DEFAULT_MAX_BYTES = 1024 * 1024 * 1024
SHARED_BOT_API_GROUP = "telegram-transcriber"
OWNER_RE = re.compile(r"^[a-z][a-z0-9_-]{1,39}$")
CREDENTIAL_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{20,}$")


def _owner_account(owner: str):
    if not OWNER_RE.fullmatch(owner):
        raise ValueError("invalid_owner")
    return pwd.getpwnam(owner)


def _load_bot_credential(env_path: Path) -> str:
    if env_path.is_symlink() or not env_path.is_file():
        raise RuntimeError("profile_env_missing_or_unsafe")
    value = ""
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        if raw.startswith("TELEGRAM_BOT_TOKEN="):
            value = raw.partition("=")[2].strip().strip("\"'")
            break
    if not CREDENTIAL_RE.fullmatch(value):
        raise RuntimeError("telegram_credential_missing_or_invalid")
    return value


def _probe_local_api(credential: str) -> None:
    # Never include the request URL or upstream body in exceptions/logs.
    url = f"{BOT_API_BASE}/bot{credential}/getMe"
    request = urllib.request.Request(url, method="POST", data=b"")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status != 200:
                raise RuntimeError("local_bot_api_unavailable")
    except (OSError, urllib.error.URLError, urllib.error.HTTPError):
        raise RuntimeError("local_bot_api_unavailable") from None


def _tenant_directory(credential: str) -> Path:
    candidate = DATA_ROOT / credential
    try:
        os.lstat(candidate)
    except FileNotFoundError as exc:
        raise RuntimeError("local_bot_api_tenant_directory_missing") from exc
    if not candidate.is_dir() or candidate.is_symlink():
        raise RuntimeError("local_bot_api_tenant_directory_unsafe")
    if candidate.parent.resolve() != DATA_ROOT.resolve():
        raise RuntimeError("local_bot_api_tenant_directory_outside_root")
    return candidate


def _require_acl_tools() -> None:
    if not shutil.which("setfacl") or not shutil.which("getfacl"):
        raise RuntimeError("acl_tools_missing")


def _snapshot_acl(path: Path) -> bytes:
    result = subprocess.run(
        ["getfacl", "-R", "-P", "--absolute-names", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30,
    )
    if result.returncode:
        raise RuntimeError("telegram_tenant_acl_snapshot_failed")
    return result.stdout


def _apply_tenant_acl(owner: str, path: Path) -> None:
    access = subprocess.run(
        ["setfacl", "-R", "-P", "-m", f"u:{owner}:r-X", str(path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
    )
    if access.returncode:
        raise RuntimeError("telegram_tenant_acl_access_failed")
    directories = [path]
    for root, names, _files in os.walk(path, followlinks=False):
        root_path = Path(root)
        for name in names:
            candidate = root_path / name
            if candidate.is_symlink():
                raise RuntimeError("telegram_tenant_tree_contains_symlink")
            if candidate.is_dir():
                directories.append(candidate)
    for start in range(0, len(directories), 128):
        batch = directories[start:start + 128]
        default = subprocess.run(
            ["setfacl", "-m", f"d:u:{owner}:r-X", *map(str, batch)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
        )
        if default.returncode:
            raise RuntimeError("telegram_tenant_acl_default_failed")


def _restore_acl(snapshot: bytes) -> None:
    result = subprocess.run(
        ["setfacl", "--restore=-"], input=snapshot,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
    )
    if result.returncode:
        raise RuntimeError("telegram_tenant_acl_restore_failed")


def _ensure_alias_root() -> None:
    if PRIVATE_ALIAS_ROOT.exists():
        if PRIVATE_ALIAS_ROOT.is_symlink() or not PRIVATE_ALIAS_ROOT.is_dir():
            raise RuntimeError("telegram_ingress_alias_root_unsafe")
    else:
        PRIVATE_ALIAS_ROOT.mkdir(parents=True, mode=0o700)
    os.chown(PRIVATE_ALIAS_ROOT, 0, 0)
    os.chmod(PRIVATE_ALIAS_ROOT, 0o700)


def _install_private_alias(owner: str, tenant_dir: Path) -> Path:
    _ensure_alias_root()
    alias = PRIVATE_ALIAS_ROOT / owner
    if alias.exists() and not alias.is_symlink():
        raise RuntimeError("telegram_ingress_alias_collision")
    temporary = PRIVATE_ALIAS_ROOT / f".{owner}.{os.getpid()}.tmp"
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(tenant_dir)
    os.replace(temporary, alias)
    return alias


def _atomic_write(path: Path, content: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _dropin_path(owner: str) -> Path:
    return Path(f"/etc/systemd/system/{owner}-hermes.service.d/telegram-large-file.conf")


def _dropin_content(owner: str, tenant_sha256: str) -> bytes:
    alias = PRIVATE_ALIAS_ROOT / owner
    text = f"""[Service]
BindReadOnlyPaths={alias}:{MOUNT_TARGET}
InaccessiblePaths=/opt/telegram-transcriber-bot
Environment=HERMES_TELEGRAM_LOCAL_MOUNT={MOUNT_TARGET}
Environment=HERMES_TELEGRAM_TENANT_SHA256={tenant_sha256}
Environment=HERMES_TELEGRAM_LOCAL_SERVER_ROOT={SERVER_FILE_ROOT}
"""
    return text.encode("utf-8")


def _profile_config_set(owner: str, home: Path, key: str, value: str) -> None:
    python = home / ".hermes/hermes-agent/venv/bin/python"
    if not python.is_file():
        raise RuntimeError("profile_runtime_python_missing")
    command = [
        "runuser", "-u", owner, "--", "env",
        f"HOME={home}", f"HERMES_HOME={home / '.hermes'}",
        str(python), "-m", "hermes_cli.main",
        "config", "set", "--force", key, value,
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError(f"profile_config_set_failed:{key}")


def _configure_profile(owner: str, home: Path, max_bytes: int) -> None:
    values = {
        "platforms.telegram.extra.base_url": f"{BOT_API_BASE}/bot",
        "platforms.telegram.extra.base_file_url": f"{BOT_API_BASE}/file/bot",
        "platforms.telegram.extra.local_mode": "true",
        "platforms.telegram.extra.max_file_bytes": str(max_bytes),
    }
    for key, value in values.items():
        _profile_config_set(owner, home, key, value)


def _restore_owned_file(path: Path, data: bytes, uid: int, gid: int, mode: int) -> None:
    _atomic_write(path, data, mode)
    os.chown(path, uid, gid)


def install(owner: str, *, max_bytes: int, apply: bool, restart: bool) -> dict:
    if os.geteuid() != 0:
        raise PermissionError("root_required")
    if not 1 <= max_bytes <= 2 * 1024 * 1024 * 1024:
        raise ValueError("invalid_max_bytes")
    account = _owner_account(owner)
    home = Path(account.pw_dir)
    if home != Path("/home") / owner or home.is_symlink():
        raise RuntimeError("profile_home_unsafe")
    env_path = home / ".hermes/.env"
    config_path = home / ".hermes/config.yaml"
    if config_path.is_symlink() or not config_path.is_file():
        raise RuntimeError("profile_config_missing_or_unsafe")
    if env_path.stat().st_mode & 0o077:
        raise RuntimeError("profile_env_permissions_too_open")

    shared_group = grp.getgrnam(SHARED_BOT_API_GROUP)
    if shared_group.gr_gid in os.getgrouplist(owner, account.pw_gid):
        raise RuntimeError("owner_must_not_have_shared_bot_api_group")
    credential = _load_bot_credential(env_path)
    _probe_local_api(credential)
    tenant_dir = _tenant_directory(credential)
    _require_acl_tools()
    tenant_sha256 = hashlib.sha256(credential.encode("utf-8")).hexdigest()
    unit = Path(f"/etc/systemd/system/{owner}-hermes.service")
    if not unit.is_file() or unit.is_symlink():
        raise RuntimeError("profile_systemd_unit_missing_or_unsafe")
    alias = PRIVATE_ALIAS_ROOT / owner
    if alias.exists() and not alias.is_symlink():
        raise RuntimeError("telegram_ingress_alias_collision")
    dropin = _dropin_path(owner)
    if dropin.exists() and (dropin.is_symlink() or not dropin.is_file()):
        raise RuntimeError("telegram_ingress_dropin_unsafe")

    result = {
        "owner": owner,
        "state": "preflight_passed",
        "max_file_bytes": max_bytes,
        "account_shared_group_membership": False,
        "tenant_directory_present": True,
        "service_isolation": "exact_read_only_bind_plus_shared_root_blackhole",
    }
    if not apply:
        return result

    config_before = config_path.read_bytes()
    config_stat = config_path.stat()
    acl_before = _snapshot_acl(tenant_dir)
    dropin_before = dropin.read_bytes() if dropin.exists() else None
    alias_before = os.readlink(alias) if alias.is_symlink() else None
    wrote_state = False
    try:
        wrote_state = True
        _apply_tenant_acl(owner, tenant_dir)
        _install_private_alias(owner, tenant_dir)
        _atomic_write(dropin, _dropin_content(owner, tenant_sha256), 0o644)
        _configure_profile(owner, home, max_bytes)
        if credential.encode("utf-8") in config_path.read_bytes():
            raise RuntimeError("telegram_credential_leaked_into_config")
        subprocess.run(["systemctl", "daemon-reload"], check=True, timeout=20)
        if restart:
            subprocess.run(
                ["systemctl", "restart", f"{owner}-hermes.service"],
                check=True,
                timeout=90,
            )
            active = subprocess.run(
                ["systemctl", "is-active", "--quiet", f"{owner}-hermes.service"],
                timeout=10,
            )
            if active.returncode:
                raise RuntimeError("profile_service_failed_after_restart")
        result["state"] = "installed_and_restarted" if restart else "installed"
        result["dropin"] = str(dropin)
        result["mount_target"] = MOUNT_TARGET
        return result
    except Exception as exc:
        rollback_failed = False
        if wrote_state:
            try:
                _restore_owned_file(
                    config_path, config_before,
                    config_stat.st_uid, config_stat.st_gid, config_stat.st_mode & 0o777,
                )
                if dropin_before is None:
                    dropin.unlink(missing_ok=True)
                else:
                    _atomic_write(dropin, dropin_before, 0o644)
                if alias.is_symlink():
                    alias.unlink()
                if alias_before is not None:
                    previous = PRIVATE_ALIAS_ROOT / f".{owner}.rollback.{os.getpid()}"
                    previous.unlink(missing_ok=True)
                    previous.symlink_to(alias_before)
                    os.replace(previous, alias)
                subprocess.run(["systemctl", "daemon-reload"], check=True, timeout=20)
                if restart:
                    subprocess.run(
                        ["systemctl", "restart", f"{owner}-hermes.service"],
                        check=True, timeout=90,
                    )
            except Exception:
                rollback_failed = True
        try:
            _restore_acl(acl_before)
        except Exception:
            rollback_failed = True
        if rollback_failed:
            raise RuntimeError("telegram_large_file_rollback_incomplete") from exc
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--no-restart", action="store_true")
    args = parser.parse_args()
    result = install(
        args.owner,
        max_bytes=args.max_bytes,
        apply=args.apply,
        restart=not args.no_restart,
    )
    import json
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
