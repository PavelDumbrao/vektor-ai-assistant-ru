#!/usr/bin/env python3
"""After a profile moved to the key broker: take config/.env backups (they still hold platform keys) out of the
client's home into a root-only store, then scan the whole profile for any platform secret value. VPS, root.

  sweep_profile_secrets.py --owner X [--apply] [--allow-skipped]

Moves backups and backup archives containing secrets to /opt/vektor/private-backups/profile-backups/<owner>/
(dir 0700 root, files 0600 root). Then walks ~/.hermes (regular files <= 512 MB, skipping shared release symlinks,
venv/lazy packages and caches) and reports files that still contain a value from /etc/proai-hermes-platform.env.
Exit code 2 if anything is found, exit code 3 if unreadable files were skipped and --allow-skipped not set.
"""
from __future__ import annotations

import argparse
import gzip
import os
import pwd
import re
import shutil
import stat
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import NamedTuple

PLATFORM_ENV = Path("/etc/proai-hermes-platform.env")
STORE = Path("/opt/vektor/private-backups/profile-backups")
SKIP_DIRS = {"hermes-agent", "lazy-packages", "venv", "cache", "__pycache__", "node_modules", ".pytest_cache"}
ACCEPTED = {"TAVILY_API_KEY"}  # known exception: web tool has no upstream base_url override yet

CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB
CHUNK_OVERLAP = 1024  # 1 KB
MAX_FILE_SIZE = 512 * 1024 * 1024  # 512 MB
MAX_ARCHIVE_MEMBER_SIZE = 20 * 1024 * 1024  # 20 MB

SAFE_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOCTTY", 0)
ARCHIVE_EXTENSIONS = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz", ".gz")


class ScanResult(NamedTuple):
    hits: list[tuple[str, str]]
    accepted: list[tuple[str, str]]
    skipped: int


def load_platform_values(env_path: Path = PLATFORM_ENV) -> list[tuple[str, bytes]]:
    if not env_path.is_file():
        return []
    values: list[tuple[str, bytes]] = []
    for line in env_path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if m:
            v = m.group(2).strip().strip("\"'")
            if len(v) >= 12:
                values.append((m.group(1), v.encode("utf-8")))
    return values


def _scan_regular_file(path: Path, values: list[tuple[str, bytes]]) -> tuple[set[str], bool]:
    """Scan regular file in chunks of 8MB with 1KB overlap up to 512MB.
    Returns (set of matched var names, is_skipped)."""
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode):
            return set(), False
        if st.st_size > MAX_FILE_SIZE:
            return set(), True
        fd = os.open(path, SAFE_OPEN_FLAGS)
    except OSError:
        return set(), True

    found: set[str] = set()
    try:
        fst = os.fstat(fd)
        if not stat.S_ISREG(fst.st_mode) or fst.st_size > MAX_FILE_SIZE:
            return set(), True

        carry = b""
        total_read = 0
        while total_read < MAX_FILE_SIZE:
            to_read = min(CHUNK_SIZE, MAX_FILE_SIZE - total_read)
            chunk = os.read(fd, to_read)
            if not chunk:
                break
            total_read += len(chunk)
            data = carry + chunk
            for var, val_bytes in values:
                if var not in found and val_bytes in data:
                    found.add(var)
            if len(found) == len(values):
                break
            carry = data[-CHUNK_OVERLAP:] if len(data) >= CHUNK_OVERLAP else data
        return found, False
    except OSError:
        return set(), True
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _scan_archive(path: Path, values: list[tuple[str, bytes]]) -> tuple[set[str], bool]:
    """Inspect text members in zip/tar/gzip archives (<= 20MB each).
    Returns (set of matched var names, is_skipped)."""
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode):
            return set(), False
    except OSError:
        return set(), True

    name = path.name.lower()
    found: set[str] = set()

    if name.endswith(".zip"):
        try:
            with zipfile.ZipFile(path, "r") as zf:
                for info in zf.infolist():
                    if info.is_dir() or info.file_size > MAX_ARCHIVE_MEMBER_SIZE:
                        continue
                    try:
                        content = zf.read(info)
                        for var, val_bytes in values:
                            if val_bytes in content:
                                found.add(var)
                    except Exception:
                        continue
            return found, False
        except Exception:
            return set(), True

    if name.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")):
        try:
            with tarfile.open(path, "r:*") as tf:
                for member in tf.getmembers():
                    if not member.isfile() or member.size > MAX_ARCHIVE_MEMBER_SIZE:
                        continue
                    try:
                        f = tf.extractfile(member)
                        if f is not None:
                            content = f.read()
                            for var, val_bytes in values:
                                if val_bytes in content:
                                    found.add(var)
                    except Exception:
                        continue
            return found, False
        except Exception:
            return set(), True

    if name.endswith(".gz") and not name.endswith((".tar.gz", ".tgz")):
        try:
            with gzip.open(path, "rb") as gz:
                content = gz.read(MAX_ARCHIVE_MEMBER_SIZE + 1)
                if len(content) <= MAX_ARCHIVE_MEMBER_SIZE:
                    for var, val_bytes in values:
                        if val_bytes in content:
                            found.add(var)
            return found, False
        except Exception:
            return set(), True

    return set(), False


