#!/usr/bin/env python3
"""lanectl — полосы брокера ключей: lanes.json + правила iptables/ip6tables. Только root, только stdlib.

  lanectl.py add --client X [--port P] [--elevenlabs-chars N] [--rpm N] [--restrict-loopback]
  lanectl.py remove --client X
  lanectl.py apply [--dry-run]     атомарно перестроить цепочки из lanes.json (iptables-restore --noflush)
  lanectl.py status                полосы, правила, слушатели; код 1 при расхождении
  lanectl.py check --client X      пробы под uid клиента: своя полоса открыта, чужие/admin отклонены; код 1 при расхождении

Правила (filter/OUTPUT, первый jump — наш):
  KEYBROKER (IPv4, только lo/tcp): admin-порт — RETURN для root; полоса — RETURN для uid клиента и root;
      затем REJECT (tcp-reset) на ВЕСЬ диапазон полос 21000-21099 — цепочка fail-closed даже без полос;
  KEYBROKER (IPv6, lo/tcp): REJECT на диапазон для всех (брокер слушает только 127.0.0.1);
  KEYBROKER_CLIENTS (полосы с "restrict_loopback": true): uid клиента на lo/докер-мостах — разрешены своя полоса,
      порты из "loopback_allow" и DNS systemd-resolved; остальное — LOG (лимит) + REJECT.
Порядок: apply → save (запись в файл только после успешного применения); flock на /run/lanectl.lock.
После add/remove: `systemctl reload proai-keybroker` (SIGHUP → брокер перечитывает полосы).
"""
import argparse
import errno
import fcntl
import json
import os
import pwd
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

LANES = Path(os.environ.get("KEYBROKER_LANES", "/etc/proai-keybroker/lanes.json"))
ADMIN_PORT = int(os.environ.get("KEYBROKER_ADMIN_PORT", "21000"))
LANE_RANGE = (21000, 21099)          # admin + полосы; REJECT по умолчанию на весь диапазон
PORT_RANGE = range(21001, LANE_RANGE[1] + 1)
BROKER_UNIT = "proai-keybroker.service"
BROKER_USER = "proai-keybroker"
CHAIN = "KEYBROKER"
CHAIN_CLIENTS = "KEYBROKER_CLIENTS"
IPT = "/usr/sbin/iptables"
IP6T = "/usr/sbin/ip6tables"
LOCK = Path("/run/lanectl.lock")
DNS = "127.0.0.53/32"
PRIVATE_DESTS = ("127.0.0.0/8", "172.17.0.1/32", "172.18.0.1/32", "10.253.0.1/32")
DEFAULT_LOOPBACK_ALLOW = [8082, 15432]  # локальный Telegram Bot API, Postgres пассивного секретаря


def log(msg: str) -> None:
    print(f"lanectl: {msg}", flush=True)


def load() -> dict:
    if not LANES.exists():
        return {"lanes": []}
    data = json.loads(LANES.read_text(encoding="utf-8"))
    if not isinstance(data.get("lanes"), list):
        sys.exit(f"{LANES}: 'lanes' must be a list")
    for lane in data["lanes"]:
        for key in ("client", "uid", "port"):
            if key not in lane:
                sys.exit(f"{LANES}: lane without '{key}': {lane}")
    return data


