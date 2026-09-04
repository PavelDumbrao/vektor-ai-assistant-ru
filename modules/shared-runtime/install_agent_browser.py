#!/usr/bin/env python3
"""Install one pinned native agent-browser artifact without npm lifecycle scripts."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import io
import json
import os
import platform
import re
import shutil
import tarfile
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath


ROOT = Path("/opt/vektor/tools")
ALIAS = Path("/usr/local/bin/agent-browser")
NATIVE = "bin/agent-browser-linux-x64"


def fetch(url: str, limit: int) -> bytes:
    if urllib.parse.urlsplit(url).scheme != "https" or urllib.parse.urlsplit(url).hostname != "registry.npmjs.org":
        raise ValueError("untrusted_registry_url")
    with urllib.request.urlopen(url, timeout=30) as response:
        if urllib.parse.urlsplit(response.geturl()).hostname != "registry.npmjs.org":
            raise ValueError("untrusted_registry_redirect")
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("registry_response_too_large")
    return data


def verify_integrity(data: bytes, expected: str) -> None:
    actual = "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()
    if not hmac.compare_digest(actual, expected):
        raise ValueError("npm_integrity_mismatch")


def extract_native_package(data: bytes, destination: Path) -> None:
    total = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive:
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts or not name.parts or name.parts[0] != "package":
                raise ValueError("archive_path_escape")
            relative = PurePosixPath(*name.parts[1:])
            if not relative.parts:
                continue
            if not member.isfile() and not member.isdir():
                raise ValueError("archive_special_file_rejected")
            if str(relative).startswith("bin/agent-browser-") and str(relative) != NATIVE:
                continue
            target = destination.joinpath(*relative.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            total += member.size
            if total > 40_000_000:
                raise ValueError("selected_artifact_too_large")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise ValueError("archive_duplicate_path")
            with archive.extractfile(member) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
            target.chmod(0o755 if str(relative) == NATIVE else 0o644)
    package = json.loads((destination / "package.json").read_text())
    if package.get("dependencies") or package.get("optionalDependencies"):
        raise ValueError("package_is_not_self_contained")
    if not (destination / NATIVE).is_file() or not (destination / "LICENSE").is_file():
        raise ValueError("native_binary_or_license_missing")
    for folder, directories, _files in os.walk(destination):
        Path(folder).chmod(0o755)
        for name in directories:
            (Path(folder) / name).chmod(0o755)


def install(version: str, expected_integrity: str) -> dict:
    if os.geteuid() != 0 or platform.system() != "Linux" or platform.machine() != "x86_64":
        raise ValueError("linux_x64_administrator_required")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("explicit_version_required")
    if not ROOT.is_dir() or ROOT.is_symlink() or ROOT.stat().st_uid != 0 or ROOT.stat().st_mode & 0o022:
        raise ValueError("managed_tools_directory_untrusted")
    metadata = json.loads(fetch(f"https://registry.npmjs.org/agent-browser/{version}", 4_000_000))
    if metadata.get("version") != version or metadata["dist"]["integrity"] != expected_integrity:
        raise ValueError("pinned_registry_metadata_changed")
    data = fetch(metadata["dist"]["tarball"], 100_000_000)
    verify_integrity(data, expected_integrity)
    destination = ROOT / f"agent-browser-{version}"
    binary = destination / NATIVE
    if destination.exists():
        raise ValueError("version_already_exists_review_instead_of_overwriting")
    if ALIAS.exists() or ALIAS.is_symlink():
        raise ValueError("global_agent_browser_already_exists")
    staging = Path(tempfile.mkdtemp(prefix=".agent-browser-", dir=ROOT))
    try:
        extract_native_package(data, staging)
        manifest = {"version": version, "npm_integrity": expected_integrity,
                    "native_sha256": hashlib.sha256((staging / NATIVE).read_bytes()).hexdigest(),
                    "license": metadata.get("license"), "lifecycle_scripts_executed": False}
        (staging / "managed-artifact.json").write_text(json.dumps(manifest, indent=2) + "\n")
        os.rename(staging, destination)
        ALIAS.symlink_to(binary)
        return {"binary": str(binary), "alias": str(ALIAS), **manifest}
    finally:
        if staging.exists():
            shutil.rmtree(staging)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--expected-integrity", required=True)
    args = parser.parse_args()
    print(json.dumps(install(args.version, args.expected_integrity)))