def sweep(
    home: Path,
    store: Path,
    apply: bool = False,
    values: list[tuple[str, bytes]] | None = None,
) -> list[Path]:
    """Find and optionally transfer backups holding secrets into root-only store."""
    if values is None:
        values = load_platform_values()

    candidates: dict[Path, bool] = {}  # path -> True

    # 1. Top-level config.yaml*bak*, .env*bak*, config.yaml.corrupt.*
    if home.is_dir():
        try:
            for p in home.iterdir():
                try:
                    st = os.lstat(p)
                    if not stat.S_ISREG(st.st_mode):
                        continue
                except OSError:
                    continue
                name = p.name
                if (
                    re.match(r"^config\.yaml.*bak.*", name)
                    or re.match(r"^\.env.*bak.*", name)
                    or name.startswith("config.yaml.corrupt.")
                ):
                    candidates[p] = True
        except OSError:
            pass

    # 2. state-snapshots/**/{.env,config.yaml,auth.json}
    snapshots_dir = home / "state-snapshots"
    if snapshots_dir.is_dir():
        for root, dirs, files in os.walk(snapshots_dir, followlinks=False):
            dirs[:] = [d for d in dirs if not (Path(root) / d).is_symlink()]
            for name in files:
                if name in (".env", "config.yaml", "auth.json"):
                    p = Path(root) / name
                    try:
                        st = os.lstat(p)
                        if stat.S_ISREG(st.st_mode):
                            candidates[p] = True
                    except OSError:
                        pass

    # 3. Any file under backups/** or named *bak*/*backup* containing a secret
    # 4. Any archive (*.zip/*.tar*/*.gz) containing a secret
    if home.is_dir():
        for root, dirs, files in os.walk(home, followlinks=False):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not (Path(root) / d).is_symlink()]
            for name in files:
                p = Path(root) / name
                if p in candidates:
                    continue
                try:
                    st = os.lstat(p)
                    if not stat.S_ISREG(st.st_mode):
                        continue
                except OSError:
                    continue

                name_lower = name.lower()
                is_archive = name_lower.endswith(ARCHIVE_EXTENSIONS)
                is_backup = (
                    "backups" in Path(root).parts
                    or "bak" in name_lower
                    or "backup" in name_lower
                )

                if is_archive:
                    found_arch, _ = _scan_archive(p, values)
                    if found_arch:
                        candidates[p] = True
                        continue

                if is_backup and values:
                    found_reg, _ = _scan_regular_file(p, values)
                    if found_reg:
                        candidates[p] = True

    backups = sorted(candidates.keys())

    if apply and backups:
        store.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(store, 0o700)
        except OSError:
            pass

        for p in backups:
            try:
                st = os.lstat(p)
                if not stat.S_ISREG(st.st_mode):
                    continue
            except OSError:
                continue

            target = store / p.relative_to(home)
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(target.parent, 0o700)
            except OSError:
                pass

            shutil.move(str(p), str(target))
            try:
                fd = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                try:
                    try:
                        os.fchown(fd, 0, 0)
                    except PermissionError:
                        pass
                    os.fchmod(fd, 0o600)
                finally:
                    os.close(fd)
            except OSError:
                try:
                    os.lchown(target, 0, 0)
                    os.chmod(target, 0o600)
                except OSError:
                    pass

    return backups