def save(data: dict) -> None:
    LANES.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    fd, tmp = tempfile.mkstemp(dir=LANES.parent, prefix=".lanes-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    try:
        gid = pwd.getpwnam(BROKER_USER).pw_gid
    except KeyError:
        gid = 0
    os.chown(tmp, 0, gid)
    os.chmod(tmp, 0o640)   # root + broker only: lane ports/limits are not for other local users
    os.replace(tmp, LANES)
    dfd = os.open(LANES.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def run(cmd, input_text=None, check=True):
    try:
        return subprocess.run(cmd, input=input_text, capture_output=True, text=True, check=check, timeout=60)
    except subprocess.CalledProcessError as exc:
        sys.exit(f"{' '.join(cmd[:2])} failed (rc={exc.returncode}): {exc.stderr.strip()[-400:]}")
    except FileNotFoundError:
        sys.exit(f"{cmd[0]} not found")


def valid_lane(lane: dict) -> bool:
    """uid должен совпадать с учётной записью client; порт — в диапазоне полос."""
    try:
        entry = pwd.getpwnam(lane["client"])
    except KeyError:
        log(f"skip lane {lane['client']}: no such user")
        return False
    if int(lane["uid"]) != entry.pw_uid:
        log(f"skip lane {lane['client']}: uid {lane['uid']} != account uid {entry.pw_uid}")
        return False
    if int(lane["port"]) not in PORT_RANGE:
        log(f"skip lane {lane['client']}: port {lane['port']} outside {PORT_RANGE.start}-{PORT_RANGE[-1]}")
        return False
    return True


def ruleset_v4(data: dict) -> str:
    lo, hi = LANE_RANGE
    rules = [f"-A {CHAIN} -p tcp --dport {ADMIN_PORT} -m owner --uid-owner 0 -j RETURN"]
    clients: list[str] = []
    for lane in data["lanes"]:
        if not valid_lane(lane):
            continue
        uid, port = int(lane["uid"]), int(lane["port"])
        rules += [f"-A {CHAIN} -p tcp --dport {port} -m owner --uid-owner {uid} -j RETURN",
                  f"-A {CHAIN} -p tcp --dport {port} -m owner --uid-owner 0 -j RETURN"]
        if lane.get("restrict_loopback"):
            allow = [port] + [int(p) for p in lane.get("loopback_allow", DEFAULT_LOOPBACK_ALLOW)]
            owner = f"-m owner --uid-owner {uid}"
            clients += [f"-A {CHAIN_CLIENTS} {owner} -d {DNS} -p {proto} --dport 53 -j RETURN" for proto in ("udp", "tcp")]
            clients += [f"-A {CHAIN_CLIENTS} {owner} -o lo -p tcp --dport {p} -j RETURN" for p in allow]
            for dest in PRIVATE_DESTS:
                clients.append(f"-A {CHAIN_CLIENTS} {owner} -d {dest} -m limit --limit 10/min -j LOG --log-prefix \"kb-deny:{uid} \"")
                clients.append(f"-A {CHAIN_CLIENTS} {owner} -d {dest} -j REJECT --reject-with icmp-port-unreachable")
    rules.append(f"-A {CHAIN} -p tcp --dport {lo}:{hi} -j REJECT --reject-with tcp-reset")  # fail-closed по умолчанию
    rules.append(f"-A {CHAIN} -j RETURN")
    clients.append(f"-A {CHAIN_CLIENTS} -j RETURN")
    return "*filter\n" + f":{CHAIN} - [0:0]\n:{CHAIN_CLIENTS} - [0:0]\n-F {CHAIN}\n-F {CHAIN_CLIENTS}\n" + "\n".join(rules + clients) + "\nCOMMIT\n"


def ruleset_v6() -> str:
    lo, hi = LANE_RANGE
    return ("*filter\n" + f":{CHAIN} - [0:0]\n-F {CHAIN}\n"
            f"-A {CHAIN} -p tcp --dport {lo}:{hi} -j REJECT --reject-with tcp-reset\n-A {CHAIN} -j RETURN\nCOMMIT\n")


def ensure_first_jump(binary: str, chain: str, match: list[str]) -> None:
    """Наш jump должен стоять первым в OUTPUT (иначе ufw/другие цепочки могут принять пакет раньше)."""
    rule = [*match, "-j", chain]
    listing = run([binary, "-w", "5", "-S", "OUTPUT"]).stdout.splitlines()
    rules = [l for l in listing if l.startswith("-A OUTPUT")]
    wanted = "-A OUTPUT " + " ".join(rule)
    if rules and rules[0] == wanted:
        return
    while run([binary, "-w", "5", "-C", "OUTPUT", *rule], check=False).returncode == 0:
        run([binary, "-w", "5", "-D", "OUTPUT", *rule])
    run([binary, "-w", "5", "-I", "OUTPUT", "1", *rule])
    log(f"{binary.rsplit('/', 1)[-1]}: jump to {chain} (re)inserted first in OUTPUT")


def apply(data: dict, dry_run: bool = False) -> None:
    v4, v6 = ruleset_v4(data), ruleset_v6()
    if dry_run:
        print(v4); print(v6)
        return
    run([IPT, "-w", "5", "-N", CHAIN], check=False)
    run([IPT, "-w", "5", "-N", CHAIN_CLIENTS], check=False)
    run([IP6T, "-w", "5", "-N", CHAIN], check=False)
    run([IPT + "-restore", "--noflush", "-w", "5"], input_text=v4)    # одна атомарная замена содержимого цепочек
    run([IP6T + "-restore", "--noflush", "-w", "5"], input_text=v6)
    ensure_first_jump(IPT, CHAIN_CLIENTS, [])                    # inserted first so that KEYBROKER ends up at position 1
    ensure_first_jump(IPT, CHAIN, ["-o", "lo", "-p", "tcp"])
    ensure_first_jump(IP6T, CHAIN, ["-o", "lo", "-p", "tcp"])
    log(f"applied: {len(data['lanes'])} lane(s), admin {ADMIN_PORT}, default REJECT {LANE_RANGE[0]}-{LANE_RANGE[1]}")


def reload_broker() -> None:
    if subprocess.run(["systemctl", "is-active", "--quiet", BROKER_UNIT]).returncode == 0:
        subprocess.run(["systemctl", "reload", BROKER_UNIT], check=False)
        log("broker reloaded (SIGHUP)")


def listener_uid(port: int) -> int | None:
    """uid процесса, слушающего 127.0.0.1:port (по /proc/net/tcp), или None."""
    want = f"0100007F:{port:04X}"
    for line in Path("/proc/net/tcp").read_text().splitlines()[1:]:
        f = line.split()
        if f[1] == want and f[3] == "0A":
            return int(f[7])
    return None


def probe(port: int) -> str:
    s = socket.socket()
    s.settimeout(2)
    try:
        rc = s.connect_ex(("127.0.0.1", port))
    finally:
        s.close()
    if rc == 0:
        return "open"
    if rc in (errno.ECONNREFUSED, errno.ECONNRESET):
        return "refused"
    return f"error({errno.errorcode.get(rc, rc)})"


def probe_as(user: str, port: int) -> str:
    code = ("import socket,errno\ns=socket.socket();s.settimeout(2);rc=s.connect_ex(('127.0.0.1',%d))\n"
            "print('open' if rc==0 else ('refused' if rc in (errno.ECONNREFUSED, errno.ECONNRESET) else 'error(%%s)' %% errno.errorcode.get(rc, rc)))" % port)
    r = subprocess.run(["runuser", "-u", user, "--", "/usr/bin/python3", "-c", code], capture_output=True, text=True)
    return (r.stdout or r.stderr).strip()[-40:]


def with_lock(fn):
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return fn()


def cmd_add(a) -> None:
    entry = pwd.getpwnam(a.client)
    if a.rpm is not None and a.rpm <= 0 or a.elevenlabs_chars is not None and a.elevenlabs_chars <= 0:
        sys.exit("limits must be positive integers")

    def do():
        data = load()
        lanes = data["lanes"]
        if any(l["client"] == a.client for l in lanes):
            sys.exit(f"lane for {a.client} already exists: use remove first")
        used = {int(l["port"]) for l in lanes}
        port = a.port or next((p for p in PORT_RANGE if p not in used), None)
        if port is None or port in used or port == ADMIN_PORT or port not in PORT_RANGE:
            sys.exit(f"port must be free and within {PORT_RANGE.start}-{PORT_RANGE[-1]}")
        holder = listener_uid(port)
        if holder is not None and holder != pwd.getpwnam(BROKER_USER).pw_uid:
            sys.exit(f"port {port} is held by uid {holder}, refusing")
        lane = {"client": a.client, "uid": entry.pw_uid, "port": port, "limits": {}}
        if a.elevenlabs_chars:
            lane["limits"]["elevenlabs_chars_month"] = a.elevenlabs_chars
        if a.rpm:
            lane["limits"]["rpm"] = a.rpm
        if a.restrict_loopback:
            lane["restrict_loopback"] = True
            lane["loopback_allow"] = DEFAULT_LOOPBACK_ALLOW
        lanes.append(lane)
        apply(data)
        save(data)
        reload_broker()
        print(json.dumps(lane))
    with_lock(do)


def cmd_remove(a) -> None:
    def do():
        data = load()
        before = len(data["lanes"])
        data["lanes"] = [l for l in data["lanes"] if l["client"] != a.client]
        if len(data["lanes"]) == before:
            sys.exit(f"no lane for {a.client}")
        apply(data)   # REJECT на диапазон закрывает порт для всех, пока брокер не снимет слушатель
        save(data)
        reload_broker()
        log(f"removed {a.client}")
    with_lock(do)


def cmd_status(a) -> int:
    data = load()
    broker_uid = pwd.getpwnam(BROKER_USER).pw_uid
    problems = 0
    active = subprocess.run(["systemctl", "is-active", BROKER_UNIT], capture_output=True, text=True).stdout.strip()
    print(f"broker: {active} | admin {ADMIN_PORT}: {probe(ADMIN_PORT)} (uid {listener_uid(ADMIN_PORT)})")
    first = next((l for l in run([IPT, "-w", "5", "-S", "OUTPUT"]).stdout.splitlines() if l.startswith("-A OUTPUT")), "")
    jump_ok = first == f"-A OUTPUT -o lo -p tcp -j {CHAIN}"
    chain = run([IPT, "-w", "5", "-S", CHAIN]).stdout
    default_ok = f"--dport {LANE_RANGE[0]}:{LANE_RANGE[1]} -j REJECT" in chain
    print(f"chain {CHAIN}: jump first={'ok' if jump_ok else 'NO'} default-reject={'ok' if default_ok else 'NO'}")
    problems += (not jump_ok) + (not default_ok)
    for lane in data["lanes"]:
        uid, port = int(lane["uid"]), int(lane["port"])
        rule_ok = f"--dport {port} -m owner --uid-owner {uid} -j RETURN" in chain
        holder = listener_uid(port)
        listen = "broker" if holder == broker_uid else ("none" if holder is None else f"FOREIGN uid {holder}")
        bad = (not rule_ok) or (holder not in (None, broker_uid)) or (active == "active" and holder is None)
        problems += bad
        print(f"{lane['client']:14} uid={uid:<6} port={port} rules={'ok' if rule_ok else 'MISSING'} listener={listen} "
              f"limits={lane.get('limits') or {}} restrict_loopback={bool(lane.get('restrict_loopback'))}")
    return 1 if problems else 0


def cmd_check(a) -> int:
    data = load()
    mine = next((l for l in data["lanes"] if l["client"] == a.client), None)
    if not mine:
        sys.exit(f"no lane for {a.client}")
    # Right after `add` the broker is still reloading (SIGHUP is asynchronous): wait briefly for the listener.
    deadline = time.monotonic() + 15
    while probe(int(mine["port"])) != "open" and time.monotonic() < deadline:
        time.sleep(0.5)
    if probe(int(mine["port"])) != "open":
        print(f"broker is not listening on {mine['port']} — start it first, probes would be meaningless")
        return 1
    expect = {f"{a.client} -> own port {mine['port']}": ("open", probe_as(a.client, int(mine["port"]))),
              f"{a.client} -> admin {ADMIN_PORT}": ("refused", probe_as(a.client, ADMIN_PORT))}
    for other in data["lanes"]:
        if other["client"] != a.client:
            expect[f"{a.client} -> {other['client']} port {other['port']}"] = ("refused", probe_as(a.client, int(other["port"])))
    rc = 0
    for label, (want, got) in expect.items():
        ok = got == want
        rc |= (not ok)
        print(f"{'PASS' if ok else 'FAIL'} {label}: {got}")
    return rc


def main() -> None:
    if os.geteuid() != 0:
        sys.exit("root required")
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("add"); p.add_argument("--client", required=True); p.add_argument("--port", type=int)
    p.add_argument("--elevenlabs-chars", type=int); p.add_argument("--rpm", type=int); p.add_argument("--restrict-loopback", action="store_true")
    p = sub.add_parser("remove"); p.add_argument("--client", required=True)
    p = sub.add_parser("apply"); p.add_argument("--dry-run", action="store_true")
    sub.add_parser("status")
    p = sub.add_parser("check"); p.add_argument("--client", required=True)
    a = ap.parse_args()
    if a.cmd == "apply":
        with_lock(lambda: apply(load(), dry_run=a.dry_run))
    elif a.cmd in ("status", "check"):
        sys.exit({"status": cmd_status, "check": cmd_check}[a.cmd](a))
    else:
        {"add": cmd_add, "remove": cmd_remove}[a.cmd](a)


if __name__ == "__main__":
    main()
