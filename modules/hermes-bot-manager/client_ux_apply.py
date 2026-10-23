#!/usr/bin/env python3
"""Client UX policy (Pavel, 2026-10-23). Run on the VPS as root.

  26-client-ux-apply.py --template [--apply]                 Forge template: config.yaml + SOUL.md
  26-client-ux-apply.py --owner X [--steer-only] [--apply] [--restart]

Full policy (clients + template):
  display.busy_input_mode = steer            follow-up messages land inside the running task (корректировка)
  display.platforms.telegram.tool_progress = off, busy_ack_detail = false   no tool kitchen in chat
  platforms.telegram.extra.allow_admin_from = tech admins only (owner id removed → owner is a plain user
      for slash commands: only user_allowed_commands + /help /whoami; plain chat unaffected)
  platforms.telegram.extra.user_allowed_commands = LEAN, command_menu.priority = LEAN_MENU
  SOUL.md: upsert «## Корректировки на ходу» + «## Только результат»
--steer-only (pavel): busy_input_mode + SOUL «Корректировки на ходу» only; admin rights and menu untouched.
Without --apply: print what would change. Config files keep their uid/gid/mode; a .bak-ux-<ts> copy is left next to them.
"""
import argparse
import json
import os
import pwd
import subprocess
import sys
import time
from pathlib import Path

import yaml

TEMPLATE_DIR = Path("/opt/proai-hermes-manager/templates/personal")
TECH_ADMIN_PLACEHOLDER = "@TECH_ADMIN_TELEGRAM_ID@"
LEAN_ALLOWED = ["start", "help", "new", "retry", "undo", "stop", "voice", "golos", "maton", "maton_cancel"]
LEAN_MENU = ["golos", "maton", "new", "voice", "retry", "undo", "help"]

SOUL_STEER = """## Корректировки на ходу
Пока ты работаешь над задачей, владелец может написать ещё — такое сообщение приходит внутри работы с пометкой OUT-OF-BAND USER MESSAGE. Это корректировка текущей задачи, а не новая задача: сразу учти её — поменяй план, цель или формат, не начинай отдельный ответ и не переспрашивай без нужды. Если корректировка отменяет то, что делаешь, — остановись и сделай по-новому. Несколько сообщений подряд — уточнения одного замысла: собери их вместе и выполни целиком. В итоговом ответе одной фразой покажи, что учёл («сделал с учётом …»).
"""
SOUL_RESULT = """## Только результат
Владельцу важен результат, а не внутренняя кухня. Не упоминай модели, провайдеров, названия инструментов, хуков и плагинов, лимиты токенов, переключения резервов и технические ошибки по именам. Если что-то не вышло — скажи просто, что именно и что делаешь дальше. Настройки моделей и интеграций ведёт команда ProAI: на просьбу «поменяй модель» ответь, что это делает поддержка, и продолжай помогать по сути.
"""

ap = argparse.ArgumentParser()
ap.add_argument("--template", action="store_true")
ap.add_argument("--owner")
ap.add_argument("--steer-only", action="store_true")
ap.add_argument("--apply", action="store_true")
ap.add_argument("--restart", action="store_true")
args = ap.parse_args()
if bool(args.template) == bool(args.owner):
    sys.exit("use --template or --owner X")

changes = []


def set_path(cfg, dotted, value):
    node = cfg
    keys = dotted.split(".")
    for k in keys[:-1]:
        node = node.setdefault(k, {})
        if not isinstance(node, dict):
            sys.exit(f"{dotted}: {k} is not a mapping")
    old = node.get(keys[-1])
    if old != value:
        changes.append(f"{dotted}: {old!r} -> {value!r}")
        node[keys[-1]] = value


def write_preserving(path: Path, text: str):
    st = path.stat()
    stamp = time.strftime("%Y%m%dT%H%M%S")
    backup = path.with_name(path.name + f".bak-ux-{stamp}")
    backup.write_bytes(path.read_bytes())
    os.chown(backup, st.st_uid, st.st_gid)
    os.chmod(backup, 0o600)
    tmp = path.with_name(path.name + ".tmp-ux")
    tmp.write_text(text, encoding="utf-8")
    os.chown(tmp, st.st_uid, st.st_gid)
    os.chmod(tmp, st.st_mode & 0o777)
    os.replace(tmp, path)


