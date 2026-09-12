#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import os
import pwd
import re
import shutil
import stat
import subprocess
from pathlib import Path

VERSION = "0.1.0"
OWNER_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
ROOT = Path("/opt/vektor/living-memory")
FILES = ["__init__.py", "core.py", "provider.py", "worker.py", "SYSTEM_PROMPT.md"]


def _sha(source: Path) -> str:
    h = hashlib.sha256()
    for name in sorted(FILES):
        h.update(name.encode()); h.update(b"\0"); h.update((source / name).read_bytes()); h.update(b"\0")
    return h.hexdigest()


def _safe_root_dir(path: Path, mode: int = 0o755) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=mode)
    info = path.lstat()
    if path.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid != 0:
        raise RuntimeError("unsafe_root_directory")
    path.chmod(mode)


def _install_units(source: Path) -> None:
    for name in ("vektor-living-memory@.service", "vektor-living-memory@.timer"):
        src = source / name
        dst = Path("/etc/systemd/system") / name
        data = src.read_bytes()
        if dst.exists() and dst.read_bytes() == data:
            continue
        tmp = dst.with_name(f".{name}.tmp")
        tmp.write_bytes(data); os.chown(tmp, 0, 0); tmp.chmod(0o644); os.replace(tmp, dst)
    subprocess.run(["systemctl", "daemon-reload"], check=True)


def install(owner: str, enable_timer: bool) -> None:
    if os.geteuid() != 0:
        raise RuntimeError("root_required")
    if not OWNER_RE.fullmatch(owner):
        raise RuntimeError("invalid_owner")
    entry = pwd.getpwnam(owner)
    if entry.pw_uid <= 0 or Path(entry.pw_dir).name != owner:
        raise RuntimeError("invalid_owner")
    home = Path(entry.pw_dir) / ".hermes"
    if not home.is_dir() or home.is_symlink() or home.stat().st_uid != entry.pw_uid:
        raise RuntimeError("profile_home_unsafe")
    py = home / "hermes-agent" / "venv" / "bin" / "python"
    if not py.is_file():
        raise RuntimeError("hermes_python_missing")

    source = Path(__file__).resolve().parent
    for name in FILES:
        p = source / name
        if not p.is_file() or p.is_symlink():
            raise RuntimeError("module_source_incomplete")
    digest = _sha(source)
    release_id = f"v{VERSION}-{digest[:12]}"
    _safe_root_dir(ROOT)
    _safe_root_dir(ROOT / "releases")
    release = ROOT / "releases" / release_id
    if not release.exists():
        release.mkdir(mode=0o755)
        for name in FILES:
            dst = release / name
            shutil.copy2(source / name, dst)
            os.chown(dst, 0, 0); dst.chmod(0o755 if name == "worker.py" else 0o644)
        os.chown(release, 0, 0); release.chmod(0o755)
    _install_units(source)

    profile_root = ROOT / "profiles" / owner
    _safe_root_dir(ROOT / "profiles")
    _safe_root_dir(profile_root)
    current = profile_root / "current"
    temp = profile_root / ".current.tmp"
    temp.unlink(missing_ok=True)
    temp.symlink_to(release)
    os.replace(temp, current)

    lm = home / "living_memory"
    lm.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chown(lm, entry.pw_uid, entry.pw_gid); lm.chmod(0o700)
    memories = home / "memories"
    memories.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chown(memories, entry.pw_uid, entry.pw_gid); memories.chmod(0o700)

    if enable_timer:
        subprocess.run(["systemctl", "enable", "--now", f"vektor-living-memory@{owner}.timer"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("installed=true")
    print(f"owner={owner}")
    print(f"version={VERSION}")
    print(f"release={release_id}")
    print(f"timer_enabled={str(enable_timer).lower()}")
    print("secrets_printed=false")


def main() -> int:
    ap = argparse.ArgumentParser(); ap.add_argument("--owner", required=True); ap.add_argument("--enable-timer", action="store_true")
    args = ap.parse_args()
    try:
        install(args.owner, args.enable_timer)
    except Exception as exc:
        print(f"error={type(exc).__name__}:{exc}", file=__import__("sys").stderr); return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
