"""Durable personal focus ledger with no agent-execution semantics."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import sqlite3
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional
from zoneinfo import ZoneInfo

import yaml

from hermes_constants import get_hermes_home


MOSCOW = ZoneInfo("Europe/Moscow")

KINDS = {"task", "commitment", "waiting", "idea"}
STATES = {"candidate", "active", "waiting", "scheduled", "done", "cancelled"}
CONFIDENCE = {"low", "medium", "high", "confirmed"}
GOAL_STATUSES = {"active", "paused", "completed", "archived"}

LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS focus_items (
    id               TEXT PRIMARY KEY,
    title            TEXT NOT NULL,
    body             TEXT,
    priority         INTEGER NOT NULL DEFAULT 0,
    kind             TEXT NOT NULL,
    state            TEXT NOT NULL,
    project          TEXT,
    goal_id          TEXT,
    next_action      TEXT,
    due_at           TEXT,
    follow_up_at     TEXT,
    waiting_on       TEXT,
    source_type      TEXT,
    source_ref       TEXT,
    source_date      TEXT,
    confidence       TEXT NOT NULL DEFAULT 'medium',
    owner_confirmed  INTEGER NOT NULL DEFAULT 0,
    snoozed_until    TEXT,
    idempotency_key  TEXT UNIQUE,
    created_by       TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    completed_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_focus_items_state_due
    ON focus_items(state, due_at, follow_up_at);
CREATE INDEX IF NOT EXISTS idx_focus_items_goal
    ON focus_items(goal_id, state);

CREATE TABLE IF NOT EXISTS focus_nudges (
    item_id          TEXT NOT NULL,
    cadence          TEXT NOT NULL,
    state_hash       TEXT NOT NULL,
    prompted_at      TEXT NOT NULL,
    prompt_count     INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (item_id, cadence)
);

CREATE TABLE IF NOT EXISTS focus_meta (
    key              TEXT PRIMARY KEY,
    value            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS focus_evidence (
    item_id TEXT PRIMARY KEY,
    evidence_json TEXT NOT NULL,
    FOREIGN KEY(item_id) REFERENCES focus_items(id)
);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _clean(value: Any, *, limit: int = 1000) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def _normalize_datetime(value: Any, *, end_of_day: bool = False) -> Optional[str]:
    text = _clean(value, limit=64)
    if text is None:
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            day = date.fromisoformat(text)
            local_time = time(23, 59) if end_of_day else time(9, 0)
            parsed = datetime.combine(day, local_time, tzinfo=MOSCOW)
        else:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=MOSCOW)
        return _iso(parsed)
    except ValueError as exc:
        raise ValueError(f"invalid ISO date/time: {text}") from exc


def _parse_stored(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _focus_dir() -> Path:
    path = get_hermes_home() / "focus"
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def ledger_path() -> Path:
    return _focus_dir() / "ledger.db"


@contextlib.contextmanager
def _write_txn(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def _connect(*, migrate: bool = True) -> sqlite3.Connection:
    path = ledger_path()
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(LEDGER_SCHEMA)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    if migrate:
        _migrate_legacy_kanban(conn)
    return conn


def _legacy_board_path() -> Path:
    return get_hermes_home() / "kanban" / "boards" / "focus" / "kanban.db"


def _migrate_legacy_kanban(conn: sqlite3.Connection) -> int:
    marker = conn.execute(
        "SELECT value FROM focus_meta WHERE key='legacy_kanban_migration_v1'"
    ).fetchone()
    if marker is not None:
        return 0
    source_path = _legacy_board_path()
    migrated = 0
    if source_path.is_file():
        source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
        source.row_factory = sqlite3.Row
        try:
            tables = {
                row[0]
                for row in source.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if {"tasks", "focus_items"}.issubset(tables):
                rows = source.execute(
                    """
                    SELECT t.id, t.title, t.body, t.priority, f.kind, f.state,
                           f.project, f.goal_id, f.next_action, f.due_at,
                           f.follow_up_at, f.waiting_on, f.source_type,
                           f.source_ref, f.source_date, f.confidence,
                           f.owner_confirmed, f.snoozed_until,
                           t.idempotency_key, t.created_by, f.created_at,
                           f.updated_at, f.completed_at
                      FROM tasks t
                      JOIN focus_items f ON f.task_id=t.id
                     ORDER BY f.created_at
                    """
                ).fetchall()
                with _write_txn(conn):
                    for row in rows:
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO focus_items (
                                id,title,body,priority,kind,state,project,goal_id,
                                next_action,due_at,follow_up_at,waiting_on,
                                source_type,source_ref,source_date,confidence,
                                owner_confirmed,snoozed_until,idempotency_key,
                                created_by,created_at,updated_at,completed_at
                            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            """,
                            tuple(row),
                        )
                        migrated += int(conn.execute("SELECT changes()").fetchone()[0])
        finally:
            source.close()
    with _write_txn(conn):
        conn.execute(
            "INSERT OR REPLACE INTO focus_meta(key,value) VALUES(?,?)",
            (
                "legacy_kanban_migration_v1",
                json.dumps(
                    {
                        "migrated_at": _iso(_now()),
                        "source": str(source_path),
                        "items": migrated,
                    },
                    ensure_ascii=False,
                ),
            ),
        )
    return migrated


