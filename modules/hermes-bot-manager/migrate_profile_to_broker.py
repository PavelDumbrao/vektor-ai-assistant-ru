#!/usr/bin/env python3
"""Перевести профиль Hermes на брокер ключей (VPS, root).

  migrate_profile_to_broker.py --owner X [--apply] [--restart]

Порт полосы — из /etc/proai-keybroker/lanes.json (сначала `lanectl add --client X`). Без --apply только показывает изменения.
Бэкапы `.env` и `config.yaml` → `*.bak-kb-<ts>` рядом (владелец и права сохраняются).

.env:   общие ключи платформы → заглушки (разные на маршрут: живая память требует «другой» ключ у резерва; брокер
        входящий Authorization игнорирует); MINIMAX_API_KEY удаляется (не используется); TAVILY_API_KEY пока остаётся
        (у веб-инструмента ядра нет переопределения адреса — решение отдельно). Добавляются адреса маршрутов для
        vendor-плагинов и патченного STT: GRSAI_BASE_URL, LINGSUAN_IMAGE_BASE_URL, LIVING_MEMORY_FALLBACK_BASE,
        HERMES_OPENROUTER_STT_ENDPOINT.
config: model.base_url/api_key, providers.*.api (по key_env), fallback_providers[].base_url (по host+key_env),
        tts.elevenlabs.base_url, mcp_servers.proaisaver.url + Authorization.
После --apply: сканер секретов — ни одно значение из /etc/proai-hermes-platform.env не должно остаться в профиле.
"""
import argparse
import json
import os
import pwd
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import yaml

LANES = Path("/etc/proai-keybroker/lanes.json")
PLATFORM_ENV = Path("/etc/proai-hermes-platform.env")
PLACEHOLDERS = {
    "LLM_API_KEY": "keybroker-lingsuan",
    "FALLBACK_LLM_API_KEY": "keybroker-lingsuan-fb",
    "LINGSUAN_OPUS_KEY": "keybroker-lingsuan-opus",
    "UNOROUTER_API_KEY": "keybroker-unorouter",
    "OPENROUTER_API_KEY": "keybroker-openrouter",
    "ELEVENLABS_API_KEY": "keybroker-elevenlabs",
    "GRSAI_API_KEY": "keybroker-grsai",
    "PROAISAVER_MCP_TOKEN": "keybroker-saver",
    "GEMINI_LLM_API_KEY": "keybroker-gemini",
}
REMOVE_ENV = {"MINIMAX_API_KEY"}
KEY_ENV_ROUTE = {"LLM_API_KEY": "/lingsuan/v1", "GEMINI_LLM_API_KEY": "/gemini/v1", "FALLBACK_LLM_API_KEY": "/lingsuan-fb/v1"}
FALLBACK_ROUTE = {
    ("api.lingsuan.si", "LINGSUAN_OPUS_KEY"): "/lingsuan-opus/v1",
    ("api.unorouter.com", "UNOROUTER_API_KEY"): "/unorouter/v1",
    ("lingsuan.top", "FALLBACK_LLM_API_KEY"): "/lingsuan-fb/v1",
    ("lingsuan.top", "LLM_API_KEY"): "/lingsuan/v1",
}

ap = argparse.ArgumentParser()
ap.add_argument("--owner", required=True)
ap.add_argument("--apply", action="store_true")
ap.add_argument("--restart", action="store_true")
args = ap.parse_args()

entry = pwd.getpwnam(args.owner)
hermes = Path(entry.pw_dir) / ".hermes"
lanes = json.loads(LANES.read_text(encoding="utf-8"))["lanes"]
lane = next((l for l in lanes if l["client"] == args.owner), None)
if not lane:
    sys.exit(f"no lane for {args.owner}: run lanectl add first")
L = f"http://127.0.0.1:{lane['port']}"
changes: list[str] = []


def write_preserving(path: Path, text: str) -> None:
    st = path.stat()
    stamp = time.strftime("%Y%m%dT%H%M%S")
    backup = path.with_name(path.name + f".bak-kb-{stamp}")
    backup.write_bytes(path.read_bytes())
    os.chown(backup, st.st_uid, st.st_gid)
    os.chmod(backup, 0o600)
    tmp = path.with_name(path.name + ".tmp-kb")
    tmp.write_text(text, encoding="utf-8")
    os.chown(tmp, st.st_uid, st.st_gid)
    os.chmod(tmp, st.st_mode & 0o777)
    os.replace(tmp, path)


