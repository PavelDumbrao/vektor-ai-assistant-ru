#!/usr/bin/env python3
"""Bounded root provisioner for clean Hermes Forge tenants.

The public Forge surface must never expose arbitrary command execution. This
module accepts only validated HermesInstance data and performs a fixed sequence
of idempotent profile operations.
"""
from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import runpy
import shutil
import stat
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

import yaml

from hermes_instance import HermesInstance, owner_for_telegram_id

ROOT = Path("/opt/vektor")
MANAGER_ROOT = Path("/opt/proai-hermes-manager")
PLATFORM_ENV = Path("/etc/proai-hermes-platform.env")
SYSTEMD_ROOT = Path("/etc/systemd/system")
TMPFILES_ROOT = Path("/etc/tmpfiles.d")
USERADD = Path("/usr/sbin/useradd")
SYSTEMCTL = Path("/usr/bin/systemctl")
SYSTEM_PYTHON = Path("/usr/bin/python3")
TECH_ADMIN_ID = 450206471
TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{20,}$")
REQUIRED_PLATFORM_KEYS = ("LLM_API_KEY", "FALLBACK_LLM_API_KEY")
OPTIONAL_PLATFORM_KEYS = ("OPENROUTER_API_KEY", "TAVILY_API_KEY", "GRSAI_API_KEY")


class ProvisionError(RuntimeError):
    pass


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"\'')
    return values


