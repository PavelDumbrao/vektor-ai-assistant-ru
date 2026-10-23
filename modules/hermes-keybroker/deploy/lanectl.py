#!/usr/bin/env python3
"""lanectl — полосы брокера ключей: lanes.json + правила iptables. Только root, только stdlib.

  lanectl.py add --client X [--port P] [--elevenlabs-chars N] [--rpm N] [--restrict-loopback]
  lanectl.py remove --client X
  lanectl.py apply                 перестроить цепочки из lanes.json (идемпотентно; запускается юнитом/таймером)
  lanectl.py status                полосы, правила, слушает ли брокер
  lanectl.py check --client X      проба под uid клиента: своя полоса доступна, чужая — отклонена, admin — отклонён

Правила (filter/OUTPUT, только исходящие на lo):
  KEYBROKER          порт полосы: RETURN для uid клиента и root, остальным REJECT (tcp-reset); admin-порт — только root.
  KEYBROKER_CLIENTS  (для полос с "restrict_loopback": true) uid клиента на lo/докер-мостах: разрешены своя полоса,
                     порты из "loopback_allow", DNS systemd-resolved; остальное — LOG (лимит) + REJECT.
После add/remove: `systemctl reload proai-keybroker` (SIGHUP → брокер перечитывает полосы).
"""
import argparse
import json
import os
import pwd
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

LANES = Path(os.environ.get("KEYBROKER_LANES", "/etc/proai-keybroker/lanes.json"))
ADMIN_PORT = int(os.environ.get("KEYBROKER_ADMIN_PORT", "21000"))
PORT_RANGE = range(21001, 21100)
BROKER_UNIT = "proai-keybroker.service"
CHAIN = "KEYBROKER"
CHAIN_CLIENTS = "KEYBROKER_CLIENTS"
IPT = ["/usr/sbin/iptables", "-w", "5"]
DNS = "127.0.0.53/32"
PRIVATE_DESTS = ("127.0.0.0/8", "172.17.0.1/32", "172.18.0.1/32", "10.253.0.1/32")
DEFAULT_LOOPBACK_ALLOW = [8082, 15432]  # локальный Telegram Bot API, Postgres пассивного секретаря


def load() -> dict:
    if not LANES.exists():
        return {"lanes": []}
    data = json.loads(LANES.read_text(encoding="utf-8"))
    if not isinstance(data.get("lanes"), list):
        sys.exit(f"{LANES}: 'lanes' must be a list")
    return data


