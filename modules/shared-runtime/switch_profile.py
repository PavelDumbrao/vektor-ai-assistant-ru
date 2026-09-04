#!/usr/bin/env python3
"""Switch one idle legacy profile to its prepared shared runtime, with rollback."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import pwd
import re
import runpy
import shlex
import stat
import subprocess
import tempfile
import time
from pathlib import Path


ROOT = Path("/opt/vektor")
HOME_ROOT = Path("/home")
UNIT_ROOT = Path("/etc/systemd/system")
TMPFILES_ROOT = Path("/etc/tmpfiles.d")
PRIVATE_TMP_ROOT = Path("/tmp/vkr")
OWNER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
SAFE_ENV = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def command(*args, timeout=60) -> str:
    result = subprocess.run(args, env=SAFE_ENV, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError(f"command_failed:{Path(args[0]).name}:{result.returncode}")
    return result.stdout.strip()


def atomic_write(path: Path, content: bytes, mode: int = 0o600) -> None:
    fd, raw = tempfile.mkstemp(prefix=".vektor-", dir=path.parent)
    candidate = Path(raw)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(candidate, path)
    finally:
        if candidate.exists():
            candidate.unlink()


def edit_unit(text: str, temporary: Path) -> str:
    lines = text.splitlines()
    if lines.count("[Service]") != 1:
        raise ValueError("unit_service_section_invalid")
    start = lines.index("[Service]") + 1
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("[")), len(lines))
    additions = []
    masks = [line.split("=", 1)[1] for line in lines[start:end] if line.startswith("UMask=")]
    if masks and any(int(value, 8) != 0o077 for value in masks):
        raise ValueError("existing_umask_requires_review")
    if not masks:
        additions.append("UMask=0077")
    temp_values = []
    for line in lines[start:end]:
        if line.startswith("Environment="):
            temp_values.extend(item.partition("=")[2] for item in shlex.split(line.split("=", 1)[1])
                               if item.startswith("TMPDIR="))
    if temp_values and any(value != str(temporary) for value in temp_values):
        raise ValueError("existing_tmpdir_requires_review")
    if not temp_values:
        additions.append(f'Environment="TMPDIR={temporary}"')
    lines[end:end] = additions + ([""] if additions else [])
    return "\n".join(lines) + "\n"


def service_state(owner: str) -> dict:
    raw = command("systemctl", "show", f"{owner}-hermes.service",
                  "-p", "ActiveState", "-p", "MainPID", "-p", "User", "-p", "WorkingDirectory")
    return dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)


def protected_hashes(home: Path) -> dict[Path, str]:
    paths = [home / ".env", home / "config.yaml", home / "SOUL.md"]
    paths.extend((home / "plugins").glob("*/settings.json"))
    return {path: digest(path) for path in paths if path.is_file()}


def wait_ready(owner: str, home: Path, *, timeout=90) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = service_state(owner)
        try:
            gateway = json.loads((home / "gateway_state.json").read_text())
        except (OSError, ValueError):
            gateway = {}
        if (state.get("ActiveState") == "active" and str(gateway.get("pid")) == state.get("MainPID")
                and gateway.get("gateway_state") == "running"
                and gateway.get("platforms", {}).get("telegram", {}).get("state") == "connected"):
            return state
        if state.get("ActiveState") == "failed":
            raise RuntimeError("gateway_failed_after_switch")
        time.sleep(2)
    raise RuntimeError("gateway_readiness_timeout")


def helper_path() -> Path:
    sibling = Path(__file__).with_name("shared_runtime_layout.py")
    return sibling if sibling.exists() else Path(__file__).resolve().parents[1] / "passive-secretary/shared_runtime_layout.py"


def switch(owner: str, *, apply=False) -> dict:
    if os.geteuid() != 0 or not OWNER_RE.fullmatch(owner):
        raise ValueError("administrator_and_valid_owner_required")
    entry = pwd.getpwnam(owner)
    home = Path(entry.pw_dir) / ".hermes"
    if Path(entry.pw_dir) != HOME_ROOT / owner or entry.pw_uid <= 0:
        raise ValueError("owner_home_invalid")
    layout = runpy.run_path(str(helper_path()))
    for directory in (ROOT, ROOT / "admin", ROOT / "profiles", ROOT / "backups"):
        layout["_root_node"](directory, directory=True)
    prepared = layout["_json"](ROOT / "admin/prepared-profiles.json")
    release_id = prepared["profiles"][owner]
    if not layout["RELEASE_RE"].fullmatch(release_id):
        raise ValueError("invalid_release_id")
    release = ROOT / "releases" / release_id
    if layout["_json"](release / "runtime.json").get("state") != "ready":
        raise ValueError("release_not_ready")
    code = release / "hermes-agent"
    layout["validate_program_tree"](code)
    layout["validate_program_tree"](release / "venv")
    original = home / "hermes-agent"
    if original.is_symlink() or not original.is_dir():
        raise ValueError("profile_is_not_a_legacy_runtime")
    for private_directory in (home.parent, home, original):
        layout["_node"](private_directory, directory=True, uid=entry.pw_uid)
    registration = ROOT / "profiles" / f"{owner}.json"
    if registration.exists() or registration.is_symlink():
        raise ValueError("profile_already_registered")
    state = service_state(owner)
    if state.get("User") != owner or state.get("WorkingDirectory") != str(home) or state.get("ActiveState") != "active":
        raise ValueError("unexpected_service_identity_or_state")
    leases = json.loads((home / "runtime/active_sessions.json").read_text()).get("entries")
    gateway = json.loads((home / "gateway_state.json").read_text())
    if leases != [] or gateway.get("active_agents", 0) != 0 or str(gateway.get("pid")) != state.get("MainPID"):
        raise ValueError("profile_is_busy")
    builder = runpy.run_path(str(Path(__file__).with_name("prepare_runtime.py")))
    expected = json.loads((release / "code-manifest.json").read_text())
    if builder["source_manifest"](original) != expected:
        raise ValueError("source_code_changed_since_preparation")
    unit = UNIT_ROOT / f"{owner}-hermes.service"
    layout["_root_node"](unit, directory=False)
    before_unit = unit.read_bytes()
    temporary = PRIVATE_TMP_ROOT / str(entry.pw_uid)
    after_unit = edit_unit(before_unit.decode(), temporary).encode()
    tmpfile = TMPFILES_ROOT / f"vektor-shared-{owner}.conf"
    if tmpfile.exists() or tmpfile.is_symlink():
        raise ValueError("tmpfiles_configuration_already_exists")
    result = {"owner": owner, "release_id": release_id, "private_tmp": str(temporary), "preflight": "passed"}
    if not apply:
        return result
    backup = Path(tempfile.mkdtemp(prefix=f"{owner}-", dir=ROOT / "backups"))
    atomic_write(backup / "unit-before.service", before_unit)
    manifest = {**result, "state": "prepared", "unit_sha256": hashlib.sha256(before_unit).hexdigest(),
                "old_runtime": str(backup / "hermes-agent"), "uv_links": []}
    atomic_write(backup / "migration.json", json.dumps(manifest, indent=2).encode())
    protected = protected_hashes(home)
    moved = False
    linked = False
    registration_written = False
    unit_written = False
    temporary_config = (f"d {PRIVATE_TMP_ROOT} 0755 root root -\n"
                        f"d {temporary} 0700 {owner} {owner} -\n")
    tmpfile_written = False
    try:
        command("systemctl", "stop", f"{owner}-hermes.service")
        if service_state(owner).get("MainPID") != "0" or unit.read_bytes() != before_unit:
            raise ValueError("service_changed_during_switch")
        if builder["source_manifest"](original) != expected:
            raise ValueError("source_code_changed_before_switch")
        if not temporary.parent.exists():
            temporary.parent.mkdir(mode=0o755)
        layout["_root_node"](temporary.parent, directory=True)
        if temporary.exists() or temporary.is_symlink():
            info = temporary.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != entry.pw_uid or info.st_mode & 0o077:
                raise ValueError("private_tmp_directory_untrusted")
        else:
            temporary.mkdir(mode=0o700)
            os.chown(temporary, entry.pw_uid, entry.pw_gid)
        atomic_write(tmpfile, temporary_config.encode(), 0o644)
        tmpfile_written = True
        os.rename(original, backup / "hermes-agent")
        moved = True
        registration_data = {"schema_version": 1, "owner": owner, "release_id": release_id}
        atomic_write(registration, json.dumps(registration_data).encode(), 0o644)
        registration_written = True
        original.symlink_to(code)
        linked = True
        os.lchown(original, entry.pw_uid, entry.pw_gid)
        layout["load_shared_runtime"](owner, home, entry.pw_uid)
        for name in ("uv", "uvx"):
            old = home / "bin" / name
            shared = ROOT / "tools/uv-0.12.3" / name
            if old.is_file() and not old.is_symlink() and shared.is_file():
                if digest(old) != digest(shared):
                    raise ValueError("uv_tool_version_changed")
                os.rename(old, backup / name)
                manifest["uv_links"].append(name)
                old.symlink_to(shared)
                os.lchown(old, entry.pw_uid, entry.pw_gid)
        atomic_write(unit, after_unit, 0o644)
        unit_written = True
        command("systemctl", "daemon-reload")
        command("systemctl", "start", f"{owner}-hermes.service")
        live = wait_ready(owner, home)
        if any(digest(path) != checksum for path, checksum in protected.items()):
            raise ValueError("protected_profile_configuration_changed")
        manifest.update(state="active", pid=live["MainPID"], protected_files_unchanged=True)
        atomic_write(backup / "migration.json", json.dumps(manifest, indent=2).encode())
        return {**result, "state": "active", "pid": live["MainPID"], "backup": str(backup), "protected_files_unchanged": True}
    except Exception:
        command("systemctl", "stop", f"{owner}-hermes.service")
        expected_unit = after_unit if unit_written else before_unit
        if unit.read_bytes() != expected_unit:
            raise RuntimeError("rollback_requires_manual_review_unit_changed")
        if linked:
            if not original.is_symlink() or original.resolve() != code:
                raise RuntimeError("rollback_requires_manual_review_profile_link_changed")
            original.unlink()
        if moved:
            os.rename(backup / "hermes-agent", original)
        for name in manifest["uv_links"]:
            alias = home / "bin" / name
            if alias.exists() or alias.is_symlink():
                if not alias.is_symlink() or alias.resolve() != ROOT / "tools/uv-0.12.3" / name:
                    raise RuntimeError("rollback_requires_manual_review_tool_link_changed")
                alias.unlink()
            os.rename(backup / name, alias)
        if registration_written:
            if not registration.is_file() or json.loads(registration.read_text()) != registration_data:
                raise RuntimeError("rollback_requires_manual_review_registration_changed")
            registration.unlink()
        atomic_write(unit, before_unit, 0o644)
        if tmpfile_written:
            if not tmpfile.is_file() or tmpfile.read_text() != temporary_config:
                raise RuntimeError("rollback_requires_manual_review_tmpfiles_changed")
            tmpfile.unlink()
        command("systemctl", "daemon-reload")
        command("systemctl", "start", f"{owner}-hermes.service")
        manifest["state"] = "rolled_back"
        atomic_write(backup / "migration.json", json.dumps(manifest, indent=2).encode())
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise ValueError("administrator_required")
    fd = os.open("/run/lock/vektor-shared-runtime.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if info.st_uid != 0 or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077:
            raise ValueError("migration_lock_untrusted")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        print(json.dumps(switch(args.owner, apply=args.apply), ensure_ascii=False), flush=True)
    finally:
        os.close(fd)


if __name__ == "__main__":
    main()
