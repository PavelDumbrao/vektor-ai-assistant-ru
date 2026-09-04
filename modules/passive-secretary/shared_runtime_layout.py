"""Validate an explicitly registered, administrator-owned shared Hermes runtime.

Only program code is shared. HERMES_HOME, credentials, plugin settings and
databases remain owned by the individual Linux user. This module never writes.
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any


RUNTIME_ROOT = Path("/opt/vektor")
HOME_ROOT = Path("/home")
TRUSTED_UID = 0
OWNER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
RELEASE_RE = re.compile(r"^hermes-[a-zA-Z0-9_.-]{1,80}$")


class SharedRuntimeError(RuntimeError):
    """Public-safe validation code without configuration or secret values."""


def _node(path: Path, *, directory: bool, uid: int = 0) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise SharedRuntimeError("shared_runtime_path_missing") from exc
    correct_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (
        not correct_type
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != uid
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise SharedRuntimeError("shared_runtime_path_untrusted")
    return info


def _root_node(path: Path, *, directory: bool) -> os.stat_result:
    return _node(path, directory=directory, uid=TRUSTED_UID)


def _json(path: Path) -> dict[str, Any]:
    info = _root_node(path, directory=False)
    if info.st_nlink != 1 or info.st_size > 16_384:
        raise SharedRuntimeError("shared_runtime_manifest_untrusted")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SharedRuntimeError("shared_runtime_manifest_invalid") from exc
    if not isinstance(value, dict):
        raise SharedRuntimeError("shared_runtime_manifest_invalid")
    return value


def _trusted_target(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
        relative = resolved.relative_to(RUNTIME_ROOT)
    except (OSError, ValueError, RuntimeError) as exc:
        raise SharedRuntimeError("shared_runtime_link_escape") from exc
    current = RUNTIME_ROOT
    _root_node(current, directory=True)
    for part in relative.parts:
        current /= part
        _root_node(current, directory=current != resolved or resolved.is_dir())
    return resolved


def validate_program_tree(path: Path) -> None:
    """Reject user-writable code, special files and links outside the managed root."""
    _root_node(path, directory=True)
    for folder, directories, files in os.walk(path, followlinks=False):
        for name in directories + files:
            candidate = Path(folder) / name
            info = candidate.lstat()
            if stat.S_ISLNK(info.st_mode):
                if info.st_uid != TRUSTED_UID:
                    raise SharedRuntimeError("shared_runtime_link_untrusted")
                _trusted_target(candidate)
            elif stat.S_ISDIR(info.st_mode):
                _root_node(candidate, directory=True)
            elif stat.S_ISREG(info.st_mode):
                _root_node(candidate, directory=False)
            else:
                raise SharedRuntimeError("shared_runtime_special_file")


def load_shared_runtime(owner: str, hermes_home: Path, uid: int) -> dict[str, Path] | None:
    """Return a validated binding, or None for an unchanged legacy private layout."""
    if not isinstance(owner, str) or not OWNER_RE.fullmatch(owner) or uid <= 0:
        raise SharedRuntimeError("shared_runtime_owner_invalid")
    linux_home = HOME_ROOT / owner
    if hermes_home != linux_home / ".hermes":
        raise SharedRuntimeError("shared_runtime_home_mismatch")
    registration = RUNTIME_ROOT / "profiles" / f"{owner}.json"
    agent_dir = hermes_home / "hermes-agent"
    if not registration.exists() and not registration.is_symlink():
        if agent_dir.is_symlink():
            raise SharedRuntimeError("shared_runtime_registration_missing")
        return None

    # Production RUNTIME_ROOT is /opt/vektor: /opt itself must be a real,
    # administrator-owned directory, not a user-controlled redirect.
    _root_node(RUNTIME_ROOT.parent, directory=True)
    _root_node(RUNTIME_ROOT, directory=True)
    _root_node(RUNTIME_ROOT / "profiles", directory=True)
    _root_node(RUNTIME_ROOT / "releases", directory=True)
    _root_node(HOME_ROOT, directory=True)
    _node(linux_home, directory=True, uid=uid)
    _node(hermes_home, directory=True, uid=uid)
    data = _json(registration)
    if (
        set(data) != {"schema_version", "owner", "release_id"}
        or type(data.get("schema_version")) is not int
        or data["schema_version"] != 1
        or data.get("owner") != owner
        or not isinstance(data.get("release_id"), str)
        or not RELEASE_RE.fullmatch(data["release_id"])
    ):
        raise SharedRuntimeError("shared_runtime_registration_invalid")
    release = RUNTIME_ROOT / "releases" / data["release_id"]
    code = release / "hermes-agent"
    venv = release / "venv"
    for directory in (release, code, venv, venv / "bin"):
        _root_node(directory, directory=True)
    ready = _json(release / "runtime.json")
    if ready.get("state") != "ready" or ready.get("release_id") != data["release_id"]:
        raise SharedRuntimeError("shared_runtime_not_ready")
    try:
        link = agent_dir.lstat()
        if not stat.S_ISLNK(link.st_mode) or link.st_uid != uid:
            raise SharedRuntimeError("shared_runtime_profile_link_invalid")
        if agent_dir.resolve(strict=True) != code:
            raise SharedRuntimeError("shared_runtime_profile_link_mismatch")
        if not (code / "venv").is_symlink() or (code / "venv").resolve(strict=True) != venv:
            raise SharedRuntimeError("shared_runtime_venv_link_mismatch")
        executable = _trusted_target(venv / "bin" / "python")
        executable.relative_to(RUNTIME_ROOT / "python")
    except (OSError, ValueError) as exc:
        raise SharedRuntimeError("shared_runtime_python_untrusted") from exc
    info = _root_node(executable, directory=False)
    if not stat.S_IMODE(info.st_mode) & 0o111:
        raise SharedRuntimeError("shared_runtime_python_not_executable")
    validate_program_tree(code)
    validate_program_tree(venv)
    return {
        "release": release,
        "code": code,
        "venv": venv,
        "python_bin": agent_dir / "venv" / "bin" / "python",
        "resolved_python": executable,
    }