def scan(home: Path, values: list[tuple[str, bytes]]) -> ScanResult:
    """Scan directory for platform secrets without following symlinks.
    Returns ScanResult(hits, accepted, skipped)."""
    file_hits: dict[str, set[str]] = {}
    skipped = 0

    if not home.is_dir():
        return ScanResult(hits=[], accepted=[], skipped=0)

    for root, dirs, files in os.walk(home, followlinks=False):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not (Path(root) / d).is_symlink()]
        for name in files:
            p = Path(root) / name
            try:
                st = os.lstat(p)
            except OSError:
                skipped += 1
                continue

            if not stat.S_ISREG(st.st_mode):
                # Symlinks, FIFO, sockets are safely skipped without counting as unreadable
                continue

            rel = str(p.relative_to(home))
            name_lower = name.lower()
            found_vars: set[str] = set()
            file_skipped = False

            if name_lower.endswith(ARCHIVE_EXTENSIONS):
                arch_vars, arch_skip = _scan_archive(p, values)
                found_vars.update(arch_vars)
                if arch_skip:
                    file_skipped = True

            reg_vars, reg_skip = _scan_regular_file(p, values)
            found_vars.update(reg_vars)
            if reg_skip and not found_vars:
                file_skipped = True

            if file_skipped and not found_vars:
                skipped += 1

            if found_vars:
                file_hits.setdefault(rel, set()).update(found_vars)

    accepted: list[tuple[str, str]] = []
    hits: list[tuple[str, str]] = []

    for rel, vars_found in sorted(file_hits.items()):
        if rel == ".env" and vars_found.issubset(ACCEPTED):
            for var in sorted(vars_found):
                accepted.append((rel, var))
        else:
            for var in sorted(vars_found):
                hits.append((rel, var))

    return ScanResult(hits=hits, accepted=accepted, skipped=skipped)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Sweep and scan profile platform secrets.")
    ap.add_argument("--owner", required=True, help="Linux profile owner")
    ap.add_argument("--apply", action="store_true", help="Move backups to store")
    ap.add_argument("--allow-skipped", action="store_true", help="Do not exit with code 3 on skipped files")
    ap.add_argument("--platform-env", default=str(PLATFORM_ENV), help="Path to platform env file")
    ap.add_argument("--store", default=str(STORE), help="Base backup store path")
    args = ap.parse_args(argv)

    try:
        entry = pwd.getpwnam(args.owner)
    except KeyError:
        print(f"[{args.owner}] unknown linux user", file=sys.stderr)
        return 1

    hermes = Path(entry.pw_dir) / ".hermes"
    platform_env_path = Path(args.platform_env)
    values = load_platform_values(platform_env_path)
    owner_store = Path(args.store) / args.owner

    backups = sweep(hermes, owner_store, apply=args.apply, values=values)
    print(f"[{args.owner}] backups with keys in home: {len(backups)}")
    if args.apply and backups:
        print(f"   moved to {owner_store}")

    scan_res = scan(hermes, values)

    for rel, var in scan_res.accepted:
        print(f"[{args.owner}] WARNING accepted exception: {rel} ({var})")

    if scan_res.skipped > 0:
        print(f"[{args.owner}] skipped={scan_res.skipped}")

    if scan_res.hits:
        print(f"[{args.owner}] SECRETS STILL PRESENT:")
        for rel, var in scan_res.hits:
            print(f"   {rel}  ({var})")
        return 2

    if scan_res.skipped > 0 and not args.allow_skipped:
        return 3

    print(f"[{args.owner}] scan clean: no platform secret values under {hermes}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
