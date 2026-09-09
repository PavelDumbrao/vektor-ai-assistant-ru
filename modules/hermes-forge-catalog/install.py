#!/usr/bin/env python3
"""Install a validated immutable Hermes Forge catalog release."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from catalog import DEFAULT_ROOT, catalog_digest, catalog_json, load_catalog

TARGET_ROOT = Path("/opt/proai-hermes-forge-catalog")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _secure_tree(root: Path) -> None:
    for path in [root, *root.rglob("*")]:
        if path.is_symlink():
            continue
        if path.is_dir():
            path.chmod(0o755)
        elif path.is_file():
            path.chmod(0o644)
        if os.geteuid() == 0:
            os.chown(path, 0, 0)


def install(source_root: Path = DEFAULT_ROOT, target_root: Path = TARGET_ROOT) -> dict[str, str]:
    source_root = source_root.resolve()
    load_catalog(source_root)
    digest = catalog_digest(source_root)
    target_root.mkdir(parents=True, exist_ok=True, mode=0o755)
    if target_root.is_symlink():
        raise RuntimeError("catalog_target_symlink")
    releases = target_root / "releases"
    releases.mkdir(parents=True, exist_ok=True, mode=0o755)
    release = releases / digest

    if not release.exists():
        staging = Path(tempfile.mkdtemp(prefix=".catalog-", dir=releases))
        try:
            shutil.copytree(source_root, staging / "source")
            payload = catalog_json(source_root)
            (staging / "catalog.json").write_text(payload + "\n", encoding="utf-8")
            (staging / "catalog.sha256").write_text(digest + "\n", encoding="utf-8")
            (staging / "release.json").write_text(json.dumps({
                "schema": "hermes.catalog.release/v1",
                "sha256": digest,
                "created_at": _utc_now(),
            }, sort_keys=True) + "\n", encoding="utf-8")
            _secure_tree(staging)
            os.replace(staging, release)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    marker = release / "catalog.sha256"
    if not marker.is_file() or marker.read_text(encoding="utf-8").strip() != digest:
        raise RuntimeError("catalog_release_integrity_failed")

    current = target_root / "current"
    previous = ""
    if current.is_symlink():
        try:
            previous = current.resolve(strict=True).name
        except OSError:
            previous = "broken"
    elif current.exists():
        raise RuntimeError("catalog_current_not_symlink")

    temp_link = target_root / f".current-{os.getpid()}"
    try:
        temp_link.symlink_to(Path("releases") / digest)
        os.replace(temp_link, current)
    finally:
        if temp_link.exists() or temp_link.is_symlink():
            temp_link.unlink()
    if os.geteuid() == 0:
        os.chown(target_root, 0, 0)
        os.chown(releases, 0, 0)
    target_root.chmod(0o755)
    releases.chmod(0o755)
    return {"sha256": digest, "release": str(release), "previous": previous}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--target-root", type=Path, default=TARGET_ROOT)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError("root_required")
    result = install(args.source_root, args.target_root)
    print(json.dumps({"installed": True, **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
