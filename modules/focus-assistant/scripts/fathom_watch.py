#!/usr/bin/env python3
"""Silent-unless-new Fathom watcher for Hermes no-agent cron jobs."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser().resolve()


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def _load_runtime(home: Path):
    plugin = home / "plugins" / "focus_assistant"
    if not plugin.is_dir():
        raise RuntimeError("focus_assistant plugin is not installed")
    sys.path.insert(0, str(plugin.parent))
    from focus_assistant import fathom, ledger
    return fathom, ledger


def _state_path(home: Path) -> Path:
    return home / "focus" / "fathom_watch_state.json"


def _read_state(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "processed": [], "last_error_notice_at": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"version": 1, "processed": []}
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "processed": []}


def _write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".fathom-watch.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _created_after(hours: int) -> str:
    dt = datetime.now(timezone.utc) - timedelta(hours=hours)
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _summary_excerpt(text: str, limit: int = 700) -> str:
    clean = " ".join(str(text or "").replace("#", " ").split())
    return clean[:limit] + ("…" if len(clean) > limit else "")


def run(*, baseline: bool = False, smoke: bool = False) -> str:
    home = _home()
    _load_env(home / ".env")
    fathom, ledger = _load_runtime(home)
    state_file = _state_path(home)
    lock_path = state_file.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = _read_state(state_file)
        meetings = fathom.list_meetings(created_after=_created_after(72), include_action_items=True, max_pages=3)
        ids = [str(m.get("recording_id")) for m in meetings if m.get("recording_id")]
        if smoke:
            return json.dumps({"ok": True, "meetings_seen": len(ids), "processed": len(state.get("processed") or [])}, ensure_ascii=False)
        if baseline or not state_file.exists():
            state["processed"] = ids[-200:]
            state["updated_at"] = datetime.now(timezone.utc).isoformat()
            _write_state(state_file, state)
            return ""

        processed = set(str(x) for x in state.get("processed") or [])
        fresh = [m for m in reversed(meetings) if str(m.get("recording_id")) not in processed]
        if not fresh:
            return ""

        messages = []
        for meeting in fresh[:3]:
            rid = str(meeting.get("recording_id"))
            title = str(meeting.get("meeting_title") or meeting.get("title") or "Встреча")[:220]
            created_at = str(meeting.get("created_at") or "")
            try:
                summary = fathom.get_summary(rid)
            except Exception:
                summary = ""
            action_lines = []
            for index, item in enumerate(meeting.get("action_items") or [], start=1):
                if not isinstance(item, dict):
                    continue
                description = str(item.get("description") or "").strip()[:500]
                if not description:
                    continue
                assignee = item.get("assignee") or {}
                assignee_name = str(assignee.get("name") or "").strip()[:160] if isinstance(assignee, dict) else ""
                kind = "commitment" if not assignee_name or "павел" in assignee_name.casefold() else "waiting"
                waiting_on = assignee_name if kind == "waiting" else None
                ledger.save_item(
                    {
                        "title": description,
                        "kind": kind,
                        "state": "candidate",
                        "owner_confirmed": False,
                        "confidence": "high",
                        "waiting_on": waiting_on,
                        "source_type": "fathom",
                        "source_ref": f"fathom:{rid}",
                        "source_date": created_at,
                        "idempotency_key": f"fathom:{rid}:action:{index}",
                        "priority": 5,
                        "created_by": "fathom_watch",
                    }
                )
                action_lines.append(f"{len(action_lines)+1}. {description}" + (f" — {assignee_name}" if assignee_name else ""))
            block = [f"🎙 Fathom: завершена встреча «{title}»." ]
            excerpt = _summary_excerpt(summary)
            if excerpt:
                block.append(f"Итог: {excerpt}")
            if action_lines:
                block.append("Кандидаты задач:\n" + "\n".join(action_lines[:8]))
                block.append("Ответьте, какие пункты подтвердить, изменить или отклонить.")
            else:
                block.append("Явных action items Fathom не выделил.")
            messages.append("\n".join(block))
            processed.add(rid)

        state["processed"] = list(processed)[-200:]
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        _write_state(state_file, state)
        return "\n\n".join(messages)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    try:
        output = run(baseline=args.baseline, smoke=args.smoke)
    except Exception as exc:
        if args.smoke:
            print(json.dumps({"ok": False, "error": type(exc).__name__}, ensure_ascii=False))
            return 1
        return 0
    if output:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
