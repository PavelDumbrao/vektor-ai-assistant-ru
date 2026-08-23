#!/usr/bin/env python3
"""Install a fail-closed daily retention systemd timer for one Hermes owner."""

from __future__ import annotations

import argparse
import fcntl
import os
import pwd
import re
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator


BUNDLE_DIR = Path("/opt/hermes-passive-secretary")
SYSTEMD_UNIT_DIR = Path("/etc/systemd/system")
LOCK_DIR = Path("/run/lock/hermes-passive-secretary")
OWNER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
MAX_UNIT_BYTES = 1_048_576
TRUSTED_BUNDLE_FILES = (
    "install_retention_timer.py",
    "run_retention.py",
    "passive_secretary_plugin/__init__.py",
    "passive_secretary_plugin/archive.py",
    "passive_secretary_plugin/settings.py",
    "passive_secretary_plugin/schema.sql",
)


class TimerInstallError(RuntimeError):
    """Stable public error code; causes and command output stay private."""


@dataclass(frozen=True)
class OwnerRuntime:
    owner: str
    uid: int
    gid: int
    linux_home: Path
    hermes_home: Path
    python_bin: Path
    settings_path: Path
    env_path: Path


@dataclass(frozen=True)
class UnitSnapshot:
    content: bytes | None
    mode: int | None


@dataclass(frozen=True)
class TimerState:
    enabled: bool
    active: bool


@dataclass(frozen=True)
class InstallResult:
    changed: bool
    service: str
    timer: str


Systemctl = Callable[[tuple[str, ...], int], subprocess.CompletedProcess[str]]


def _reject_symlink_components(path: Path) -> None:
    if not path.is_absolute():
        raise TimerInstallError("path_not_absolute")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except OSError as exc:
            raise TimerInstallError("path_missing_or_unsafe") from exc
        if stat.S_ISLNK(info.st_mode):
            raise TimerInstallError("symlink_path_rejected")


def _require_root_ancestor_chain(path: Path) -> None:
    if not path.is_absolute():
        raise TimerInstallError("bundle_untrusted")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except OSError as exc:
            raise TimerInstallError("bundle_untrusted") from exc
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != 0
        ):
            raise TimerInstallError("bundle_untrusted")
        mode = stat.S_IMODE(info.st_mode)
        if mode & 0o022 and not info.st_mode & stat.S_ISVTX:
            raise TimerInstallError("bundle_untrusted")


