#!/usr/bin/env python3
"""Versioned, rollback-safe live upgrade for the unprivileged Forge API only."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SOURCE = Path(__file__).resolve().parent
KITCHEN_SOURCE = SOURCE.parent / "hermes-forge-kitchen" / "kitchen.py"
TARGET = Path("/opt/proai-hermes-forge-api")
RELEASES = TARGET / "releases"
CURRENT = TARGET / "current"
SYSTEMD = Path("/etc/systemd/system")
DROPIN_DIR = SYSTEMD / "proai-hermes-forge-api.service.d"
DROPIN = DROPIN_DIR / "30-versioned-release.conf"
SERVICE = "proai-hermes-forge-api.service"
BASE_URL = "http://127.0.0.1:8650"


def _sources() -> list[tuple[str, Path]]:
    items: list[tuple[str, Path]] = [
        ("api.py", SOURCE / "api.py"),
        ("kitchen.py", KITCHEN_SOURCE),
    ]
    for path in sorted((SOURCE / "static").iterdir()):
        if path.is_file() and not path.is_symlink():
            items.append((f"static/{path.name}", path))
    return items


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bundle_manifest() -> dict[str, str]:
    result: dict[str, str] = {}
    for name, path in _sources():
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("source_invalid")
        result[name] = _sha256_file(path)
    return result


def bundle_digest(manifest: dict[str, str] | None = None) -> str:
    data = manifest if manifest is not None else bundle_manifest()
    raw = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _atomic_write(path: Path, content: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        if os.geteuid() == 0:
            os.chown(tmp, 0, 0)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _run(*args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError(Path(args[0]).name + "_failed")
    return result


def _release_metadata(digest: str, manifest: dict[str, str]) -> dict[str, Any]:
    return {
        "schema": "hermes.forge-api-release/v1",
        "sha256": digest,
        "files": manifest,
    }


def _verify_release(path: Path, digest: str, manifest: dict[str, str]) -> None:
    marker = path / "release.json"
    if path.is_symlink() or not path.is_dir() or marker.is_symlink() or not marker.is_file():
        raise RuntimeError("release_invalid")
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("release_invalid") from None
    if payload != _release_metadata(digest, manifest):
        raise RuntimeError("release_metadata_mismatch")
    expected_files = set(manifest) | {"release.json"}
    actual_files = {
        str(item.relative_to(path))
        for item in path.rglob("*")
        if item.is_file() and not item.is_symlink()
    }
    if actual_files != expected_files:
        raise RuntimeError("release_file_set_mismatch")
    for name, expected in manifest.items():
        candidate = path / name
        if candidate.is_symlink() or not candidate.is_file() or _sha256_file(candidate) != expected:
            raise RuntimeError("release_file_mismatch")


def prepare_release() -> tuple[str, Path]:
    manifest = bundle_manifest()
    digest = bundle_digest(manifest)
    release = RELEASES / digest
    if release.exists():
        _verify_release(release, digest, manifest)
        return digest, release
    RELEASES.mkdir(parents=True, exist_ok=True, mode=0o755)
    staging = Path(tempfile.mkdtemp(prefix=f".{digest}.staging-", dir=RELEASES))
    try:
        for name, source in _sources():
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            shutil.copyfile(source, target)
            os.chmod(target, 0o644)
            if os.geteuid() == 0:
                os.chown(target, 0, 0)
        metadata = json.dumps(_release_metadata(digest, manifest), sort_keys=True, indent=2) + "\n"
        _atomic_write(staging / "release.json", metadata)
        for directory in [staging, *(p for p in staging.rglob("*") if p.is_dir())]:
            os.chmod(directory, 0o755)
            if os.geteuid() == 0:
                os.chown(directory, 0, 0)
        os.replace(staging, release)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    _verify_release(release, digest, manifest)
    return digest, release


def _dropin_text() -> str:
    return """[Service]
