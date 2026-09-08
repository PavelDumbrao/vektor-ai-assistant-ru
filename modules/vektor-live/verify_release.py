#!/usr/bin/env python3
from __future__ import annotations
import hashlib, json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
MANIFEST = ROOT / "releases" / VERSION / "manifest.json"

def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def main() -> int:
    spec = json.loads(MANIFEST.read_text(encoding="utf-8"))
    errors = []
    if spec.get("version") != VERSION:
        errors.append("VERSION does not match manifest")
    for rel, expected in spec.get("files_sha256", {}).items():
        path = ROOT / rel
        if not path.is_file():
            errors.append(f"missing: {rel}")
        elif digest(path) != expected:
            errors.append(f"digest mismatch: {rel}")
    if errors:
        raise SystemExit("\n".join(errors))
    print(f"Vektor Live {VERSION}: {len(spec['files_sha256'])} production files match manifest")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
