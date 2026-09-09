"""Secure disk-backed ingress for Telegram Bot API local mode."""
from __future__ import annotations

import hashlib
import hmac
import os
import stat
from pathlib import Path, PurePosixPath

DEFAULT_SERVER_ROOT = PurePosixPath("/var/lib/telegram-bot-api")
_COPY_CHUNK = 1024 * 1024


class LocalTelegramPathError(ValueError):
    """A local Bot API path escaped or violated the tenant boundary."""


def tenant_relative_path(
    file_path: str,
    *,
    expected_tenant_sha256: str,
    server_root: str = str(DEFAULT_SERVER_ROOT),
) -> PurePosixPath:
    """Validate tenant identity and return its private relative file path."""
    root = PurePosixPath(server_root)
    raw = PurePosixPath(file_path)
    if not root.is_absolute() or not raw.is_absolute():
        raise LocalTelegramPathError("Telegram local path must be absolute")
    try:
        below_root = raw.relative_to(root)
    except ValueError as exc:
        raise LocalTelegramPathError("Telegram local path is outside server root") from exc
    parts = below_root.parts
    if len(parts) < 2 or any(part in {"", ".", ".."} for part in parts):
        raise LocalTelegramPathError("Telegram local path has invalid components")
    tenant_component = parts[0]
    observed = hashlib.sha256(tenant_component.encode("utf-8")).hexdigest()
    expected = (expected_tenant_sha256 or "").strip().lower()
    if len(expected) != 64 or not hmac.compare_digest(observed, expected):
        raise LocalTelegramPathError("Telegram local path belongs to another tenant")
    return PurePosixPath(*parts[1:])


def _open_directory_component(parent_fd: int, name: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    return os.open(name, flags, dir_fd=parent_fd)


def copy_from_private_mount(
    mount_root: Path,
    relative_path: PurePosixPath,
    destination: Path,
    *,
    max_bytes: int,
    expected_bytes: int = 0,
) -> int:
    """Copy through dir-fds with O_NOFOLLOW, never buffering the full file."""
    parts = relative_path.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise LocalTelegramPathError("invalid private relative path")
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None:
        raise RuntimeError("secure local Telegram ingress requires O_NOFOLLOW/O_DIRECTORY")

    root_flags = os.O_RDONLY | directory | nofollow | getattr(os, "O_CLOEXEC", 0)
    current_fd = os.open(mount_root, root_flags)
    source_fd = -1
    target_fd = -1
    try:
        for component in parts[:-1]:
            next_fd = _open_directory_component(current_fd, component)
            os.close(current_fd)
            current_fd = next_fd
        source_flags = os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0)
        source_fd = os.open(parts[-1], source_flags, dir_fd=current_fd)
        source_info = os.fstat(source_fd)
        if not stat.S_ISREG(source_info.st_mode) or source_info.st_size <= 0:
            raise LocalTelegramPathError("Telegram local source is not a regular file")
        if max_bytes and source_info.st_size > max_bytes:
            raise ValueError("Telegram local file exceeds configured size limit")
        if expected_bytes and source_info.st_size != expected_bytes:
            raise LocalTelegramPathError("Telegram local file size changed before copy")

        dest_info = os.lstat(destination)
        if stat.S_ISLNK(dest_info.st_mode) or not stat.S_ISREG(dest_info.st_mode):
            raise LocalTelegramPathError("Telegram staging destination is unsafe")
        target_flags = os.O_WRONLY | os.O_TRUNC | nofollow | getattr(os, "O_CLOEXEC", 0)
        target_fd = os.open(destination, target_flags)
        copied = 0
        while True:
            chunk = os.read(source_fd, _COPY_CHUNK)
            if not chunk:
                break
            copied += len(chunk)
            if max_bytes and copied > max_bytes:
                raise ValueError("Telegram local file exceeded configured size limit while copying")
            view = memoryview(chunk)
            while view:
                written = os.write(target_fd, view)
                view = view[written:]
        os.fsync(target_fd)
        if copied != source_info.st_size:
            raise LocalTelegramPathError("Telegram local file changed during copy")
        if expected_bytes and copied != expected_bytes:
            raise LocalTelegramPathError("Telegram local file size mismatch")
        return copied
    finally:
        for fd in (source_fd, target_fd, current_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