WorkingDirectory=/opt/proai-hermes-forge-api/current
ExecStart=
ExecStart=/usr/bin/python3 /opt/proai-hermes-forge-api/current/api.py
"""


def _current_target() -> str | None:
    if not CURRENT.exists() and not CURRENT.is_symlink():
        return None
    if not CURRENT.is_symlink():
        raise RuntimeError("current_not_symlink")
    return os.readlink(CURRENT)


def _switch_current(release: Path) -> None:
    TARGET.mkdir(parents=True, exist_ok=True, mode=0o755)
    temp = TARGET / f".current.{time.time_ns()}"
    try:
        os.symlink(str(release), temp)
        os.replace(temp, CURRENT)
    finally:
        if temp.is_symlink():
            temp.unlink()


def _restore_current(previous: str | None) -> None:
    if previous is None:
        if CURRENT.is_symlink():
            CURRENT.unlink()
        return
    temp = TARGET / f".current.rollback.{time.time_ns()}"
    try:
        os.symlink(previous, temp)
        os.replace(temp, CURRENT)
    finally:
        if temp.is_symlink():
            temp.unlink()


def _read_previous_dropin() -> bytes | None:
    if not DROPIN.exists():
        return None
    if DROPIN.is_symlink() or not DROPIN.is_file():
        raise RuntimeError("dropin_invalid")
    return DROPIN.read_bytes()


def _restore_dropin(previous: bytes | None) -> None:
    if previous is None:
        if DROPIN.exists() or DROPIN.is_symlink():
            DROPIN.unlink()
        return
    DROPIN_DIR.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=".30-versioned-release.", dir=DROPIN_DIR)
    tmp = Path(raw)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(previous)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o644)
        if os.geteuid() == 0:
            os.chown(tmp, 0, 0)
        os.replace(tmp, DROPIN)
    finally:
        if tmp.exists():
            tmp.unlink()


def _install_dropin() -> None:
    _atomic_write(DROPIN, _dropin_text(), 0o644)


def _request(path: str, *, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    data = None
    headers: dict[str, str] = {}
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(BASE_URL + path, data=data, headers=headers, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=4) as response:
            raw = response.read(1024 * 1024)
            return int(response.status), json.loads(raw.decode("utf-8")) if raw else None
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
        return 0, None


def _api_result(status: int, payload: Any) -> dict[str, Any] | None:
    if status != 200 or not isinstance(payload, dict) or payload.get("ok") is not True:
        return None
    result = payload.get("result")
    return result if isinstance(result, dict) else None


def basic_probes_ok() -> bool:
    health = _api_result(*_request("/healthz"))
    if health is None or health.get("ok") is not True:
        return False
    catalog = _api_result(*_request("/v1/catalog"))
    return catalog is not None and catalog.get("schema") == "hermes.catalog.public/v1"


def probes_ok() -> bool:
    if not basic_probes_ok():
        return False
    preview_status, preview = _request(
        "/v1/kitchen/preview",
        body={"agent_id": "personal-hermes", "optional_capabilities": []},
    )
    preview_result = _api_result(preview_status, preview)
    return preview_result is not None and preview_result.get("schema") == "hermes.kitchen-preview/v1"


def _wait_for(predicate: Any, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.5)
    return False


def _wait_for_probes(timeout: float = 20.0) -> bool:
    return _wait_for(probes_ok, timeout)


def _wait_for_basic_probes(timeout: float = 20.0) -> bool:
    return _wait_for(basic_probes_ok, timeout)


def plan_upgrade() -> dict[str, Any]:
    manifest = bundle_manifest()
    digest = bundle_digest(manifest)
    return {
        "release_sha256": digest,
        "release_path": str(RELEASES / digest),
        "current_target": _current_target(),
        "service": SERVICE,
        "apply": False,
    }


@contextmanager
def _upgrade_lock():
    TARGET.mkdir(parents=True, exist_ok=True, mode=0o755)
    path = TARGET / ".upgrade.lock"
    with path.open("a+", encoding="utf-8") as handle:
        os.chmod(path, 0o600)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("upgrade_busy") from None
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def apply_upgrade() -> dict[str, Any]:
    if os.geteuid() != 0:
        raise RuntimeError("root_required")
    with _upgrade_lock():
        return _apply_upgrade_locked()


def _apply_upgrade_locked() -> dict[str, Any]:
    digest, release = prepare_release()
    previous_target = _current_target()
    previous_dropin = _read_previous_dropin()
    switched = False
    try:
        _switch_current(release)
        switched = True
        _install_dropin()
        _run("/usr/bin/systemctl", "daemon-reload")
        _run("/usr/bin/systemctl", "restart", SERVICE)
        if not _wait_for_probes():
            raise RuntimeError("activation_probe_failed")
    except (RuntimeError, OSError):
        if switched:
            _restore_current(previous_target)
            _restore_dropin(previous_dropin)
            try:
                _run("/usr/bin/systemctl", "daemon-reload")
                _run("/usr/bin/systemctl", "restart", SERVICE)
                if not _wait_for_basic_probes():
                    raise RuntimeError("rollback_probe_failed")
            except (RuntimeError, OSError) as rollback_exc:
                raise RuntimeError("activation_failed_rollback_failed") from rollback_exc
        raise
    return {
        "release_sha256": digest,
        "release_path": str(release),
        "previous_target": previous_target,
        "current_target": _current_target(),
        "service": SERVICE,
        "apply": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="perform the bounded API switch")
    args = parser.parse_args()
    result = apply_upgrade() if args.apply else plan_upgrade()
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