def upsert_section(soul: str, heading: str, body: str, before: str | None) -> str:
    """Replace the section with this heading, or insert it before `before` heading, else append."""
    lines = soul.split("\n")
    idx = [i for i, l in enumerate(lines) if l.strip() == heading]
    block = body.rstrip("\n").split("\n")
    if idx:
        start = idx[0]
        end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
        new = lines[:start] + block + [""] + lines[end:]
        if new != lines:
            changes.append(f"SOUL: section {heading!r} rewritten")
        return "\n".join(new)
    changes.append(f"SOUL: section {heading!r} added")
    if before:
        pos = next((i for i, l in enumerate(lines) if l.strip() == before), None)
        if pos is not None:
            return "\n".join(lines[:pos] + block + [""] + lines[pos:])
    return soul.rstrip("\n") + "\n\n" + body


def apply_config(cfg: dict, *, steer_only: bool, tech_admins: list):
    set_path(cfg, "display.busy_input_mode", "steer")
    if steer_only:
        return
    set_path(cfg, "display.platforms.telegram.tool_progress", "off")
    set_path(cfg, "display.platforms.telegram.busy_ack_detail", False)
    # The built-in steer confirmation is English-only ("⏩ Steered into current run."); clients get the
    # acknowledgement inside the answer instead (SOUL «Корректировки на ходу»).
    set_path(cfg, "display.platforms.telegram.busy_steer_ack_enabled", False)
    extra = cfg.setdefault("platforms", {}).setdefault("telegram", {}).setdefault("extra", {})
    set_path(cfg, "platforms.telegram.extra.allow_admin_from", tech_admins)
    set_path(cfg, "platforms.telegram.extra.user_allowed_commands", LEAN_ALLOWED)
    menu = extra.setdefault("command_menu", {})
    old = menu.get("priority")
    if old != LEAN_MENU:
        changes.append(f"platforms.telegram.extra.command_menu.priority: {old!r} -> {LEAN_MENU!r}")
        menu["priority"] = LEAN_MENU
    menu.setdefault("priority_mode", "replace")


def process(cfg_path: Path, soul_path: Path, *, steer_only: bool, tech_admins: list):
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    apply_config(cfg, steer_only=steer_only, tech_admins=tech_admins)
    soul = soul_path.read_text(encoding="utf-8")
    new_soul = upsert_section(soul, "## Корректировки на ходу", SOUL_STEER, before="## Владелец и границы")
    if not steer_only:
        new_soul = upsert_section(new_soul, "## Только результат", SOUL_RESULT, before="## Владелец и границы")
    for c in changes:
        print("  ", c)
    if not changes:
        print("   (no changes)")
    if args.apply:
        write_preserving(cfg_path, yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False, width=1000))
        if new_soul != soul:
            write_preserving(soul_path, new_soul)
        print("   written")


if args.template:
    print("[template]")
    process(TEMPLATE_DIR / "config.yaml", TEMPLATE_DIR / "SOUL.md", steer_only=False,
            tech_admins=[TECH_ADMIN_PLACEHOLDER])
    sys.exit()

owner = args.owner
entry = pwd.getpwnam(owner)
hermes = Path(entry.pw_dir) / ".hermes"
cfg0 = yaml.safe_load((hermes / "config.yaml").read_text(encoding="utf-8"))
extra0 = cfg0.get("platforms", {}).get("telegram", {}).get("extra", {})
owner_ids = {str(x) for x in (extra0.get("business_owner_ids") or [])}
tech_admins = [str(x) for x in (extra0.get("allow_admin_from") or []) if str(x) not in owner_ids]
print(f"[{owner}] owner_ids={sorted(owner_ids)} tech_admins={tech_admins} steer_only={args.steer_only}")
if not args.steer_only and not tech_admins:
    sys.exit(f"[{owner}] no tech admin besides the owner — refusing to drop the owner's admin rights")
process(hermes / "config.yaml", hermes / "SOUL.md", steer_only=args.steer_only, tech_admins=tech_admins)

if args.apply and args.restart:
    unit = f"{owner}-hermes.service"
    subprocess.run(["systemctl", "restart", unit], check=True)
    time.sleep(25)
    state = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True).stdout.strip()
    gw = json.loads((hermes / "gateway_state.json").read_text(encoding="utf-8"))
    tg = gw.get("platforms", {}).get("telegram", {}).get("state")
    errors = subprocess.run(["journalctl", "-u", unit, "--since", "-40s", "-p", "err", "--no-pager", "-q"],
                            capture_output=True, text=True).stdout.strip()
    print(f"[{owner}] restarted: service={state} telegram={tg} errors={'none' if not errors else errors[-300:]}")