def _require_root_node(path: Path, *, directory: bool, code: str) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise TimerInstallError(code) from exc
    expected_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (
        not expected_type
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != 0
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise TimerInstallError(code)


def _validate_bundle(
    bundle_dir: Path,
    launcher_path: Path,
    *,
    require_canonical: bool,
) -> None:
    if require_canonical and bundle_dir != BUNDLE_DIR:
        raise TimerInstallError("bundle_untrusted")
    if not bundle_dir.is_absolute() or launcher_path != bundle_dir / "install_retention_timer.py":
        raise TimerInstallError("bundle_untrusted")
    _require_root_ancestor_chain(bundle_dir.parent)
    _require_root_node(bundle_dir, directory=True, code="bundle_untrusted")
    checked_directories = {bundle_dir}
    for relative in TRUSTED_BUNDLE_FILES:
        candidate = bundle_dir / relative
        current = candidate.parent
        missing: list[Path] = []
        while current not in checked_directories:
            missing.append(current)
            current = current.parent
        for directory in reversed(missing):
            _require_root_node(directory, directory=True, code="bundle_untrusted")
            checked_directories.add(directory)
        _require_root_node(candidate, directory=False, code="bundle_untrusted")


def _require_owner_directory(path: Path, uid: int) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise TimerInstallError("owner_runtime_unsafe") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != uid
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise TimerInstallError("owner_runtime_unsafe")


def _require_owner_private_file(path: Path, uid: int, code: str) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise TimerInstallError(code) from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != uid
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        raise TimerInstallError(code)


def _validate_owner_runtime(owner: str) -> OwnerRuntime:
    if not OWNER_RE.fullmatch(owner):
        raise TimerInstallError("owner_invalid")
    try:
        entry = pwd.getpwnam(owner)
    except KeyError as exc:
        raise TimerInstallError("owner_unknown") from exc
    uid = int(entry.pw_uid)
    gid = int(entry.pw_gid)
    linux_home = Path(entry.pw_dir)
    expected_home = Path("/home") / owner
    if uid <= 0 or not linux_home.is_absolute() or linux_home != expected_home:
        raise TimerInstallError("owner_runtime_unsafe")
    _reject_symlink_components(linux_home)
    hermes_home = linux_home / ".hermes"
    agent_dir = hermes_home / "hermes-agent"
    plugin_dir = hermes_home / "plugins" / "passive-secretary"
    python_bin = agent_dir / "venv" / "bin" / "python"
    settings_path = plugin_dir / "settings.json"
    env_path = hermes_home / ".env"
    for directory in (
        linux_home,
        hermes_home,
        agent_dir,
        agent_dir / "venv",
        agent_dir / "venv" / "bin",
        hermes_home / "plugins",
        plugin_dir,
    ):
        _require_owner_directory(directory, uid)
    _require_owner_private_file(settings_path, uid, "settings_file_unsafe")
    _require_owner_private_file(env_path, uid, "environment_file_unsafe")

    # Venv launchers are normally symlinks. They are safe here because systemd
    # executes them after dropping to this same owner; still bind resolution to
    # the owner's home and reject a writable/non-executable final program.
    try:
        launcher_info = python_bin.lstat()
        resolved_python = python_bin.resolve(strict=True)
        program_info = resolved_python.stat()
        resolved_python.relative_to(linux_home)
    except (OSError, ValueError) as exc:
        raise TimerInstallError("python_runtime_unsafe") from exc
    if (
        launcher_info.st_uid != uid
        or not stat.S_ISREG(program_info.st_mode)
        or program_info.st_uid != uid
        or stat.S_IMODE(program_info.st_mode) & 0o022
        or not stat.S_IMODE(program_info.st_mode) & 0o111
    ):
        raise TimerInstallError("python_runtime_unsafe")
    return OwnerRuntime(
        owner=owner,
        uid=uid,
        gid=gid,
        linux_home=linux_home,
        hermes_home=hermes_home,
        python_bin=python_bin,
        settings_path=settings_path,
        env_path=env_path,
    )


def _unit_names(owner: str) -> tuple[str, str]:
    stem = f"{owner}-hermes-passive-secretary-retention"
    return f"{stem}.service", f"{stem}.timer"


def _service_unit(runtime: OwnerRuntime, bundle_dir: Path) -> str:
    run_script = bundle_dir / "run_retention.py"
    return (
        "[Unit]\n"
        "Description=Hermes Passive Secretary retention pass\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"User={runtime.owner}\n"
        f"Group={runtime.gid}\n"
        f"WorkingDirectory={runtime.hermes_home}\n"
        f"Environment=HOME={runtime.linux_home}\n"
        f"Environment=USER={runtime.owner}\n"
        f"Environment=LOGNAME={runtime.owner}\n"
        "Environment=PATH=/usr/bin:/bin\n"
        f"EnvironmentFile={runtime.env_path}\n"
        f"ExecStart={runtime.python_bin} -B -E -s {run_script} --settings {runtime.settings_path}\n"
        "NoNewPrivileges=true\n"
        "PrivateDevices=true\n"
        "PrivateTmp=true\n"
        "ProtectClock=true\n"
        "ProtectControlGroups=true\n"
        "ProtectHome=read-only\n"
        "ProtectHostname=true\n"
        "ProtectKernelLogs=true\n"
        "ProtectKernelModules=true\n"
        "ProtectKernelTunables=true\n"
        "ProtectSystem=strict\n"
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6\n"
        "RestrictNamespaces=true\n"
        "RestrictRealtime=true\n"
        "RestrictSUIDSGID=true\n"
        "LockPersonality=true\n"
        "MemoryDenyWriteExecute=true\n"
        "CapabilityBoundingSet=\n"
        "AmbientCapabilities=\n"
        "SystemCallArchitectures=native\n"
        "UMask=0077\n"
        "TimeoutStartSec=120\n"
        "StandardOutput=journal\n"
        "StandardError=journal\n"
    )


def _timer_unit(service: str) -> str:
    return (
        "[Unit]\n"
        "Description=Daily Hermes Passive Secretary retention\n\n"
        "[Timer]\n"
        "OnCalendar=daily\n"
        "Persistent=true\n"
        "RandomizedDelaySec=15m\n"
        "AccuracySec=1m\n"
        f"Unit={service}\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )


def _validate_unit_directory(unit_dir: Path, *, require_canonical: bool) -> None:
    if require_canonical and unit_dir != SYSTEMD_UNIT_DIR:
        raise TimerInstallError("unit_directory_unsafe")
    _require_root_ancestor_chain(unit_dir.parent)
    _require_root_node(unit_dir, directory=True, code="unit_directory_unsafe")


@contextmanager
def _installer_lock(
    lock_dir: Path,
    *,
    require_canonical: bool,
) -> Iterator[None]:
    """Serialize snapshot/write/smoke/rollback across all owner units."""

    if require_canonical and lock_dir != LOCK_DIR:
        raise TimerInstallError("installer_lock_unsafe")
    if not lock_dir.is_absolute():
        raise TimerInstallError("installer_lock_unsafe")
    _require_root_ancestor_chain(lock_dir.parent)
    try:
        lock_dir.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise TimerInstallError("installer_lock_unsafe") from exc
    _require_root_node(lock_dir, directory=True, code="installer_lock_unsafe")
    try:
        if stat.S_IMODE(lock_dir.lstat().st_mode) != 0o700:
            raise TimerInstallError("installer_lock_unsafe")
    except OSError as exc:
        raise TimerInstallError("installer_lock_unsafe") from exc

    lock_path = lock_dir / "retention-installer.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise TimerInstallError("installer_lock_unsafe") from exc
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise TimerInstallError("installer_lock_unsafe")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise TimerInstallError("installer_busy") from exc
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def _snapshot_unit(path: Path) -> UnitSnapshot:
    if not path.exists() and not path.is_symlink():
        return UnitSnapshot(None, None)
    try:
        info = path.lstat()
    except OSError as exc:
        raise TimerInstallError("existing_unit_unsafe") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != 0
        or stat.S_IMODE(info.st_mode) & 0o022
        or info.st_size > MAX_UNIT_BYTES
    ):
        raise TimerInstallError("existing_unit_unsafe")
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise TimerInstallError("existing_unit_unsafe") from exc
    return UnitSnapshot(content, stat.S_IMODE(info.st_mode))


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write_unit(path: Path, content: bytes, mode: int = 0o644) -> None:
    if path.is_symlink():
        raise TimerInstallError("unit_write_unsafe")
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except OSError as exc:
        raise TimerInstallError("unit_write_failed") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _restore_unit(path: Path, snapshot: UnitSnapshot) -> None:
    if snapshot.content is None:
        if path.is_symlink():
            raise TimerInstallError("unit_rollback_failed")
        try:
            path.unlink(missing_ok=True)
            _fsync_directory(path.parent)
        except OSError as exc:
            raise TimerInstallError("unit_rollback_failed") from exc
        return
    _atomic_write_unit(path, snapshot.content, snapshot.mode or 0o644)


def _run_systemctl(
    arguments: tuple[str, ...],
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ("/usr/bin/systemctl",) + arguments,
            cwd="/",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
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
        raise TimerInstallError("systemctl_unavailable") from exc


def _checked_systemctl(
    systemctl: Systemctl,
    arguments: tuple[str, ...],
    *,
    timeout: int,
    code: str,
) -> subprocess.CompletedProcess[str]:
    try:
        result = systemctl(arguments, timeout)
    except TimerInstallError:
        raise
    except Exception as exc:
        raise TimerInstallError("systemctl_unavailable") from exc
    if result.returncode != 0:
        raise TimerInstallError(code)
    return result


def _query_timer_state(systemctl: Systemctl, timer: str) -> TimerState:
    try:
        enabled = systemctl(("is-enabled", timer), 10)
        active = systemctl(("is-active", timer), 10)
    except Exception as exc:
        raise TimerInstallError("timer_state_unavailable") from exc
    enabled_value = enabled.stdout.strip()
    active_value = active.stdout.strip()
    if enabled.returncode == 0 and enabled_value in {"enabled", "enabled-runtime"}:
        was_enabled = True
    elif enabled.returncode in {1, 3, 4}:
        was_enabled = False
    else:
        raise TimerInstallError("timer_state_unavailable")
    if active.returncode == 0 and active_value == "active":
        was_active = True
    elif active.returncode in {1, 3, 4}:
        was_active = False
    else:
        raise TimerInstallError("timer_state_unavailable")
    return TimerState(was_enabled, was_active)


def _show_properties(
    systemctl: Systemctl,
    unit: str,
    properties: Iterable[str],
    *,
    code: str,
) -> dict[str, str]:
    property_names = tuple(properties)
    result = _checked_systemctl(
        systemctl,
        ("show", unit)
        + tuple(f"--property={name}" for name in property_names)
        + ("--no-pager",),
        timeout=15,
        code=code,
    )
    if len(result.stdout) > 65_536:
        raise TimerInstallError(code)
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in property_names and key not in values:
            values[key] = value
    if set(values) != set(property_names):
        raise TimerInstallError(code)
    return values


def _verify_smoke(
    systemctl: Systemctl,
    service: str,
    service_path: Path,
) -> None:
    values = _show_properties(
        systemctl,
        service,
        ("LoadState", "ActiveState", "Result", "ExecMainStatus", "FragmentPath"),
        code="retention_smoke_failed",
    )
    if values != {
        "LoadState": "loaded",
        "ActiveState": "inactive",
        "Result": "success",
        "ExecMainStatus": "0",
        "FragmentPath": str(service_path),
    }:
        raise TimerInstallError("retention_smoke_failed")


def _verify_loaded_definitions(
    systemctl: Systemctl,
    *,
    runtime: OwnerRuntime,
    bundle_dir: Path,
    service: str,
    timer: str,
    service_path: Path,
    timer_path: Path,
) -> None:
    """Reject systemd generators/drop-ins before any owner venv is executed."""

    service_values = _show_properties(
        systemctl,
        service,
        (
            "LoadState",
            "Type",
            "User",
            "Group",
            "ExecStart",
            "WorkingDirectory",
            "FragmentPath",
            "DropInPaths",
        ),
        code="service_definition_mismatch",
    )
    expected_argv = (
        f"{{ path={runtime.python_bin} ; argv[]={runtime.python_bin} -B -E -s "
        f"{bundle_dir / 'run_retention.py'} --settings {runtime.settings_path} ; "
    )
    if (
        service_values["LoadState"] != "loaded"
        or service_values["Type"] != "oneshot"
        or service_values["User"] != runtime.owner
        or service_values["Group"] != str(runtime.gid)
        or not service_values["ExecStart"].startswith(expected_argv)
        or service_values["WorkingDirectory"] != str(runtime.hermes_home)
        or service_values["FragmentPath"] != str(service_path)
        or service_values["DropInPaths"]
    ):
        raise TimerInstallError("service_definition_mismatch")

    timer_values = _show_properties(
        systemctl,
        timer,
        ("LoadState", "FragmentPath", "DropInPaths"),
        code="timer_definition_mismatch",
    )
    if timer_values != {
        "LoadState": "loaded",
        "FragmentPath": str(timer_path),
        "DropInPaths": "",
    }:
        raise TimerInstallError("timer_definition_mismatch")


def _verify_timer(
    systemctl: Systemctl,
    timer: str,
    timer_path: Path,
) -> None:
    values = _show_properties(
        systemctl,
        timer,
        ("LoadState", "ActiveState", "SubState", "UnitFileState", "FragmentPath"),
        code="timer_enable_failed",
    )
    if values != {
        "LoadState": "loaded",
        "ActiveState": "active",
        "SubState": "waiting",
        "UnitFileState": "enabled",
        "FragmentPath": str(timer_path),
    }:
        raise TimerInstallError("timer_enable_failed")


def _rollback_install(
    *,
    systemctl: Systemctl,
    timer: str,
    prior: TimerState,
    snapshots: dict[Path, UnitSnapshot],
    activation_attempted: bool,
) -> None:
    """Restore files and runtime state even when one rollback action fails."""

    failed = False
    if activation_attempted:
        try:
            failed = systemctl(("disable", "--now", timer), 30).returncode != 0
        except Exception:
            failed = True
    for path, snapshot in snapshots.items():
        try:
            _restore_unit(path, snapshot)
        except Exception:
            failed = True
    try:
        if systemctl(("daemon-reload",), 30).returncode != 0:
            failed = True
    except Exception:
        failed = True
    if activation_attempted:
        commands: list[tuple[str, ...]] = []
        if prior.enabled:
            commands.append(("enable", timer))
        if prior.active:
            commands.append(("start", timer))
        for command in commands:
            try:
                if systemctl(command, 30).returncode != 0:
                    failed = True
            except Exception:
                failed = True
    if failed:
        raise TimerInstallError("unit_rollback_failed")


def _install(
    owner: str,
    *,
    bundle_dir: Path,
    unit_dir: Path,
    lock_dir: Path,
    launcher_path: Path,
    systemctl: Systemctl,
    require_canonical: bool,
) -> InstallResult:
    if os.geteuid() != 0:
        raise TimerInstallError("root_required")
    _validate_bundle(bundle_dir, launcher_path, require_canonical=require_canonical)
    _validate_unit_directory(unit_dir, require_canonical=require_canonical)
    with _installer_lock(lock_dir, require_canonical=require_canonical):
        return _install_locked(
            owner,
            bundle_dir=bundle_dir,
            unit_dir=unit_dir,
            systemctl=systemctl,
        )


def _install_locked(
    owner: str,
    *,
    bundle_dir: Path,
    unit_dir: Path,
    systemctl: Systemctl,
) -> InstallResult:
    runtime = _validate_owner_runtime(owner)
    service, timer = _unit_names(owner)
    service_path = unit_dir / service
    timer_path = unit_dir / timer
    snapshots = {
        service_path: _snapshot_unit(service_path),
        timer_path: _snapshot_unit(timer_path),
    }
    prior = _query_timer_state(systemctl, timer)
    desired = {
        service_path: _service_unit(runtime, bundle_dir).encode("utf-8"),
        timer_path: _timer_unit(service).encode("utf-8"),
    }
    changed = any(
        snapshots[path].content != content or snapshots[path].mode != 0o644
        for path, content in desired.items()
    )
    activation_attempted = False
    try:
        if changed:
            for path, content in desired.items():
                _atomic_write_unit(path, content)
            _checked_systemctl(
                systemctl,
                ("daemon-reload",),
                timeout=30,
                code="daemon_reload_failed",
            )
        _verify_loaded_definitions(
            systemctl,
            runtime=runtime,
            bundle_dir=bundle_dir,
            service=service,
            timer=timer,
            service_path=service_path,
            timer_path=timer_path,
        )
        _checked_systemctl(
            systemctl,
            ("start", service),
            timeout=150,
            code="retention_smoke_failed",
        )
        _verify_smoke(systemctl, service, service_path)
        activation_attempted = True
        _checked_systemctl(
            systemctl,
            ("enable", "--now", timer),
            timeout=60,
            code="timer_enable_failed",
        )
        _verify_timer(systemctl, timer, timer_path)
    except BaseException as exc:
        try:
            _rollback_install(
                systemctl=systemctl,
                timer=timer,
                prior=prior,
                snapshots=snapshots,
                activation_attempted=activation_attempted,
            )
        except Exception as rollback_exc:
            raise TimerInstallError("unit_rollback_failed") from rollback_exc
        if isinstance(exc, TimerInstallError):
            raise
        raise TimerInstallError("retention_timer_install_failed") from exc
    return InstallResult(changed=changed, service=service, timer=timer)


def install(owner: str) -> InstallResult:
    return _install(
        owner,
        bundle_dir=BUNDLE_DIR,
        unit_dir=SYSTEMD_UNIT_DIR,
        lock_dir=LOCK_DIR,
        launcher_path=Path(__file__).absolute(),
        systemctl=_run_systemctl,
        require_canonical=True,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = install(args.owner)
    except TimerInstallError as exc:
        print(f"error={exc}", file=sys.stderr)
        return 1
    except Exception:
        print("error=unexpected_failure", file=sys.stderr)
        return 1
    print(f"installed={'true' if result.changed else 'already_current'}")
    print(f"service={result.service}")
    print(f"timer={result.timer}")
    print("smoke_test=passed")
    print("schedule=daily")
    print("persistent=true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
