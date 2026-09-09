#!/usr/bin/env python3
"""One-time root migration of approved shared provider keys into Forge platform secrets."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

SOURCE = Path("/home/pavel/.hermes/.env")
TARGET = Path("/etc/proai-hermes-platform.env")
MAPPING = {
    "LLM_API_KEY": "PAVEL_LINGSUAN_KEY",
    "FALLBACK_LLM_API_KEY": "PAVEL_GENGRUIHUAN_KEY",
    "OPENROUTER_API_KEY": "OPENROUTER_API_KEY",
    "TAVILY_API_KEY": "TAVILY_API_KEY",
    "GRSAI_API_KEY": "GRSAI_API_KEY",
    "MINIMAX_API_KEY": "MINIMAX_API_KEY",
}


def read_env(path: Path) -> dict[str, str]:
    values = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"\'')
    return values


def main() -> int:
    if os.geteuid() != 0:
        raise RuntimeError("root_required")
    if SOURCE.is_symlink() or not SOURCE.is_file():
        raise RuntimeError("source_env_missing")
    source = read_env(SOURCE)
    values = {}
    for target, origin in MAPPING.items():
        value = source.get(origin, "")
        if value:
            values[target] = value
    for required in ("LLM_API_KEY", "FALLBACK_LLM_API_KEY"):
        if not values.get(required):
            raise RuntimeError("required_platform_key_missing")
    if any("\n" in value or "\r" in value for value in values.values()):
        raise RuntimeError("platform_key_invalid")

    fd, raw = tempfile.mkstemp(prefix=".proai-hermes-platform.", dir="/etc")
    temp = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for key, value in sorted(values.items()):
                handle.write(f"{key}={value}\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chown(temp, 0, 0)
        os.chmod(temp, 0o600)
        os.replace(temp, TARGET)
    finally:
        if temp.exists():
            temp.unlink()
    print("platform_secrets_seeded=true")
    print(f"keys_migrated={len(values)}")
    print("secrets_printed=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
