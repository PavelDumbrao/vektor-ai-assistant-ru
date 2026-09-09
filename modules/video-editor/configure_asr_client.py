#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import pwd
import re
import secrets
import stat
import tempfile
from pathlib import Path

RUNTIME = Path("/opt/vektor/video-editor")
PROFILE_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
DIRECTORY = getattr(os, "O_DIRECTORY", 0)


def _open_dir(path: Path) -> int:
    try:
        fd = os.open(str(path), os.O_RDONLY | DIRECTORY | NOFOLLOW)
    except OSError as exc:
        raise RuntimeError("unsafe_directory") from exc
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        os.close(fd)
        raise RuntimeError("unsafe_directory")
    return fd

def _read_profile_token(dir_fd: int, uid: int) -> str:
    try:
        fd = os.open("asr_token", os.O_RDONLY | NOFOLLOW, dir_fd=dir_fd)
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise RuntimeError("profile_token_unsafe") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != uid:
            raise RuntimeError("profile_token_unsafe")
        if (info.st_mode & 0o077) or info.st_size > 512:
            raise RuntimeError("profile_token_permissions_unsafe")
        value = os.read(fd, 513).decode("utf-8").strip()
    finally:
        os.close(fd)
    if 32 <= len(value) <= 256 and not any(ch.isspace() for ch in value):
        return value
    return ""


def _write_profile_token(dir_fd: int, uid: int, gid: int, token: str) -> None:
    temp_name = f".asr_token.{secrets.token_hex(12)}"
    fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, 0o600, dir_fd=dir_fd)
    try:
        os.fchown(fd, uid, gid)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", closefd=False) as handle:
            handle.write(token + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(fd)
    try:
        os.replace(temp_name, "asr_token", src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    finally:
        try:
            os.unlink(temp_name, dir_fd=dir_fd)
        except FileNotFoundError:
            pass


def _ensure_root_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if path.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid != 0:
        raise RuntimeError("shared_token_dir_unsafe")
    path.chmod(0o700)


def _write_shared_token(path: Path, token: str) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=".asr-token-", dir=path.parent)
    temp = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        os.fchown(fd, 0, 0)
        with os.fdopen(fd, "w", encoding="utf-8", closefd=False) as handle:
            handle.write(token + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.close(fd)
        fd = -1
        os.replace(temp, path)
        path.chmod(0o600)
        os.chown(path, 0, 0)
    finally:
        if fd >= 0:
            os.close(fd)
        temp.unlink(missing_ok=True)


def configure(owner: str) -> None:
    if os.geteuid() != 0:
        raise RuntimeError("root_required")
    if not PROFILE_RE.fullmatch(owner):
        raise RuntimeError("invalid_owner")
    entry = pwd.getpwnam(owner)
    if entry.pw_uid <= 0 or Path(entry.pw_dir).name != owner:
        raise RuntimeError("invalid_owner")

    hermes = Path(entry.pw_dir) / ".hermes"
    hermes_fd = _open_dir(hermes)
    try:
        hermes_info = os.fstat(hermes_fd)
        if hermes_info.st_uid != entry.pw_uid:
            raise RuntimeError("profile_home_unsafe")
        try:
            os.mkdir("video_editor", mode=0o700, dir_fd=hermes_fd)
            os.chown("video_editor", entry.pw_uid, entry.pw_gid, dir_fd=hermes_fd, follow_symlinks=False)
        except FileExistsError:
            pass
        local_fd = os.open("video_editor", os.O_RDONLY | DIRECTORY | NOFOLLOW, dir_fd=hermes_fd)
        try:
            local_info = os.fstat(local_fd)
            if not stat.S_ISDIR(local_info.st_mode) or local_info.st_uid != entry.pw_uid:
                raise RuntimeError("profile_video_editor_dir_unsafe")
            os.fchmod(local_fd, 0o700)
            token = _read_profile_token(local_fd, entry.pw_uid)
            if not token:
                token = secrets.token_urlsafe(32)
                _write_profile_token(local_fd, entry.pw_uid, entry.pw_gid, token)
        finally:
            os.close(local_fd)
    finally:
        os.close(hermes_fd)

    clients = RUNTIME / "private" / "clients"
    _ensure_root_dir(clients)
    _write_shared_token(clients / f"{owner}.token", token)
    print("asr_client_configured=true")
    print(f"owner={owner}")
    print("token_printed=false")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner", required=True)
    args = parser.parse_args()
    try:
        configure(args.owner)
    except Exception as exc:
        print(f"error={type(exc).__name__}:{exc}", file=__import__("sys").stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
