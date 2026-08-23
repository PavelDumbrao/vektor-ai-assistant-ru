#!/usr/bin/env python3
"""Run one bounded, exact-scope PostgreSQL retention pass as the profile owner.

This command only calls the canonical archive retention operation. It never
inspects, acknowledges, unlinks, or otherwise mutates the Hermes receive spool.
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path
from typing import Any, Callable

from passive_secretary_plugin.archive import PostgresArchive
from passive_secretary_plugin.settings import Settings, load_settings


MAX_SETTINGS_BYTES = 65_536


class RetentionCommandError(RuntimeError):
    """Stable public error code with no secret or path content."""


ArchiveFactory = Callable[[Settings], Any]


def _validated_settings_path(raw_path: str | Path) -> Path:
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        raise RetentionCommandError("settings_path_must_be_absolute")
    try:
        entry_stat = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise RetentionCommandError("settings_file_unavailable") from exc
    if stat.S_ISLNK(entry_stat.st_mode) or resolved != candidate:
        raise RetentionCommandError("settings_path_not_canonical")
    if not stat.S_ISREG(entry_stat.st_mode):
        raise RetentionCommandError("settings_file_not_regular")
    if entry_stat.st_uid != os.geteuid():
        raise RetentionCommandError("settings_owner_mismatch")
    if stat.S_IMODE(entry_stat.st_mode) & 0o077:
        raise RetentionCommandError("settings_permissions_too_open")
    if entry_stat.st_size <= 0 or entry_stat.st_size > MAX_SETTINGS_BYTES:
        raise RetentionCommandError("settings_file_size_invalid")
    return resolved


def run_retention(
    settings_path: str | Path,
    *,
    archive_factory: ArchiveFactory | None = None,
) -> int:
    path = _validated_settings_path(settings_path)
    try:
        settings = load_settings(path)
    except (OSError, KeyError, TypeError, ValueError, RuntimeError) as exc:
        raise RetentionCommandError("settings_invalid") from exc
    if not settings.postgres_configured():
        raise RetentionCommandError("postgres_not_configured")
    try:
        factory = archive_factory or PostgresArchive
        deleted = factory(settings).enforce_retention()
    except Exception as exc:
        # Database drivers may include a DSN in exception text. Never expose it.
        raise RetentionCommandError("retention_failed") from exc
    if isinstance(deleted, bool) or not isinstance(deleted, int) or deleted < 0:
        raise RetentionCommandError("retention_result_invalid")
    return deleted


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--settings",
        required=True,
        help="Absolute canonical path to the owner-only plugin settings.json.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        deleted = run_retention(args.settings)
    except RetentionCommandError as exc:
        print(f"error={exc}", file=sys.stderr)
        return 1
    print("retention_ok=true")
    print(f"deleted_messages={deleted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