def save(data: dict) -> None:
    LANES.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    fd, tmp = tempfile.mkstemp(dir=LANES.parent, prefix=".lanes-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, LANES)


def ipt(*args, check=True):
    return subprocess.run([*IPT, *args], capture_output=True, text=True, check=check)


def rule_exists(*rule) -> bool:
    return ipt("-C", *rule, check=False).returncode == 0


def rebuild_chain(name: str, rules: list[list[str]], jump: list[str]) -> None:
    ipt("-N", name, check=False)
    ipt("-F", name)
    for r in rules:
        ipt("-A", name, *r)
    if not rule_exists("OUTPUT", *jump, "-j", name):
        ipt("-I", "OUTPUT", "1", *jump, "-j", name)


def apply(data: dict) -> None:
    rules = [
        ["-p", "tcp", "--dport", str(ADMIN_PORT), "-m", "owner", "--uid-owner", "0", "-j", "RETURN"],
        ["-p", "tcp", "--dport", str(ADMIN_PORT), "-j", "REJECT", "--reject-with", "tcp-reset"],
    ]
    client_rules: list[list[str]] = []
    for lane in data["lanes"]:
        uid, port = str(lane["uid"]), str(lane["port"])
        rules += [
            ["-p", "tcp", "--dport", port, "-m", "owner", "--uid-owner", uid, "-j", "RETURN"],
            ["-p", "tcp", "--dport", port, "-m", "owner", "--uid-owner", "0", "-j", "RETURN"],
            ["-p", "tcp", "--dport", port, "-j", "REJECT", "--reject-with", "tcp-reset"],
        ]
        if lane.get("restrict_loopback"):
            allow = [int(port)] + [int(p) for p in lane.get("loopback_allow", DEFAULT_LOOPBACK_ALLOW)]
            owner = ["-m", "owner", "--uid-owner", uid]
            client_rules += [[*owner, "-d", DNS, "-p", proto, "--dport", "53", "-j", "RETURN"] for proto in ("udp", "tcp")]
            client_rules += [[*owner, "-o", "lo", "-p", "tcp", "--dport", str(p), "-j", "RETURN"] for p in allow]
            for dest in PRIVATE_DESTS:
                client_rules.append([*owner, "-d", dest, "-m", "limit", "--limit", "10/min", "-j", "LOG",
                                     "--log-prefix", f"keybroker-deny {lane['client']} "])
                client_rules.append([*owner, "-d", dest, "-j", "REJECT", "--reject-with", "icmp-port-unreachable"])
    rules.append(["-j", "RETURN"])
    rebuild_chain(CHAIN, rules, ["-o", "lo", "-p", "tcp"])
    client_rules.append(["-j", "RETURN"])
    rebuild_chain(CHAIN_CLIENTS, client_rules, [])


def reload_broker() -> None:
    r = subprocess.run(["systemctl", "is-active", "--quiet", BROKER_UNIT])
    if r.returncode == 0:
        subprocess.run(["systemctl", "reload", BROKER_UNIT], check=False)


def cmd_add(a) -> None:
    entry = pwd.getpwnam(a.client)
    data = load()
    lanes = data["lanes"]
    if any(l["client"] == a.client for l in lanes):
        sys.exit(f"lane for {a.client} already exists: use remove first")
    used = {int(l["port"]) for l in lanes}
    port = a.port or next((p for p in PORT_RANGE if p not in used), None)
    if port is None or port in used or port == ADMIN_PORT:
        sys.exit("no free port in range")
    lane = {"client": a.client, "uid": entry.pw_uid, "port": port, "limits": {}}
    if a.elevenlabs_chars:
        lane["limits"]["elevenlabs_chars_month"] = a.elevenlabs_chars
    if a.rpm:
        lane["limits"]["rpm"] = a.rpm
    if a.restrict_loopback:
        lane["restrict_loopback"] = True
        lane["loopback_allow"] = DEFAULT_LOOPBACK_ALLOW
    lanes.append(lane)
    save(data)
    apply(data)
    reload_broker()
    print(json.dumps(lane))


def cmd_remove(a) -> None:
    data = load()
    before = len(data["lanes"])
    data["lanes"] = [l for l in data["lanes"] if l["client"] != a.client]
    if len(data["lanes"]) == before:
        sys.exit(f"no lane for {a.client}")
    save(data)
    apply(data)
    reload_broker()
    print("removed", a.client)


def listening(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def cmd_status(a) -> None:
    data = load()
    print(f"admin {ADMIN_PORT}: {'listening' if listening(ADMIN_PORT) else 'DOWN'} | broker: "
          f"{subprocess.run(['systemctl', 'is-active', BROKER_UNIT], capture_output=True, text=True).stdout.strip()}")
    jump_ok = rule_exists("OUTPUT", "-o", "lo", "-p", "tcp", "-j", CHAIN)
    print(f"chain {CHAIN} jump: {'ok' if jump_ok else 'MISSING'}")
    for lane in data["lanes"]:
        ok = rule_exists(CHAIN, "-p", "tcp", "--dport", str(lane["port"]), "-m", "owner", "--uid-owner", str(lane["uid"]), "-j", "RETURN")
        print(f"{lane['client']:14} uid={lane['uid']:<6} port={lane['port']} rules={'ok' if ok else 'MISSING'} "
              f"listening={'yes' if listening(lane['port']) else 'no'} limits={lane.get('limits') or {}} "
              f"restrict_loopback={bool(lane.get('restrict_loopback'))}")


def probe_as(user: str, port: int) -> str:
    code = ("import socket,sys\ns=socket.socket();s.settimeout(2)\n"
            f"print('open' if s.connect_ex(('127.0.0.1',{port}))==0 else 'rejected')")
    r = subprocess.run(["runuser", "-u", user, "--", "/usr/bin/python3", "-c", code], capture_output=True, text=True)
    return (r.stdout or r.stderr).strip()[-40:]


def cmd_check(a) -> None:
    data = load()
    mine = next((l for l in data["lanes"] if l["client"] == a.client), None)
    if not mine:
        sys.exit(f"no lane for {a.client}")
    print(f"{a.client} -> own port {mine['port']}: {probe_as(a.client, mine['port'])}")
    print(f"{a.client} -> admin {ADMIN_PORT}: {probe_as(a.client, ADMIN_PORT)}")
    for other in data["lanes"]:
        if other["client"] != a.client:
            print(f"{a.client} -> {other['client']} port {other['port']}: {probe_as(a.client, other['port'])}")


def main() -> None:
    if os.geteuid() != 0:
        sys.exit("root required")
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("add"); p.add_argument("--client", required=True); p.add_argument("--port", type=int)
    p.add_argument("--elevenlabs-chars", type=int); p.add_argument("--rpm", type=int); p.add_argument("--restrict-loopback", action="store_true")
    p = sub.add_parser("remove"); p.add_argument("--client", required=True)
    sub.add_parser("apply"); sub.add_parser("status")
    p = sub.add_parser("check"); p.add_argument("--client", required=True)
    a = ap.parse_args()
    if a.cmd == "apply":
        apply(load()); print("applied")
    else:
        {"add": cmd_add, "remove": cmd_remove, "status": cmd_status, "check": cmd_check}[a.cmd](a)


if __name__ == "__main__":
    main()
