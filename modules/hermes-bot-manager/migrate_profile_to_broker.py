#!/usr/bin/env python3
"""Перевести профиль Hermes на брокер ключей (VPS, root).

  migrate_profile_to_broker.py --owner X [--apply] [--restart] [--allow-unpatched-release]

Порт полосы — из /etc/proai-keybroker/lanes.json (сначала `lanectl add --client X`). Без --apply только показывает изменения.
Бэкапы `.env` и `config.yaml` → в root-хранилище /opt/vektor/private-backups/profile-backups/<owner>/ (0700/0600 root).
В домашнем каталоге бэкапы не сохраняются.
"""
from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import yaml

LANES = Path("/etc/proai-keybroker/lanes.json")
PLATFORM_ENV = Path("/etc/proai-hermes-platform.env")
BACKUP_STORE = Path("/opt/vektor/private-backups/profile-backups")

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
ACCEPTED = {"TAVILY_API_KEY"}


def write_preserving(path: Path, text: str, uid: int, gid: int, mode: int = 0o600) -> None:
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.tmp-kb-", dir=path.parent)
    temp = Path(raw)
    try:
        try:
            os.fchown(fd, uid, gid)
        except PermissionError:
            pass
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass


def make_root_backup(path: Path, store: Path, owner: str) -> Path:
    dest = store / owner
    dest.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(store, 0o700)
    except OSError:
        pass
    try:
        os.chmod(dest, 0o700)
    except OSError:
        pass

    content = path.read_bytes()
    stamp = time.strftime("%Y%m%dT%H%M%S")
    counter = 0
    while True:
        suffix = f".bak-kb-{stamp}" + (f"-{counter}" if counter else "")
        backup_path = dest / f"{path.name}{suffix}"
        try:
            fd = os.open(backup_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            counter += 1
            if counter > 1000:
                raise

    try:
        try:
            os.fchown(fd, 0, 0)
        except PermissionError:
            pass
        os.fchmod(fd, 0o600)
        os.write(fd, content)
    finally:
        os.close(fd)

    return backup_path


def check_profile_files(hermes: Path, owner_uid: int) -> tuple[Path, Path]:
    env_path = hermes / ".env"
    cfg_path = hermes / "config.yaml"
    for p in (env_path, cfg_path):
        try:
            st = os.lstat(p)
        except OSError as exc:
            raise RuntimeError(f"{p} cannot be accessed: {exc}") from exc
        if not stat.S_ISREG(st.st_mode):
            raise RuntimeError(f"{p} is not a regular file (symlinks/devices not allowed)")
        if st.st_uid != owner_uid:
            raise RuntimeError(f"{p} owner uid {st.st_uid} does not match profile owner {owner_uid}")
    return env_path, cfg_path


def check_profile_release(hermes: Path, allow_unpatched: bool = False) -> None:
    agent_link = hermes / "hermes-agent"
    try:
        link_target = os.readlink(agent_link)
    except OSError as exc:
        if allow_unpatched:
            return
        raise RuntimeError(f"failed to readlink {agent_link}: {exc}") from exc

    target_path = Path(link_target)
    if not target_path.is_absolute():
        target_path = (hermes / target_path).resolve()

    candidates = [
        target_path / "runtime.json",
        target_path.parent / "runtime.json",
    ]
    runtime_file = next((c for c in candidates if c.is_file()), None)
    if not runtime_file:
        if allow_unpatched:
            return
        raise RuntimeError(f"runtime.json not found for release link {target_path}")

    try:
        data = json.loads(runtime_file.read_text(encoding="utf-8"))
    except Exception as exc:
        if allow_unpatched:
            return
        raise RuntimeError(f"failed to read {runtime_file}: {exc}") from exc

    verif = data.get("verification") or {}
    if verif.get("env_override_verified") is not True:
        if not allow_unpatched:
            raise RuntimeError(
                f"release {runtime_file.parent.name} verification.env_override_verified is not true"
            )


def migrate_env(text: str, lane_url: str, changes: list[str]) -> str:
    new_vars = {
        "GRSAI_BASE_URL": f"{lane_url}/grsai",
        "LINGSUAN_IMAGE_BASE_URL": f"{lane_url}/lingsuan-image",
        "LIVING_MEMORY_FALLBACK_BASE": f"{lane_url}/lingsuan-opus/v1",
        "HERMES_OPENROUTER_STT_ENDPOINT": f"{lane_url}/stt/v1/audio/transcriptions",
    }
    out: list[str] = []
    seen: set[str] = set()
    line_re = re.compile(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")

    for line in text.splitlines():
        m = line_re.match(line)
        if not m:
            out.append(line)
            continue
        raw_prefix = m.group(1)
        key = m.group(2)
        raw_value = m.group(3)
        seen.add(key)

        prefix = raw_prefix.rstrip()
        lead = (prefix + " ") if prefix else ""

        val_stripped = raw_value.strip()
        unquoted = (
            val_stripped[1:-1]
            if len(val_stripped) >= 2 and val_stripped[0] == val_stripped[-1] and val_stripped[0] in ('"', "'")
            else val_stripped
        )

        if key in REMOVE_ENV:
            changes.append(f".env: {key} removed")
            continue
        if key in PLACEHOLDERS:
            if unquoted != PLACEHOLDERS[key]:
                changes.append(f".env: {key} -> placeholder")
            out.append(f"{lead}{key}={PLACEHOLDERS[key]}")
            continue
        if key in new_vars:
            if unquoted != new_vars[key]:
                changes.append(f".env: {key} = {new_vars[key]}")
            out.append(f"{lead}{key}={new_vars[key]}")
            continue
        out.append(line)

    for key, value in new_vars.items():
        if key not in seen:
            changes.append(f".env: + {key}={value}")
            out.append(f"{key}={value}")

    return "\n".join(out) + "\n"


def host(url: str) -> str:
    return (urlparse(str(url or "")).hostname or "").lower()


def setv(node: dict, key: str, value, label: str, changes: list[str]) -> None:
    if node.get(key) != value:
        changes.append(
            f"config: {label} -> {value if 'key' not in key.lower() and 'auth' not in key.lower() else '<placeholder>'}"
        )
        node[key] = value


def migrate_config(cfg: dict, lane_url: str, changes: list[str]) -> None:
    model = cfg.setdefault("model", {})
    if host(model.get("base_url")) not in ("lingsuan.top", "127.0.0.1"):
        raise RuntimeError(f"unexpected model.base_url host: {host(model.get('base_url'))}")
    setv(model, "base_url", f"{lane_url}/lingsuan/v1", "model.base_url", changes)
    if model.get("api_key"):
        setv(model, "api_key", PLACEHOLDERS["LLM_API_KEY"], "model.api_key", changes)
    for name, prov in (cfg.get("providers") or {}).items():
        key_env = prov.get("key_env")
        if host(prov.get("api")) in ("127.0.0.1",):
            continue
        if host(prov.get("api")) != "lingsuan.top" or key_env not in KEY_ENV_ROUTE:
            raise RuntimeError(f"providers.{name}: unknown api/key_env combination ({host(prov.get('api'))}, {key_env})")
        setv(prov, "api", f"{lane_url}{KEY_ENV_ROUTE[key_env]}", f"providers.{name}.api", changes)
    for i, fb in enumerate(cfg.get("fallback_providers") or []):
        if host(fb.get("base_url")) == "127.0.0.1":
            continue
        route = FALLBACK_ROUTE.get((host(fb.get("base_url")), fb.get("key_env")))
        if not route:
            raise RuntimeError(f"fallback_providers[{i}]: unknown combination ({host(fb.get('base_url'))}, {fb.get('key_env')})")
        setv(fb, "base_url", f"{lane_url}{route}", f"fallback_providers[{i}].base_url", changes)
    el = cfg.setdefault("tts", {}).setdefault("elevenlabs", {})
    setv(el, "base_url", f"{lane_url}/elevenlabs", "tts.elevenlabs.base_url", changes)
    saver = (cfg.get("mcp_servers") or {}).get("proaisaver")
    if saver:
        setv(saver, "url", f"{lane_url}/saver/mcp/", "mcp_servers.proaisaver.url", changes)
        headers = saver.setdefault("headers", {})
        setv(
            headers,
            "Authorization",
            f"Bearer {PLACEHOLDERS['PROAISAVER_MCP_TOKEN']}",
            "mcp_servers.proaisaver.headers.Authorization",
            changes,
        )


def load_platform_secrets(path: Path) -> list[tuple[str, str]]:
    if not path.is_file():
        return []
    values: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if m:
            v = m.group(2).strip().strip("\"'")
            if len(v) >= 12:
                values.append((m.group(1), v))
    return values


def scan_for_secrets(hermes: Path, platform_env_path: Path) -> tuple[bool, list[tuple[str, str]]]:
    """Check .env and config.yaml for leaked platform secrets with ACCEPTED exemption."""
    platform_values = load_platform_secrets(platform_env_path)
    blobs = {
        p.name: p.read_text(encoding="utf-8", errors="replace")
        for p in (hermes / ".env", hermes / "config.yaml")
        if p.is_file()
    }

    file_vars: dict[str, set[str]] = {}
    for var, val in platform_values:
        for fname, text in blobs.items():
            if val in text:
                file_vars.setdefault(fname, set()).add(var)

    leaks: list[tuple[str, str]] = []
    for fname, vars_found in file_vars.items():
        if fname == ".env" and vars_found.issubset(ACCEPTED):
            continue
        for var in sorted(vars_found):
            leaks.append((fname, var))

    return (len(leaks) == 0), leaks


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Migrate Hermes profile to keybroker.")
    ap.add_argument("--owner", required=True, help="Linux profile owner")
    ap.add_argument("--apply", action="store_true", help="Apply migrations")
    ap.add_argument("--restart", action="store_true", help="Restart systemd unit after applying")
    ap.add_argument("--allow-unpatched-release", action="store_true", help="Allow unpatched release without env_override_verified")
    ap.add_argument("--lanes-file", default=str(LANES), help="Path to lanes.json")
    ap.add_argument("--platform-env", default=str(PLATFORM_ENV), help="Path to platform env")
    ap.add_argument("--store", default=str(BACKUP_STORE), help="Root backup store dir")
    args = ap.parse_args(argv)

    try:
        entry = pwd.getpwnam(args.owner)
    except KeyError:
        print(f"[{args.owner}] unknown linux user", file=sys.stderr)
        return 1

    hermes = Path(entry.pw_dir) / ".hermes"

    # Pre-check: regular files owned by profile owner (no symlinks)
    try:
        env_path, cfg_path = check_profile_files(hermes, entry.pw_uid)
    except Exception as exc:
        print(f"[{args.owner}] file check failed: {exc}", file=sys.stderr)
        return 1

    # Check release verification
    try:
        check_profile_release(hermes, allow_unpatched=args.allow_unpatched_release)
    except Exception as exc:
        print(f"[{args.owner}] release check failed: {exc}", file=sys.stderr)
        return 1

    lanes_path = Path(args.lanes_file)
    if not lanes_path.is_file():
        print(f"[{args.owner}] lanes file not found: {lanes_path}", file=sys.stderr)
        return 1

    try:
        lanes_data = json.loads(lanes_path.read_text(encoding="utf-8")).get("lanes", [])
        lane = next((l for l in lanes_data if l.get("client") == args.owner), None)
    except Exception as exc:
        print(f"[{args.owner}] failed to read lanes: {exc}", file=sys.stderr)
        return 1

    if not lane:
        print(f"[{args.owner}] no lane for {args.owner}: run lanectl add first", file=sys.stderr)
        return 1

    lane_url = f"http://127.0.0.1:{lane['port']}"
    changes: list[str] = []

    new_env = migrate_env(env_path.read_text(encoding="utf-8"), lane_url, changes)
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    try:
        migrate_config(cfg, lane_url, changes)
    except Exception as exc:
        print(f"[{args.owner}] config migration failed: {exc}", file=sys.stderr)
        return 1

    print(f"[{args.owner}] lane port {lane['port']}")
    for c in changes:
        print("  ", c)
    if not changes:
        print("   (no changes)")

    if not args.apply:
        return 0

    st_env = env_path.stat()
    st_cfg = cfg_path.stat()
    store_dir = Path(args.store)

    # Make backups in root store with O_CREAT|O_EXCL
    cfg_backup = make_root_backup(cfg_path, store_dir, args.owner)
    env_backup = make_root_backup(env_path, store_dir, args.owner)

    # Write config.yaml first, then .env; rollback config on .env failure
    new_cfg_text = yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False, width=1000)
    write_preserving(cfg_path, new_cfg_text, st_cfg.st_uid, st_cfg.st_gid, st_cfg.st_mode & 0o777)

    try:
        write_preserving(env_path, new_env, st_env.st_uid, st_env.st_gid, st_env.st_mode & 0o777)
    except Exception as exc:
        # Rollback config.yaml from root backup
        write_preserving(cfg_path, cfg_backup.read_text(encoding="utf-8"), st_cfg.st_uid, st_cfg.st_gid, st_cfg.st_mode & 0o777)
        print(f"[{args.owner}] failed writing .env, rolled back config.yaml: {exc}", file=sys.stderr)
        return 1

    platform_env_path = Path(args.platform_env)
    is_clean, leaks = scan_for_secrets(hermes, platform_env_path)
    if not is_clean:
        print(f"[{args.owner}] written | secret scan: LEAK — platform values still present!", file=sys.stderr)
        for fname, var in leaks:
            print(f"   {fname}  ({var})", file=sys.stderr)
        return 2

    print(f"[{args.owner}] written | secret scan: CLEAN")

    if args.restart:
        restart_time = time.time()
        unit = f"{args.owner}-hermes.service"
        subprocess.run(["systemctl", "restart", unit], check=True)
        time.sleep(25)
        state = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True).stdout.strip()

        gw_file = hermes / "gateway_state.json"
        if not gw_file.exists():
            print(f"[{args.owner}] gateway_state.json missing after restart", file=sys.stderr)
            return 1
        if gw_file.stat().st_mtime <= restart_time:
            print(f"[{args.owner}] gateway_state.json mtime is older than restart timestamp", file=sys.stderr)
            return 1

        try:
            gw = json.loads(gw_file.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[{args.owner}] invalid gateway_state.json: {exc}", file=sys.stderr)
            return 1

        main_pid_res = subprocess.run(
            ["systemctl", "show", unit, "--property=MainPID", "--value"],
            capture_output=True, text=True, check=False,
        )
        main_pid_str = main_pid_res.stdout.strip()
        if main_pid_str.isdigit():
            main_pid = int(main_pid_str)
            gw_pid = gw.get("pid")
            if gw_pid is not None and gw_pid != main_pid:
                print(f"[{args.owner}] gateway_state.json pid ({gw_pid}) != MainPID ({main_pid})", file=sys.stderr)
                return 1

        tg = gw.get("platforms", {}).get("telegram", {}).get("state")
        errors = subprocess.run(
            ["journalctl", "-u", unit, "--since", "-40s", "-p", "err", "--no-pager", "-q"],
            capture_output=True, text=True,
        ).stdout.strip()
        print(f"[{args.owner}] restarted: service={state} telegram={tg} errors={'none' if not errors else errors[-300:]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