def migrate_env(text: str) -> str:
    new_vars = {
        "GRSAI_BASE_URL": f"{L}/grsai",
        "LINGSUAN_IMAGE_BASE_URL": f"{L}/lingsuan-image",
        "LIVING_MEMORY_FALLBACK_BASE": f"{L}/lingsuan-opus/v1",
        "HERMES_OPENROUTER_STT_ENDPOINT": f"{L}/stt/v1/audio/transcriptions",
    }
    out, seen = [], set()
    for line in text.splitlines():
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if not m:
            out.append(line)
            continue
        key, value = m.group(1), m.group(2)
        seen.add(key)
        if key in REMOVE_ENV:
            changes.append(f".env: {key} removed")
            continue
        if key in PLACEHOLDERS:
            if value.strip().strip("\"'") != PLACEHOLDERS[key]:
                changes.append(f".env: {key} -> placeholder")
            out.append(f"{key}={PLACEHOLDERS[key]}")
            continue
        if key in new_vars:
            if value.strip() != new_vars[key]:
                changes.append(f".env: {key} = {new_vars[key]}")
            out.append(f"{key}={new_vars[key]}")
            continue
        out.append(line)
    for key, value in new_vars.items():
        if key not in seen:
            changes.append(f".env: + {key}={value}")
            out.append(f"{key}={value}")
    return "\n".join(out) + "\n"


def host(url: str) -> str:
    return (urlparse(str(url or "")).hostname or "").lower()


def setv(node: dict, key: str, value, label: str) -> None:
    if node.get(key) != value:
        changes.append(f"config: {label} -> {value if 'key' not in key.lower() and 'auth' not in key.lower() else '<placeholder>'}")
        node[key] = value


def migrate_config(cfg: dict) -> None:
    model = cfg.setdefault("model", {})
    if host(model.get("base_url")) not in ("lingsuan.top", "127.0.0.1"):
        sys.exit(f"unexpected model.base_url host: {host(model.get('base_url'))}")
    setv(model, "base_url", f"{L}/lingsuan/v1", "model.base_url")
    if model.get("api_key"):
        setv(model, "api_key", PLACEHOLDERS["LLM_API_KEY"], "model.api_key")
    for name, prov in (cfg.get("providers") or {}).items():
        key_env = prov.get("key_env")
        if host(prov.get("api")) in ("127.0.0.1",):
            continue
        if host(prov.get("api")) != "lingsuan.top" or key_env not in KEY_ENV_ROUTE:
            sys.exit(f"providers.{name}: unknown api/key_env combination ({host(prov.get('api'))}, {key_env})")
        setv(prov, "api", f"{L}{KEY_ENV_ROUTE[key_env]}", f"providers.{name}.api")
    for i, fb in enumerate(cfg.get("fallback_providers") or []):
        if host(fb.get("base_url")) == "127.0.0.1":
            continue
        route = FALLBACK_ROUTE.get((host(fb.get("base_url")), fb.get("key_env")))
        if not route:
            sys.exit(f"fallback_providers[{i}]: unknown combination ({host(fb.get('base_url'))}, {fb.get('key_env')})")
        setv(fb, "base_url", f"{L}{route}", f"fallback_providers[{i}].base_url")
    el = cfg.setdefault("tts", {}).setdefault("elevenlabs", {})
    setv(el, "base_url", f"{L}/elevenlabs", "tts.elevenlabs.base_url")
    saver = (cfg.get("mcp_servers") or {}).get("proaisaver")
    if saver:
        setv(saver, "url", f"{L}/saver/mcp/", "mcp_servers.proaisaver.url")
        headers = saver.setdefault("headers", {})
        setv(headers, "Authorization", f"Bearer {PLACEHOLDERS['PROAISAVER_MCP_TOKEN']}", "mcp_servers.proaisaver.headers.Authorization")


def platform_values() -> list[str]:
    values = []
    for line in PLATFORM_ENV.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if m:
            v = m.group(2).strip().strip("\"'")
            if len(v) >= 12:
                values.append(v)
    return values


def secret_scan() -> bool:
    leaked = []
    blobs = {p.name: p.read_text(encoding="utf-8", errors="replace") for p in (hermes / ".env", hermes / "config.yaml")}
    for v in platform_values():
        for name, blob in blobs.items():
            if v in blob:
                leaked.append(name)
    return not leaked


env_path, cfg_path = hermes / ".env", hermes / "config.yaml"
new_env = migrate_env(env_path.read_text(encoding="utf-8"))
cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
migrate_config(cfg)
print(f"[{args.owner}] lane port {lane['port']}")
for c in changes:
    print("  ", c)
if not changes:
    print("   (no changes)")
if not args.apply:
    sys.exit()
write_preserving(env_path, new_env)
write_preserving(cfg_path, yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False, width=1000))
print("   written | secret scan:", "CLEAN" if secret_scan() else "LEAK — platform values still present!")
if args.restart:
    unit = f"{args.owner}-hermes.service"
    subprocess.run(["systemctl", "restart", unit], check=True)
    time.sleep(25)
    state = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True).stdout.strip()
    gw = json.loads((hermes / "gateway_state.json").read_text(encoding="utf-8"))
    tg = gw.get("platforms", {}).get("telegram", {}).get("state")
    errors = subprocess.run(["journalctl", "-u", unit, "--since", "-40s", "-p", "err", "--no-pager", "-q"],
                            capture_output=True, text=True).stdout.strip()
    print(f"[{args.owner}] restarted: service={state} telegram={tg} errors={'none' if not errors else errors[-300:]}")