def _atomic_write(path: Path, content: str, *, uid: int, gid: int, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chown(temp, uid, gid)
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def _safe_token(path: Path, bot_id: int) -> str:
    expected = MANAGER_ROOT / "state" / "managed" / f"{bot_id}.env"
    if path != expected or path.is_symlink() or not path.is_file():
        raise ProvisionError("managed_token_path_invalid")
    info = path.stat()
    if info.st_uid != 0 or info.st_mode & 0o077:
        raise ProvisionError("managed_token_file_unsafe")
    token = _read_env(path).get("TELEGRAM_BOT_TOKEN", "")
    if not TOKEN_RE.fullmatch(token):
        raise ProvisionError("managed_token_invalid")
    return token


def _platform_values(path: Path = PLATFORM_ENV) -> dict[str, str]:
    if path.is_symlink() or not path.is_file():
        raise ProvisionError("platform_secret_file_missing")
    info = path.stat()
    if info.st_uid != 0 or info.st_mode & 0o077:
        raise ProvisionError("platform_secret_file_unsafe")
    values = _read_env(path)
    missing = [key for key in REQUIRED_PLATFORM_KEYS if not values.get(key)]
    if missing:
        raise ProvisionError("platform_secret_missing:" + ",".join(missing))
    return {key: values[key] for key in (*REQUIRED_PLATFORM_KEYS, *OPTIONAL_PLATFORM_KEYS) if values.get(key)}


def _verified_release(release_id: str) -> tuple[Path, dict]:
    release = ROOT / "releases" / release_id
    runtime = release / "runtime.json"
    code = release / "hermes-agent"
    if release.is_symlink() or not runtime.is_file() or not code.is_dir():
        raise ProvisionError("release_missing")
    payload = json.loads(runtime.read_text(encoding="utf-8"))
    if payload.get("state") != "ready" or payload.get("release_id") != release_id:
        raise ProvisionError("release_not_verified")
    if payload.get("schema_rollback_compatible") is not True:
        raise ProvisionError("release_not_rollback_compatible")
    return release, payload


def _ensure_account(owner: str) -> pwd.struct_passwd:
    try:
        entry = pwd.getpwnam(owner)
    except KeyError:
        result = subprocess.run(
            [str(USERADD), "--create-home", "--shell", "/bin/bash", owner],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if result.returncode:
            raise ProvisionError("linux_user_create_failed")
        entry = pwd.getpwnam(owner)
    if entry.pw_uid <= 0 or Path(entry.pw_dir) != Path("/home") / owner:
        raise ProvisionError("linux_user_identity_invalid")
    return entry


def _ensure_private_dir(path: Path, entry: pwd.struct_passwd) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ProvisionError("profile_directory_symlink")
    os.chown(path, entry.pw_uid, entry.pw_gid)
    os.chmod(path, 0o700)


def _render_profile(instance: HermesInstance, entry: pwd.struct_passwd) -> Path:
    template_root = MANAGER_ROOT / "templates" / "personal"
    hermes = Path(entry.pw_dir) / ".hermes"
    for relative in (
        ".", "bin", "plugins", "memories", "logs", "cron", "runtime",
        "skills", "tools", "hooks", "cache", "cache/audio", "backups",
    ):
        _ensure_private_dir(hermes / relative, entry)
    for relative in ("workspace", "workspace/knowledge", "workspace/documents", "workspace/data"):
        _ensure_private_dir(Path(entry.pw_dir) / relative, entry)

    replacements = {
        "@OWNER@": instance.owner_linux,
        "@OWNER_TELEGRAM_ID@": str(instance.owner_telegram_id),
        "@TECH_ADMIN_TELEGRAM_ID@": str(TECH_ADMIN_ID),
    }
    for name in ("config.yaml", "SOUL.md", "USER.md"):
        source = template_root / name
        if source.is_symlink() or not source.is_file():
            raise ProvisionError(f"profile_template_missing:{name}")
        text = source.read_text(encoding="utf-8")
        for old, new in replacements.items():
            text = text.replace(old, new)
        target = hermes / ("memories/USER.md" if name == "USER.md" else name)
        if not target.exists():
            _atomic_write(target, text, uid=entry.pw_uid, gid=entry.pw_gid)
    return hermes


def _ensure_profile_env(
    instance: HermesInstance,
    entry: pwd.struct_passwd,
    hermes: Path,
    token: str,
    platform_values: dict[str, str],
) -> None:
    path = hermes / ".env"
    existing = _read_env(path) if path.is_file() and not path.is_symlink() else {}
    if existing.get("TELEGRAM_BOT_TOKEN") not in (None, "", token):
        raise ProvisionError("telegram_token_conflict")
    desired = dict(existing)
    desired.update(platform_values)
    desired.update({
        "TELEGRAM_BOT_TOKEN": token,
        "TELEGRAM_ALLOWED_USERS": f"{instance.owner_telegram_id},{TECH_ADMIN_ID}",
        "MCP_MATON_API_KEY": existing.get("MCP_MATON_API_KEY", ""),
        "HERMES_DOC_WORKSPACE": str(Path(entry.pw_dir) / "workspace"),
        "HERMES_DOC_AUTHOR": instance.bot_name,
        "HERMES_TELEGRAM_DISABLE_FALLBACK_IPS": "1",
    })
    if any("\n" in value or "\r" in value for value in desired.values()):
        raise ProvisionError("environment_value_invalid")
    content = "".join(f"{key}={value}\n" for key, value in sorted(desired.items()))
    _atomic_write(path, content, uid=entry.pw_uid, gid=entry.pw_gid)


def _ensure_symlink(path: Path, target: Path, entry: pwd.struct_passwd) -> None:
    if path.is_symlink():
        if path.resolve() != target.resolve():
            raise ProvisionError(f"symlink_target_conflict:{path.name}")
        return
    if path.exists():
        raise ProvisionError(f"symlink_path_conflict:{path.name}")
    path.symlink_to(target)
    os.lchown(path, entry.pw_uid, entry.pw_gid)


def _bind_runtime(instance: HermesInstance, entry: pwd.struct_passwd, hermes: Path, release: Path) -> None:
    code = release / "hermes-agent"
    _ensure_symlink(hermes / "hermes-agent", code, entry)
    for name in ("uv", "uvx"):
        _ensure_symlink(hermes / "bin" / name, ROOT / "tools" / "uv-0.12.3" / name, entry)

    registration = ROOT / "profiles" / f"{instance.owner_linux}.json"
    payload = {"schema_version": 1, "owner": instance.owner_linux, "release_id": instance.release_id}
    expected = json.dumps(payload, sort_keys=True) + "\n"
    if registration.exists():
        current = json.loads(registration.read_text(encoding="utf-8"))
        if current != payload:
            raise ProvisionError("profile_registration_conflict")
    else:
        _atomic_write(registration, expected, uid=0, gid=0, mode=0o644)

    layout_path = ROOT / "admin" / "shared_runtime_layout.py"
    layout = runpy.run_path(str(layout_path))
    layout["load_shared_runtime"](instance.owner_linux, hermes, entry.pw_uid)


def _install_service(instance: HermesInstance, entry: pwd.struct_passwd) -> Path:
    temporary = Path("/tmp/vkr") / str(entry.pw_uid)
    temporary.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    if not temporary.exists():
        temporary.mkdir(mode=0o700)
        os.chown(temporary, entry.pw_uid, entry.pw_gid)
    elif temporary.is_symlink():
        raise ProvisionError("runtime_tmp_symlink")

    tmpfiles = TMPFILES_ROOT / f"vektor-shared-{instance.owner_linux}.conf"
    tmp_content = (
        "d /tmp/vkr 0755 root root -\n"
        f"d {temporary} 0700 {instance.owner_linux} {instance.owner_linux} -\n"
    )
    if not tmpfiles.exists():
        _atomic_write(tmpfiles, tmp_content, uid=0, gid=0, mode=0o644)

    source = MANAGER_ROOT / "templates" / "hermes.service.template"
    text = source.read_text(encoding="utf-8")
    text = text.replace("@OWNER@", instance.owner_linux).replace("@UID@", str(entry.pw_uid))
    unit = SYSTEMD_ROOT / f"{instance.owner_linux}-hermes.service"
    if unit.exists() and unit.read_text(encoding="utf-8") != text:
        raise ProvisionError("systemd_unit_conflict")
    if not unit.exists():
        _atomic_write(unit, text, uid=0, gid=0, mode=0o644)
    return unit


def _run(args: list[str], *, timeout: int = 300, user: int | None = None, group: int | None = None, env: dict[str, str] | None = None) -> None:
    kwargs = {
        "capture_output": True,
        "text": True,
        "timeout": timeout,
        "check": False,
    }
    if user is not None:
        kwargs.update(user=user, group=group, extra_groups=())
    if env is not None:
        kwargs["env"] = env
    result = subprocess.run(args, **kwargs)
    if result.returncode:
        raise ProvisionError("subprocess_failed:" + Path(args[0]).name)


def _provision_database(instance: HermesInstance, entry: pwd.struct_passwd, hermes: Path) -> None:
    script = MANAGER_ROOT / "vendor" / "passive-secretary-postgres" / "provision_postgres.py"
    _run([
        str(SYSTEM_PYTHON), "-I", str(script), "provision",
        "--client-id", instance.owner_linux,
        "--tenant-id", instance.owner_linux,
        "--owner", instance.owner_linux,
        "--hermes-env", str(hermes / ".env"),
    ], timeout=180)


def _install_passive_secretary(instance: HermesInstance, entry: pwd.struct_passwd, hermes: Path) -> None:
    script = MANAGER_ROOT / "vendor" / "passive-secretary" / "install_passive_secretary.py"
    _run([
        str(SYSTEM_PYTHON), str(script), "install",
        "--client", instance.owner_linux,
        "--owner", instance.owner_linux,
        "--hermes-home", str(hermes),
        "--tenant-id", instance.owner_linux,
        "--source-id", "telegram_business",
        "--owner-user-id", str(instance.owner_telegram_id),
        "--retention-days", "365",
        "--skip-deps",
    ], timeout=180)


def _install_maton(entry: pwd.struct_passwd, hermes: Path, release: Path) -> None:
    source = MANAGER_ROOT / "vendor" / "maton-onboarding"
    staged = hermes / "setup-maton"
    if staged.is_symlink():
        raise ProvisionError("maton_staging_symlink")
    if staged.exists():
        shutil.rmtree(staged)
    shutil.copytree(source, staged, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for root, directories, files in os.walk(staged):
        os.chown(root, entry.pw_uid, entry.pw_gid)
        os.chmod(root, 0o700)
        for filename in files:
            path = Path(root) / filename
            os.chown(path, entry.pw_uid, entry.pw_gid)
            os.chmod(path, 0o600)
    script = staged / "install.py"
    env = {
        "HOME": entry.pw_dir, "USER": entry.pw_name, "LOGNAME": entry.pw_name,
        "HERMES_HOME": str(hermes), "PATH": "/usr/local/bin:/usr/bin:/bin",
    }
    _run([str(release / "venv/bin/python"), str(script), "--hermes-home", str(hermes)],
         timeout=60, user=entry.pw_uid, group=entry.pw_gid, env=env)


def _install_grsai(instance: HermesInstance, platform_values: dict[str, str]) -> None:
    if not platform_values.get("GRSAI_API_KEY"):
        return
    script = MANAGER_ROOT / "vendor" / "grsai-image-provider" / "install.py"
    _run([str(SYSTEM_PYTHON), str(script), "--owner", instance.owner_linux], timeout=60)


def _start_service(instance: HermesInstance) -> None:
    service = f"{instance.owner_linux}-hermes.service"
    _run([str(SYSTEMCTL), "daemon-reload"], timeout=30)
    _run([str(SYSTEMCTL), "enable", service], timeout=30)
    _run([str(SYSTEMCTL), "restart", service], timeout=60)


def _telegram_identity(token: str) -> str:
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/getMe",
        data=b"{}",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not payload.get("ok"):
        raise ProvisionError("telegram_health_failed")
    return str((payload.get("result") or {}).get("username") or "").lower()


def _wait_healthy(instance: HermesInstance, token: str, expected_version: str, timeout: int = 75) -> dict[str, object]:
    service = f"{instance.owner_linux}-hermes.service"
    home = Path("/home") / instance.owner_linux / ".hermes"
    deadline = time.monotonic() + timeout
    last_gateway: dict = {}
    while time.monotonic() < deadline:
        active = subprocess.run(
            [str(SYSTEMCTL), "is-active", "--quiet", service],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode == 0
        gateway_path = home / "gateway_state.json"
        if gateway_path.is_file() and not gateway_path.is_symlink():
            try:
                last_gateway = json.loads(gateway_path.read_text(encoding="utf-8"))
            except Exception:
                last_gateway = {}
        telegram = (last_gateway.get("platforms") or {}).get("telegram") or {}
        if active and telegram.get("state") == "connected" and last_gateway.get("code_version") == expected_version:
            if _telegram_identity(token) != instance.bot_username:
                raise ProvisionError("telegram_identity_mismatch")
            return {
                "service": True,
                "telegram": True,
                "runtime_version": expected_version,
                "gateway_pid": int(last_gateway.get("pid") or 0),
            }
        time.sleep(2)
    raise ProvisionError("profile_health_timeout")


def _instance_path(instance: HermesInstance) -> Path:
    return MANAGER_ROOT / "state" / "instances" / f"{instance.instance_id}.json"


def _receipt_path(instance: HermesInstance) -> Path:
    return MANAGER_ROOT / "state" / "provisioning" / f"{instance.instance_id}.json"


def _write_root_json(path: Path, payload: dict) -> None:
    _atomic_write(
        path,
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        uid=0,
        gid=0,
    )


def _record(instance: HermesInstance, state: str, **fields: object) -> None:
    _write_root_json(_receipt_path(instance), {
        "schema_version": 1,
        "instance_id": instance.instance_id,
        "owner": instance.owner_linux,
        "state": state,
        "release_id": instance.release_id,
        "updated_at": int(time.time()),
        **fields,
    })


def provision(instance: HermesInstance, token_file: Path | None = None) -> dict[str, object]:
    if os.geteuid() != 0:
        raise ProvisionError("root_required")
    instance.validate()
    token_path = token_file or (MANAGER_ROOT / "state" / "managed" / f"{instance.bot_id}.env")
    token = _safe_token(token_path, instance.bot_id)
    platform_values = _platform_values()
    release, runtime = _verified_release(instance.release_id)
    instance_path = _instance_path(instance)
    if instance_path.exists():
        current = HermesInstance.from_dict(json.loads(instance_path.read_text(encoding="utf-8")))
        if current != instance:
            raise ProvisionError("instance_desired_state_conflict")
    else:
        instance.write_atomic(instance_path)

    _record(instance, "provisioning")
    try:
        entry = _ensure_account(instance.owner_linux)
        hermes = _render_profile(instance, entry)
        _ensure_profile_env(instance, entry, hermes, token, platform_values)
        _bind_runtime(instance, entry, hermes, release)
        _install_service(instance, entry)
        _provision_database(instance, entry, hermes)
        _install_passive_secretary(instance, entry, hermes)
        _install_maton(entry, hermes, release)
        _install_grsai(instance, platform_values)
        _start_service(instance)
        health = _wait_healthy(instance, token, str(runtime.get("version") or ""))
    except Exception as exc:
        code = str(exc) if isinstance(exc, ProvisionError) else type(exc).__name__
        _record(instance, "failed", error_code=code[:120])
        raise
    _record(instance, "active", health=health)
    return {
        "instance_id": instance.instance_id,
        "owner": instance.owner_linux,
        "state": "active",
        "release_id": instance.release_id,
        "health": health,
    }


def load_instance_file(path: Path) -> HermesInstance:
    directory = MANAGER_ROOT / "state" / "instances"
    try:
        resolved_parent = path.parent.resolve(strict=True)
    except OSError as exc:
        raise ProvisionError("instance_file_parent_missing") from exc
    if resolved_parent != directory.resolve() or path.is_symlink() or not path.is_file():
        raise ProvisionError("instance_file_invalid")
    info = path.stat()
    if info.st_uid != 0 or info.st_mode & 0o077:
        raise ProvisionError("instance_file_unsafe")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return HermesInstance.from_dict(payload)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("provision",))
    parser.add_argument("--instance-file", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        instance = load_instance_file(args.instance_file)
        result = provision(instance)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        print("secrets_printed=false")
        return 0
    except ProvisionError as exc:
        print("error=" + str(exc), file=__import__("sys").stderr)
        return 1
    except Exception:
        print("error=unexpected_failure", file=__import__("sys").stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