def initialize() -> dict[str, Any]:
    conn = _connect()
    try:
        count = int(conn.execute("SELECT COUNT(*) FROM focus_items").fetchone()[0])
        marker = conn.execute(
            "SELECT value FROM focus_meta WHERE key='legacy_kanban_migration_v1'"
        ).fetchone()
    finally:
        conn.close()
    migrated = 0
    if marker:
        try:
            migrated = int(json.loads(marker["value"]).get("items") or 0)
        except (TypeError, ValueError, json.JSONDecodeError):
            migrated = 0
    return {
        "backend": "sqlite",
        "path": str(ledger_path()),
        "items": count,
        "migrated_from_kanban": migrated,
    }


def _state_hash(row: sqlite3.Row) -> str:
    fields = (
        row["state"], row["kind"], row["title"], row["priority"],
        row["next_action"], row["due_at"], row["follow_up_at"],
        row["waiting_on"], row["goal_id"], row["snoozed_until"],
    )
    payload = json.dumps(fields, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _fetch_item(conn: sqlite3.Connection, item_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM focus_items WHERE id=?", (item_id,)).fetchone()


def _row_to_item(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"], "title": row["title"], "kind": row["kind"],
        "state": row["state"], "priority": int(row["priority"] or 0),
        "project": row["project"], "goal_id": row["goal_id"],
        "next_action": row["next_action"], "due_at": row["due_at"],
        "follow_up_at": row["follow_up_at"], "waiting_on": row["waiting_on"],
        "source_type": row["source_type"], "source_ref": row["source_ref"],
        "source_date": row["source_date"], "confidence": row["confidence"],
        "owner_confirmed": bool(row["owner_confirmed"]),
        "snoozed_until": row["snoozed_until"], "created_at": row["created_at"],
        "updated_at": row["updated_at"], "completed_at": row["completed_at"],
    }


def _validate_enum(name: str, value: Any, allowed: set[str], default: str) -> str:
    normalized = str(value or default).strip().lower()
    if normalized not in allowed:
        raise ValueError(f"{name} must be one of {sorted(allowed)}")
    return normalized


def _new_item_id(conn: sqlite3.Connection) -> str:
    for _ in range(20):
        item_id = f"f_{secrets.token_hex(4)}"
        if _fetch_item(conn, item_id) is None:
            return item_id
    raise RuntimeError("could not allocate focus item id")


def save_item(args: dict[str, Any]) -> dict[str, Any]:
    title = _clean(args.get("title"), limit=300)
    if not title:
        raise ValueError("title is required")
    item_id = _clean(args.get("item_id"), limit=64)
    if item_id:
        return update_item({**args, "item_id": item_id})
    kind = _validate_enum("kind", args.get("kind"), KINDS, "task")
    if "owner_confirmed" in args and type(args["owner_confirmed"]) is not bool:
        raise ValueError("owner_confirmed must be a boolean")
    owner_confirmed = args.get("owner_confirmed", False)
    state = _validate_enum(
        "state", args.get("state"), STATES,
        "active" if owner_confirmed else "candidate",
    )
    if state in {"active", "waiting", "scheduled"} and not owner_confirmed:
        raise ValueError("owner_confirmed=true is required before activating a task")
    confidence = _validate_enum(
        "confidence", args.get("confidence"), CONFIDENCE,
        "confirmed" if owner_confirmed else "medium",
    )
    priority = max(-100, min(100, int(args.get("priority") or 0)))
    idempotency_key = _clean(args.get("idempotency_key"), limit=240)
    now = _iso(_now())
    conn = _connect()
    try:
        if idempotency_key:
            existing = conn.execute(
                "SELECT * FROM focus_items WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing:
                return {"created": False, "deduplicated": True, "item": _row_to_item(existing)}
        item_id = _new_item_id(conn)
        values = (
            item_id, title, _clean(args.get("body"), limit=4000), priority,
            kind, state, _clean(args.get("project"), limit=200),
            _clean(args.get("goal_id"), limit=100),
            _clean(args.get("next_action"), limit=1000),
            _normalize_datetime(args.get("due_at"), end_of_day=True),
            _normalize_datetime(args.get("follow_up_at")),
            _clean(args.get("waiting_on"), limit=200),
            _clean(args.get("source_type"), limit=80),
            _clean(args.get("source_ref"), limit=500),
            _clean(args.get("source_date"), limit=64), confidence,
            1 if owner_confirmed else 0,
            _normalize_datetime(args.get("snoozed_until")), idempotency_key,
            _clean(args.get("created_by"), limit=80) or "focus_assistant",
            now, now, now if state == "done" else None,
        )
        with _write_txn(conn):
            conn.execute(
                """
                INSERT INTO focus_items (
                    id,title,body,priority,kind,state,project,goal_id,next_action,
                    due_at,follow_up_at,waiting_on,source_type,source_ref,
                    source_date,confidence,owner_confirmed,snoozed_until,
                    idempotency_key,created_by,created_at,updated_at,completed_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                values,
            )
        row = _fetch_item(conn, item_id)
        return {"created": True, "deduplicated": False, "item": _row_to_item(row)}
    finally:
        conn.close()


def update_item(args: dict[str, Any]) -> dict[str, Any]:
    item_id = _clean(args.get("item_id"), limit=64)
    if not item_id:
        raise ValueError("item_id is required")
    conn = _connect()
    try:
        current = _fetch_item(conn, item_id)
        if current is None:
            raise ValueError(f"unknown focus item: {item_id}")
        data = _row_to_item(current)
        title = _clean(args.get("title"), limit=300) or data["title"]
        body = _clean(args.get("body"), limit=4000) if "body" in args else current["body"]
        kind = _validate_enum("kind", args.get("kind"), KINDS, data["kind"])
        state = _validate_enum("state", args.get("state"), STATES, data["state"])
        if "owner_confirmed" in args and type(args["owner_confirmed"]) is not bool:
            raise ValueError("owner_confirmed must be a boolean")
        owner_confirmed = args.get("owner_confirmed", data["owner_confirmed"])
        if state in {"active", "waiting", "scheduled"} and not owner_confirmed:
            raise ValueError("owner_confirmed=true is required before activating a task")
        confidence = _validate_enum(
            "confidence", args.get("confidence"), CONFIDENCE, data["confidence"]
        )
        priority = max(-100, min(100, int(args.get("priority", data["priority"]))))
        now = _iso(_now())

        def selected(name: str, current_value: Any, *, dt: bool = False, end_of_day: bool = False):
            if name not in args:
                return current_value
            if args.get(name) in (None, ""):
                return None
            return _normalize_datetime(args.get(name), end_of_day=end_of_day) if dt else _clean(args.get(name), limit=1000)

        completed_at = data["completed_at"]
        if state == "done" and not completed_at:
            completed_at = now
        elif state != "done":
            completed_at = None
        values = {
            "project": selected("project", data["project"]),
            "goal_id": selected("goal_id", data["goal_id"]),
            "next_action": selected("next_action", data["next_action"]),
            "due_at": selected("due_at", data["due_at"], dt=True, end_of_day=True),
            "follow_up_at": selected("follow_up_at", data["follow_up_at"], dt=True),
            "waiting_on": selected("waiting_on", data["waiting_on"]),
            "source_type": selected("source_type", data["source_type"]),
            "source_ref": selected("source_ref", data["source_ref"]),
            "source_date": selected("source_date", data["source_date"]),
            "snoozed_until": selected("snoozed_until", data["snoozed_until"], dt=True),
        }
        with _write_txn(conn):
            conn.execute(
                """
                UPDATE focus_items SET
                    title=?,body=?,priority=?,kind=?,state=?,project=?,goal_id=?,
                    next_action=?,due_at=?,follow_up_at=?,waiting_on=?,source_type=?,
                    source_ref=?,source_date=?,confidence=?,owner_confirmed=?,
                    snoozed_until=?,updated_at=?,completed_at=? WHERE id=?
                """,
                (
                    title, body, priority, kind, state, values["project"],
                    values["goal_id"], values["next_action"], values["due_at"],
                    values["follow_up_at"], values["waiting_on"],
                    values["source_type"], values["source_ref"],
                    values["source_date"], confidence,
                    1 if owner_confirmed else 0, values["snoozed_until"],
                    now, completed_at, item_id,
                ),
            )
        return {"updated": True, "item": _row_to_item(_fetch_item(conn, item_id))}
    finally:
        conn.close()


def list_items(args: dict[str, Any]) -> dict[str, Any]:
    requested_states = args.get("states")
    if isinstance(requested_states, str):
        requested_states = [requested_states]
    if requested_states is None and args.get("state"):
        requested_states = [args.get("state")]
    states = [_validate_enum("state", value, STATES, "active") for value in (requested_states or [])]
    include_done = bool(args.get("include_done", False))
    limit = max(1, min(100, int(args.get("limit") or 50)))
    query = "SELECT * FROM focus_items WHERE 1=1"
    params: list[Any] = []
    if states:
        query += " AND state IN (%s)" % ",".join("?" for _ in states)
        params.extend(states)
    elif not include_done:
        query += " AND state NOT IN ('done','cancelled')"
    for field in ("kind", "project", "goal_id"):
        value = _clean(args.get(field), limit=200)
        if value:
            if field == "kind":
                value = _validate_enum("kind", value, KINDS, "task")
            query += f" AND {field}=?"
            params.append(value)
    due_before = _normalize_datetime(args.get("due_before"), end_of_day=True)
    if due_before:
        query += " AND due_at IS NOT NULL AND due_at<=?"
        params.append(due_before)
    query += " ORDER BY priority DESC, COALESCE(due_at,follow_up_at,'9999') ASC, created_at ASC LIMIT ?"
    params.append(limit)
    conn = _connect()
    try:
        rows = conn.execute(query, tuple(params)).fetchall()
        counts = {row["state"]: int(row["n"]) for row in conn.execute("SELECT state,COUNT(*) n FROM focus_items GROUP BY state")}
        return {"items": [_row_to_item(row) for row in rows], "count": len(rows), "counts": counts, "backend": "sqlite"}
    finally:
        conn.close()


def attention(args: dict[str, Any]) -> dict[str, Any]:
    cadence = str(args.get("cadence") or "morning").strip().lower()
    if cadence not in {"morning", "evening", "weekly", "event"}:
        raise ValueError("cadence must be morning, evening, weekly, or event")
    now_text = _normalize_datetime(args.get("now")) if args.get("now") else _iso(_now())
    now = _parse_stored(now_text) or _now()
    max_items = max(1, min(20, int(args.get("max_items") or 8)))
    mark_prompted = bool(args.get("mark_prompted", True))
    horizon_hours = {"morning": 36, "evening": 96, "weekly": 24 * 8, "event": 24}[cadence]
    cooldown_hours = {"morning": 18, "evening": 8, "weekly": 24 * 6, "event": 20}[cadence]
    horizon = now + timedelta(hours=horizon_hours)
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM focus_items WHERE state NOT IN ('done','cancelled') ORDER BY priority DESC, COALESCE(due_at,follow_up_at,'9999') ASC").fetchall()
        candidates: list[tuple[int, datetime, sqlite3.Row, str]] = []
        suppressed = 0
        for row in rows:
            snoozed = _parse_stored(row["snoozed_until"])
            if snoozed and snoozed > now:
                continue
            due = _parse_stored(row["due_at"])
            follow = _parse_stored(row["follow_up_at"])
            reason = None
            rank = 50
            when = due or follow or datetime.max.replace(tzinfo=timezone.utc)
            if due and due < now:
                reason, rank = "просрочено", 100
            elif follow and follow <= now:
                reason, rank = "пора проверить ожидание", 95
            elif due and due <= horizon:
                reason, rank = "срок близко", 85
            elif follow and follow <= horizon:
                reason, rank = "приближается контроль", 80
            elif row["state"] == "candidate" and cadence in {"evening", "weekly"}:
                reason, rank = "нужно подтвердить или отклонить", 65
            elif row["state"] == "active" and int(row["priority"] or 0) > 0:
                reason, rank = "активный приоритет", 55
            elif cadence == "weekly":
                reason, rank = "незакрытый контур", 40
            if not reason:
                continue
            digest = _state_hash(row)
            previous = conn.execute("SELECT state_hash,prompted_at FROM focus_nudges WHERE item_id=? AND cadence=?", (row["id"], cadence)).fetchone()
            if previous and previous["state_hash"] == digest:
                prompted_at = _parse_stored(previous["prompted_at"])
                if prompted_at and prompted_at > now - timedelta(hours=cooldown_hours):
                    suppressed += 1
                    continue
            candidates.append((rank, when, row, reason))
        candidates.sort(key=lambda item: (-item[0], -int(item[2]["priority"] or 0), item[1]))
        chosen = candidates[:max_items]
        if mark_prompted and chosen:
            with _write_txn(conn):
                for _rank, _when, row, _reason in chosen:
                    conn.execute(
                        """INSERT INTO focus_nudges(item_id,cadence,state_hash,prompted_at,prompt_count)
                           VALUES(?,?,?,?,1)
                           ON CONFLICT(item_id,cadence) DO UPDATE SET
                           state_hash=excluded.state_hash,prompted_at=excluded.prompted_at,
                           prompt_count=focus_nudges.prompt_count+1""",
                        (row["id"], cadence, _state_hash(row), now_text),
                    )
        items = []
        for _rank, _when, row, reason in chosen:
            item = _row_to_item(row)
            item["attention_reason"] = reason
            items.append(item)
        return {"cadence": cadence, "now": now_text, "items": items, "count": len(items), "suppressed_unchanged": suppressed, "marked_prompted": mark_prompted}
    finally:
        conn.close()


def _goals_path() -> Path:
    return _focus_dir() / "goals.yaml"


def _default_goals() -> dict[str, Any]:
    return {
        "version": 2, "timezone": "Europe/Moscow",
        "status": "needs_owner_input", "updated_at": None,
        "confirmed_goals": [], "weekly_outcomes": [],
        "guardrails": [
            "Не считать кандидаты из переписки подтверждёнными целями.",
            "Не создавать цели без явного подтверждения Павла.",
        ],
    }


def _read_goals() -> dict[str, Any]:
    path = _goals_path()
    if not path.exists():
        data = _default_goals()
        _write_goals(data)
        return data
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    data = _default_goals()
    if isinstance(loaded, dict):
        data.update(loaded)
    if not isinstance(data.get("confirmed_goals"), list):
        data["confirmed_goals"] = []
    return data


def _write_goals(data: dict[str, Any]) -> None:
    path = _goals_path()
    fd, tmp_name = tempfile.mkstemp(prefix=".goals.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _goal_id(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:32]
    digest = hashlib.sha256(title.encode("utf-8")).hexdigest()[:8]
    return f"g-{slug or 'goal'}-{digest}"


def goals(args: dict[str, Any]) -> dict[str, Any]:
    action = str(args.get("action") or "list").strip().lower()
    if action not in {"list", "upsert", "archive"}:
        raise ValueError("action must be list, upsert, or archive")
    data = _read_goals()
    current = data["confirmed_goals"]
    if action == "list":
        return {"status": data.get("status"), "updated_at": data.get("updated_at"), "goals": current}
    if not bool(args.get("confirmed_by_owner", False)):
        raise ValueError("confirmed_by_owner=true is required to change confirmed goals")
    goal_id = _clean(args.get("goal_id"), limit=100)
    if action == "archive":
        if not goal_id:
            raise ValueError("goal_id is required")
        found = False
        for goal in current:
            if isinstance(goal, dict) and goal.get("id") == goal_id:
                goal["status"] = "archived"
                goal["updated_at"] = _iso(_now())
                found = True
        if not found:
            raise ValueError(f"unknown goal: {goal_id}")
    else:
        title = _clean(args.get("title"), limit=300)
        if not title:
            raise ValueError("title is required")
        goal_id = goal_id or _goal_id(title)
        status = _validate_enum("status", args.get("status"), GOAL_STATUSES, "active")
        goal = {
            "id": goal_id, "title": title,
            "horizon": _clean(args.get("horizon"), limit=100),
            "metric": _clean(args.get("metric"), limit=300),
            "target": _clean(args.get("target"), limit=300),
            "due_date": _clean(args.get("due_date"), limit=32),
            "why": _clean(args.get("why"), limit=1000),
            "next_action": _clean(args.get("next_action"), limit=1000),
            "status": status, "confirmed_at": _iso(_now()),
            "updated_at": _iso(_now()),
        }
        replaced = False
        for index, existing in enumerate(current):
            if isinstance(existing, dict) and existing.get("id") == goal_id:
                goal["confirmed_at"] = existing.get("confirmed_at") or goal["confirmed_at"]
                current[index] = goal
                replaced = True
                break
        if not replaced:
            active_count = len([item for item in current if isinstance(item, dict) and item.get("status") == "active"])
            if active_count >= 5:
                raise ValueError("no more than 5 active confirmed goals are allowed")
            current.append(goal)
    data["confirmed_goals"] = current
    data["updated_at"] = _iso(_now())
    data["status"] = "active" if any(isinstance(goal, dict) and goal.get("status") == "active" for goal in current) else "needs_owner_input"
    _write_goals(data)
    return {"status": data["status"], "updated_at": data["updated_at"], "goals": current}


def json_result(callable_, args: dict[str, Any]) -> str:
    try:
        return json.dumps({"ok": True, **callable_(args)}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"ok": False, "error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False)
