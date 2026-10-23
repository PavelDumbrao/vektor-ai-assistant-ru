#!/usr/bin/env python3
"""proai-hermes-alerts: Forge fleet watchdog. Runs as root every 10 minutes (timer) and tells the Forge admin in
Telegram when something changes state: a client bot unit is down, a hire is stuck/failed, the key broker is
unhealthy, a release channel is paused, the nightly backup is stale, the disk is nearly full.

Alerts are sent on transitions only (ok -> bad, bad -> ok), with a reminder every 6 h while still bad, so a quiet
fleet means no messages. `--check` prints the status without sending; `--test` sends one test message.
Secrets: the manager bot token is read from the manager's env file by this root process and never printed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

PROFILES = Path("/opt/vektor/profiles")
MANAGER_ROOT = Path("/opt/proai-hermes-manager")
MANAGER_ENV = Path("/etc/proai-hermes-manager.env")
POLICY = Path("/etc/proai-hermes-fleet-policy.json")
BACKUP_MARK = Path("/var/backups/proai-hermes/last-ok")
STATE_PATH = Path("/var/lib/proai-hermes-alerts/state.json")
BROKER_HEALTH = "http://127.0.0.1:21000/healthz"
TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{20,}$")

PLATFORM_UNITS = [
    "proai-hermes-manager.service",
    "proai-keybroker.service",
    "proai-hermes-forge-api.service",
    "proai-hermes-forge-control.service",
    "proai-hermes-forge-bridge.service",
    "proai-hermes-fleet-updater.timer",
    "proai-keybroker-lanes.timer",
    "proai-hermes-backup.timer",
]
UNIT_CONFIRM_RUNS = 2          # a unit must be down on 2 consecutive runs (>= 10 min) before we alert
HIRE_DEADLINE_S = 20 * 60
BACKUP_MAX_AGE_S = 26 * 3600
DISK_MIN_FREE_PCT = 10
REMIND_EVERY_S = 6 * 3600


def run(cmd: list[str], timeout: int = 15) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def owners() -> list[str]:
    out = []
    for path in sorted(PROFILES.glob("*.json")):
        try:
            owner = json.loads(path.read_text(encoding="utf-8")).get("owner", "")
        except (OSError, ValueError):
            continue
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,39}", owner or ""):
            out.append(owner)
    return out


# ---- checks: each returns {check_id: (ok: bool, detail: str)} -------------------------------------------------

def check_units() -> dict[str, tuple[bool, str]]:
    res = {}
    units = [f"{o}-hermes.service" for o in owners()] + PLATFORM_UNITS
    for unit in units:
        state = run(["systemctl", "is-active", "--", unit]) or "unknown"
        res[f"unit:{unit}"] = (state == "active", f"{unit}: {state}")
    return res


def check_broker() -> dict[str, tuple[bool, str]]:
    try:
        with urllib.request.urlopen(BROKER_HEALTH, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - any failure is "broker down"
        return {"broker": (False, f"брокер ключей не отвечает: {type(exc).__name__}")}
    bad = [l.get("client") for l in data.get("lanes", []) if l.get("state") != "ok"]
    missing = data.get("routes_without_key") or []
    ok = bool(data.get("ok")) and not bad and not missing
    detail = "брокер ключей: ok"
    if not ok:
        detail = f"брокер ключей: ok={data.get('ok')} полосы не ok={bad} маршруты без ключа={missing}"
    return {"broker": (ok, detail)}


def _ts(value) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(time.mktime(time.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")))
        except ValueError:
            return 0
    return 0


def check_hires() -> dict[str, tuple[bool, str]]:
    res = {}
    try:
        state = json.loads((MANAGER_ROOT / "state" / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"manager-state": (False, f"state.json менеджера не читается: {type(exc).__name__}")}
    now = int(time.time())
    for bot_id, item in (state.get("managed") or {}).items():
        status = str(item.get("profile_status") or item.get("status") or "")
        name = item.get("username") or bot_id
        started = _ts(item.get("provision_started_at") or item.get("updated_at"))
        cid = f"hire:{bot_id}"
        if status in ("provision_failed", "token_pending", "failed"):
            res[cid] = (False, f"найм @{name}: {status} ({item.get('error_code') or 'без кода'})")
        elif status == "provisioning" and started and now - started > HIRE_DEADLINE_S:
            res[cid] = (False, f"найм @{name}: завис в provisioning {int((now - started) / 60)} мин")
        else:
            res[cid] = (True, f"найм @{name}: {status}")
    return res


def _paused(node) -> bool:
    if isinstance(node, dict):
        return bool(node.get("paused") is True) or any(_paused(v) for v in node.values())
    if isinstance(node, list):
        return any(_paused(v) for v in node)
    return False


def check_policy() -> dict[str, tuple[bool, str]]:
    try:
        policy = json.loads(POLICY.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"policy": (True, "политика флота: файла нет (пропуск)")}
    if policy.get("enabled") is False:
        return {"policy": (False, "политика флота: обновления выключены (enabled=false)")}
    if _paused(policy):
        return {"policy": (False, "политика флота: канал релиза на паузе (paused=true)")}
    return {"policy": (True, "политика флота: ok")}


def check_backup() -> dict[str, tuple[bool, str]]:
    try:
        last = int(BACKUP_MARK.read_text().strip())
    except (OSError, ValueError):
        return {"backup": (False, "бэкап: ещё ни одного успешного (нет last-ok)")}
    age = int(time.time()) - last
    ok = age <= BACKUP_MAX_AGE_S
    return {"backup": (ok, f"бэкап: последний успешный {int(age / 3600)} ч назад")}


def check_disk() -> dict[str, tuple[bool, str]]:
    usage = shutil.disk_usage("/")
    free_pct = usage.free * 100 // usage.total
    return {"disk": (free_pct >= DISK_MIN_FREE_PCT, f"диск /: свободно {free_pct}% ({usage.free // 2**30} ГБ)")}


def all_checks() -> dict[str, tuple[bool, str]]:
    res: dict[str, tuple[bool, str]] = {}
    for fn in (check_units, check_broker, check_hires, check_policy, check_backup, check_disk):
        try:
            res.update(fn())
        except Exception as exc:  # noqa: BLE001 - one broken check must not hide the others
            res[f"check:{fn.__name__}"] = (False, f"проверка {fn.__name__} упала: {type(exc).__name__}")
    return res


# ---- state + telegram ---------------------------------------------------------------------------------------

def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE_PATH)


def manager_token() -> str:
    for raw in MANAGER_ENV.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            value = line.split("=", 1)[1].strip().strip("\"'")
            if TOKEN_RE.fullmatch(value):
                return value
    raise RuntimeError("manager_token_missing")


def admin_chat_id() -> int:
    env = os.environ.get("FORGE_ALERT_CHAT_ID", "").strip()
    if env.isdigit():
        return int(env)
    src = (MANAGER_ROOT / "manager.py").read_text(encoding="utf-8")
    match = re.search(r"^PAVEL_ID\s*=\s*(\d+)", src, re.M)
    if not match:
        raise RuntimeError("admin_id_missing")
    return int(match.group(1))


def send(text: str) -> bool:
    token = manager_token()
    body = urllib.parse.urlencode({"chat_id": admin_chat_id(), "text": text[:4000], "disable_web_page_preview": "true"}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return bool(json.loads(resp.read().decode("utf-8")).get("ok"))
    except Exception as exc:  # noqa: BLE001
        print(f"alerts: telegram send failed: {type(exc).__name__}", file=sys.stderr)
        return False


def evaluate(results: dict[str, tuple[bool, str]], state: dict, now: int) -> tuple[list[str], dict]:
    """Returns (lines to send, new state). Units need UNIT_CONFIRM_RUNS consecutive failures; others alert at once."""
    lines: list[str] = []
    new_state: dict = {}
    for cid, (ok, detail) in results.items():
        prev = state.get(cid) or {}
        bad_runs = 0 if ok else int(prev.get("bad_runs", 0)) + 1
        need = UNIT_CONFIRM_RUNS if cid.startswith("unit:") else 1
        was_alerted = bool(prev.get("alerted"))
        entry = {"bad_runs": bad_runs, "alerted": was_alerted, "last_alert": int(prev.get("last_alert", 0)), "detail": detail}
        if not ok and bad_runs >= need:
            if not was_alerted or now - entry["last_alert"] >= REMIND_EVERY_S:
                lines.append(("🔴 " if not was_alerted else "🔁 всё ещё: ") + detail)
                entry["alerted"] = True
                entry["last_alert"] = now
        elif ok and was_alerted:
            lines.append("🟢 восстановлено: " + detail)
            entry["alerted"] = False
            entry["last_alert"] = 0
        new_state[cid] = entry
    return lines, new_state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="print status, do not send or save state")
    parser.add_argument("--test", action="store_true", help="send a test message and exit")
    args = parser.parse_args()
    if args.test:
        return 0 if send("✅ Forge watchdog: тестовое сообщение, алерты работают.") else 1
    now = int(time.time())
    results = all_checks()
    if args.check:
        for cid, (ok, detail) in sorted(results.items()):
            print(("OK   " if ok else "BAD  ") + detail)
        return 0 if all(ok for ok, _ in results.values()) else 1
    state = load_state()
    lines, new_state = evaluate(results, state, now)
    if lines:
        text = "⚙️ Hermes Forge watchdog\n" + "\n".join(lines)
        if not send(text):
            # keep the previous alert bookkeeping so the same transitions are retried next run
            for cid in new_state:
                if cid in state:
                    new_state[cid]["alerted"] = state[cid].get("alerted", False)
                    new_state[cid]["last_alert"] = state[cid].get("last_alert", 0)
    save_state(new_state)
    bad = [d for ok, d in results.values() if not ok]
    print(f"alerts: checks={len(results)} bad={len(bad)} sent={len(lines)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
