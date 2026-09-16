#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]
ALLOWED_STATUS = {
    "captured", "needs_clarification", "research", "proposed",
    "approved", "in_progress", "deployed", "deferred",
}
ALLOWED_COMMERCIAL = {"included", "paid_addon", "future_product", "unknown"}
FORBIDDEN_KEYS = {"raw_chat", "raw_transcript", "transcript_text", "message_text", "api_key", "token", "session_string"}

def walk(obj, path: str, errors: list[str]) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            key_s = str(key).lower()
            if key_s in FORBIDDEN_KEYS:
                errors.append(f"{path}: forbidden key {key}")
            if key == "status" and value not in ALLOWED_STATUS:
                errors.append(f"{path}: invalid status {value!r}")
            if key == "commercial" and value not in ALLOWED_COMMERCIAL:
                errors.append(f"{path}: invalid commercial {value!r}")
            walk(value, f"{path}.{key}", errors)
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            walk(value, f"{path}[{i}]", errors)

def main() -> int:
    files = sorted((ROOT / "clients").glob("*/*.yaml")) + sorted((ROOT / "capabilities").glob("*.yaml"))
    errors: list[str] = []
    if not files:
        errors.append("registry contains no YAML files")
    for file in files:
        try:
            data = yaml.safe_load(file.read_text(encoding="utf-8"))
        except Exception as exc:
            errors.append(f"{file.relative_to(ROOT)}: YAML parse error: {exc}")
            continue
        if not isinstance(data, dict) or not data.get("schema"):
            errors.append(f"{file.relative_to(ROOT)}: missing top-level schema")
            continue
        walk(data, str(file.relative_to(ROOT)), errors)
    if errors:
        print("registry validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print(f"registry validation OK: {len(files)} YAML files")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
