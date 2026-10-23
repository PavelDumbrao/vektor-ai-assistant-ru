#!/usr/bin/env python3
"""After a profile moved to the key broker: take config/.env backups (they still hold platform keys) out of the
client's home into a root-only store, then scan the whole profile for any platform secret value. VPS, root.

  sweep_profile_secrets.py --owner X [--apply]

Moves `~/.hermes/config.yaml.bak-*` and `~/.hermes/.env.bak-*` to /opt/vektor/private-backups/profile-backups/<owner>/
(dir 0700 root, files 0600 root). Then walks ~/.hermes (text files ≤ 5 MB, skipping the shared release symlink,
venv/lazy packages and caches) and reports files that still contain a value from /etc/proai-hermes-platform.env.
Exit code 2 if anything is found."""
import argparse
import os
import pwd
import re
import shutil
import sys
from pathlib import Path

PLATFORM_ENV = Path("/etc/proai-hermes-platform.env")
STORE = Path("/opt/vektor/private-backups/profile-backups")
SKIP_DIRS = {"hermes-agent", "lazy-packages", "venv", "cache", "__pycache__", "node_modules", ".pytest_cache"}

ap = argparse.ArgumentParser()
ap.add_argument("--owner", required=True)
ap.add_argument("--apply", action="store_true")
args = ap.parse_args()
entry = pwd.getpwnam(args.owner)
hermes = Path(entry.pw_dir) / ".hermes"

backups = sorted(p for p in hermes.iterdir() if p.is_file() and re.match(r"^(config\.yaml|\.env)\.bak-", p.name))
backups += sorted(p for p in (hermes / "backups").rglob("*") if p.is_file() and p.name in (".env", "config.yaml"))
print(f"[{args.owner}] backups with keys in home: {len(backups)}")
if args.apply and backups:
    dest = STORE / args.owner
    dest.mkdir(parents=True, exist_ok=True)
    os.chmod(STORE, 0o700); os.chmod(dest, 0o700)
    for p in backups:
        target = dest / p.relative_to(hermes)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(target.parent, 0o700)
        shutil.move(str(p), str(target))
        os.chown(target, 0, 0); os.chmod(target, 0o600)
    print(f"   moved to {dest}")

values = []
for line in PLATFORM_ENV.read_text(encoding="utf-8").splitlines():
    m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
    if m:
        v = m.group(2).strip().strip("\"'")
        if len(v) >= 12:
            values.append((m.group(1), v.encode()))

hits = []
for root, dirs, files in os.walk(hermes, followlinks=False):
    dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not (Path(root) / d).is_symlink()]
    for name in files:
        p = Path(root) / name
        try:
            if p.is_symlink() or p.stat().st_size > 5 * 1024 * 1024:
                continue
            blob = p.read_bytes()
        except OSError:
            continue
        for var, v in values:
            if v in blob:
                hits.append((str(p.relative_to(hermes)), var))
                break
ACCEPTED = {"TAVILY_API_KEY"}  # no upstream override in the core web tool yet; tracked as a known exception
accepted = [(rel, var) for rel, var in hits if var in ACCEPTED and rel == ".env"]
hits = [(rel, var) for rel, var in hits if (rel, var) not in accepted]
for rel, var in accepted:
    print(f"[{args.owner}] WARNING accepted exception: {rel} ({var})")
if hits:
    print(f"[{args.owner}] SECRETS STILL PRESENT:")
    for rel, var in hits:
        print(f"   {rel}  ({var})")
    sys.exit(2)
print(f"[{args.owner}] scan clean: no platform secret values under {hermes}")
