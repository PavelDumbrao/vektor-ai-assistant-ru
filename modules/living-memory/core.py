from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

SCHEMA_VERSION = 1
VERSION = "0.1.0"
CELLS = {
    "identity", "communication", "preferences", "goals", "projects", "people",
    "decisions", "workflows", "expertise", "corrections", "boundaries",
    "content_text", "content_images", "content_video", "temporary_context",
}
SOURCE_KINDS = {"explicit", "correction", "pattern", "hypothesis"}
ACTIONS = {"add", "update", "merge", "archive", "forget", "noop"}
MANAGED_PREFIX = "[Hermes Living Memory - managed]"
ENTRY_DELIMITER = "\n§\n"
MAX_ITEM_CHARS = 360
MAX_SUMMARY_CHARS = 2400
MAX_OPERATIONS = 12

_SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b", re.I),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{12,}\b", re.I),
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b(?:api[_ -]?key|token|password|secret)\s*[:=]\s*\S{8,}", re.I),
    re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----", re.I),
]
_INJECTION_RE = re.compile(
    r"\b(ignore|disregard|override|forget)\b.{0,40}\b(previous|system|developer|instructions?|prompt)\b",
    re.I | re.S,
)
_FORGET_RE = re.compile(
    r"\b(забуд(?:ь|ьте|ем)?|не\s+запоминай|не\s+сохраняй|удали\s+из\s+памят|"
    r"forget\b|do\s+not\s+remember|don't\s+remember|remove\s+from\s+memory)\b",
    re.I,
)
_SENSITIVE_LABELS = {
    "health", "medical", "religion", "politics", "political", "sexuality", "sex_life",
    "criminal", "precise_address", "government_id", "financial_account", "credentials",
}
_SENSITIVE_TEXT_RE = re.compile(
    r"\b(health|medical|diagnos(?:is|ed)|disease|medication|pregnan(?:t|cy)|religio(?:n|us)|"
    r"christian|muslim|jewish|politic(?:s|al)|democrat|republican|liberal|conservative|"
    r"sexual(?:ity| orientation| life)|gay|lesbian|bisexual|criminal|conviction|"
    r"диагноз|болезн|заболеван|лекарств|беремен|здоровь|религи|христиан|мусульман|иуде|"
    r"политик|парти(?:я|и)|либерал|консерват|сексуал|ориентац|интимн|судим|преступ)\b",
    re.I,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def living_dir(home: Path) -> Path:
    return home / "living_memory"


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def _atomic_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    ensure_private_dir(path.parent)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        path.chmod(mode)
    finally:
        tmp.unlink(missing_ok=True)


def atomic_json(path: Path, value: Any) -> None:
    _atomic_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode())


def atomic_text(path: Path, value: str) -> None:
    _atomic_bytes(path, value.encode("utf-8"))


@contextlib.contextmanager
def file_lock(path: Path):
    import fcntl
    ensure_private_dir(path.parent)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    handle = os.fdopen(fd, "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def load_config(home: Path) -> dict[str, Any]:
    raw = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise RuntimeError("config_invalid")
    return raw


def owner_telegram_ids(config: dict[str, Any]) -> list[str]:
    extra = (((config.get("platforms") or {}).get("telegram") or {}).get("extra") or {})
    values = extra.get("business_owner_ids") or []
    return [str(v).strip() for v in values if str(v).strip()]


def default_state(owner: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "version": VERSION,
        "owner": owner,
        "updated_at": iso_now(),
        "memories": [],
        "tombstones": [],
    }


def load_state(home: Path, owner: str) -> dict[str, Any]:
    path = living_dir(home) / "state.json"
    if not path.exists():
        return default_state(owner)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError("living_memory_state_invalid")
    if str(data.get("owner")) != owner:
        raise RuntimeError("living_memory_owner_mismatch")
    if not isinstance(data.get("memories"), list) or not isinstance(data.get("tombstones"), list):
        raise RuntimeError("living_memory_state_invalid")
    return data


def save_state(home: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = iso_now()
    atomic_json(living_dir(home) / "state.json", state)


def load_cursor(home: Path) -> dict[str, Any]:
    path = living_dir(home) / "cursor.json"
    if not path.exists():
        return {"last_message_id": 0, "last_scan_at": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"last_message_id": 0, "last_scan_at": None}
    return {
        "last_message_id": max(0, int(data.get("last_message_id") or 0)),
        "last_scan_at": data.get("last_scan_at"),
    }


def save_cursor(home: Path, message_id: int) -> None:
    atomic_json(living_dir(home) / "cursor.json", {
        "last_message_id": max(0, int(message_id)),
        "last_scan_at": iso_now(),
    })


def _text_from_content(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return ""
        if s[:1] in "[{":
            try:
                parsed = json.loads(s)
            except Exception:
                return s
            return _text_from_content(parsed)
        return s
    if isinstance(value, list):
        parts = [_text_from_content(x) for x in value]
        return "\n".join(p for p in parts if p)
    if isinstance(value, dict):
        typ = str(value.get("type") or "")
        if typ in {"text", "input_text", "output_text"}:
            return str(value.get("text") or value.get("content") or "")
        for key in ("text", "content", "message"):
            if key in value:
                return _text_from_content(value[key])
        return ""
    return str(value)


def redact_text(text: str) -> str:
    out = text.replace("\x00", " ")
    for pat in _SECRET_PATTERNS:
        out = pat.sub("[REDACTED_SECRET]", out)
    # Avoid shipping giant opaque credential-like blobs to the curator.
    out = re.sub(r"\b[A-Za-z0-9+/=_-]{80,}\b", "[REDACTED_OPAQUE]", out)
    return out.strip()


def extract_interactions(
    home: Path,
    config: dict[str, Any],
    *,
    last_message_id: int = 0,
    initial_hours: int = 24,
) -> list[dict[str, Any]]:
    owner_ids = owner_telegram_ids(config)
    if not owner_ids:
        raise RuntimeError("living_memory_owner_telegram_id_missing")
    db = home / "state.db"
    if not db.is_file():
        return []
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=2)
    con.row_factory = sqlite3.Row
    try:
        params: list[Any] = list(owner_ids)
        owner_marks = ",".join("?" for _ in owner_ids)
        where_cursor = "m.id > ?"
        if last_message_id > 0:
            params.append(last_message_id)
        else:
            where_cursor = "m.timestamp >= ?"
            params.append(time.time() - initial_hours * 3600)
        sql = f"""
            SELECT m.id, m.session_id, m.role, m.content, m.timestamp,
                   COALESCE(m.display_kind, '') AS display_kind
            FROM messages m
            JOIN sessions s ON s.id = m.session_id
            WHERE s.source = 'telegram'
              AND s.chat_type = 'dm'
              AND CAST(s.user_id AS TEXT) IN ({owner_marks})
              AND m.role IN ('user','assistant')
              AND COALESCE(m.active,1) = 1
              AND COALESCE(m.display_kind,'') NOT IN ('internal_notification','hidden')
              AND {where_cursor}
            ORDER BY m.id ASC
        """
        rows = con.execute(sql, params).fetchall()
    finally:
        con.close()
    result: list[dict[str, Any]] = []
    for row in rows:
        text = redact_text(_text_from_content(row["content"]))
        if not text:
            continue
        result.append({
            "message_id": int(row["id"]),
            "session_id": str(row["session_id"]),
            "role": str(row["role"]),
            "timestamp": float(row["timestamp"] or 0),
            "text": text[:12000],
        })
    return result


def batch_interactions(
    messages: list[dict[str, Any]], *, max_chars: int = 70000, max_messages: int = 220
) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    size = 0
    for msg in messages:
        cost = len(msg.get("text") or "") + 120
        if current and (len(current) >= max_messages or size + cost > max_chars):
            batches.append(current)
            current = []
            size = 0
        current.append(msg)
        size += cost
    if current:
        batches.append(current)
    return batches


def transcript_for_model(messages: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for m in messages:
        stamp = datetime.fromtimestamp(m["timestamp"], tz=timezone.utc).isoformat() if m["timestamp"] else "unknown"
        lines.append(
            f"<message session={json.dumps(m['session_id'])} id={m['message_id']} role={m['role']} at={stamp}>\n"
            f"{m['text']}\n</message>"
        )
    return "\n".join(lines)


def state_for_model(state: dict[str, Any]) -> dict[str, Any]:
    memories = []
    for m in state.get("memories", []):
        if m.get("status") in {"active", "hypothesis"}:
            memories.append({
                "id": m.get("id"), "cell": m.get("cell"), "text": m.get("text"),
                "status": m.get("status"), "confidence": m.get("confidence"),
                "source_kind": m.get("source_kind"), "evidence_count": m.get("evidence_count", 0),
                "last_seen": m.get("last_seen"), "expires_at": m.get("expires_at"),
            })
    return {"memories": memories, "tombstones": state.get("tombstones", [])[-30:]}


def proposal_tool_schema() -> list[dict[str, Any]]:
    operation = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "action": {"type": "string", "enum": sorted(ACTIONS)},
            "cell": {"type": "string", "enum": sorted(CELLS)},
            "memory_id": {"type": "string", "maxLength": 80},
            "merge_ids": {"type": "array", "items": {"type": "string", "maxLength": 80}, "maxItems": 8},
            "text": {"type": "string", "maxLength": MAX_ITEM_CHARS},
            "source_kind": {"type": "string", "enum": sorted(SOURCE_KINDS)},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "sensitivity": {"type": "string", "enum": ["normal", "sensitive"]},
            "sensitive_category": {"type": "string", "maxLength": 60},
            "ttl_days": {"type": "integer", "minimum": 1, "maximum": 30},
            "evidence": {
                "type": "array", "maxItems": 12,
                "items": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "session_id": {"type": "string", "maxLength": 160},
                        "message_id": {"type": "integer", "minimum": 1},
                    },
                    "required": ["session_id", "message_id"],
                },
            },
            "reason": {"type": "string", "maxLength": 300},
        },
        "required": ["action", "cell", "source_kind", "confidence", "sensitivity", "evidence", "reason"],
    }
    return [{
        "type": "function",
        "function": {
            "name": "living_memory_submit_changes",
            "description": "Submit evidence-bound proposed changes to Hermes Living Memory. Host validation decides what is actually applied.",
            "parameters": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "summary": {"type": "string", "maxLength": 1000},
                    "operations": {"type": "array", "items": operation, "maxItems": MAX_OPERATIONS},
                },
                "required": ["summary", "operations"],
            },
        },
    }]


def _unsafe_memory_text(text: str) -> bool:
    if not text or len(text) > MAX_ITEM_CHARS:
        return True
    if any(p.search(text) for p in _SECRET_PATTERNS):
        return True
    if _INJECTION_RE.search(text):
        return True
    return False


def _memory_by_id(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(m.get("id")): m for m in state.get("memories", []) if m.get("id")}


def validate_operations(
    proposal: dict[str, Any],
    state: dict[str, Any],
    batch: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    ops = proposal.get("operations") if isinstance(proposal, dict) else None
    if not isinstance(ops, list):
        return [], [{"reason": "operations_missing"}]
    ops = ops[:MAX_OPERATIONS]
    user_evidence = {
        (str(m["session_id"]), int(m["message_id"])): m
        for m in batch if m.get("role") == "user"
    }
    memories = _memory_by_id(state)
    for raw in ops:
        if not isinstance(raw, dict):
            rejected.append({"reason": "operation_not_object"}); continue
        op = dict(raw)
        action = str(op.get("action") or "")
        cell = str(op.get("cell") or "")
        kind = str(op.get("source_kind") or "")
        text = str(op.get("text") or "").strip()
        sensitivity = str(op.get("sensitivity") or "normal")
        sensitive_category = str(op.get("sensitive_category") or "").strip().lower()
        try: confidence = float(op.get("confidence"))
        except Exception: confidence = -1
        if action not in ACTIONS or cell not in CELLS or kind not in SOURCE_KINDS or not 0 <= confidence <= 1:
            rejected.append({"reason": "invalid_enum_or_confidence", "action": action}); continue
        if sensitivity != "normal" or sensitive_category in _SENSITIVE_LABELS or _SENSITIVE_TEXT_RE.search(text):
            rejected.append({"reason": "sensitive_auto_memory_blocked", "action": action}); continue
        evidence_raw = op.get("evidence") or []
        refs: list[dict[str, Any]] = []
        seen: set[tuple[str, int]] = set()
        for ref in evidence_raw if isinstance(evidence_raw, list) else []:
            if not isinstance(ref, dict): continue
            try: key = (str(ref.get("session_id") or ""), int(ref.get("message_id") or 0))
            except Exception: continue
            if key in user_evidence and key not in seen:
                seen.add(key); refs.append({"session_id": key[0], "message_id": key[1]})
        if action == "noop":
            accepted.append({**op, "evidence": refs}); continue
        if not refs:
            rejected.append({"reason": "no_valid_user_evidence", "action": action}); continue
        if action in {"add", "update", "merge"} and _unsafe_memory_text(text):
            rejected.append({"reason": "unsafe_or_empty_text", "action": action}); continue
        if kind == "pattern" and len(refs) < 2:
            rejected.append({"reason": "pattern_needs_two_user_messages", "action": action}); continue
        if action in {"update", "archive", "forget"}:
            mid = str(op.get("memory_id") or "")
            if mid not in memories or memories[mid].get("status") not in {"active", "hypothesis"}:
                rejected.append({"reason": "memory_id_not_active", "action": action}); continue
        if action == "merge":
            mids = [str(x) for x in (op.get("merge_ids") or [])]
            mids = list(dict.fromkeys(x for x in mids if x))
            if len(mids) < 2 or any(x not in memories or memories[x].get("status") not in {"active", "hypothesis"} for x in mids):
                rejected.append({"reason": "merge_ids_invalid", "action": action}); continue
            op["merge_ids"] = mids
        if action == "forget":
            if not any(_FORGET_RE.search(user_evidence[(r["session_id"], r["message_id"])]["text"]) for r in refs):
                rejected.append({"reason": "forget_intent_not_explicit", "action": action}); continue
        if cell == "temporary_context":
            try: ttl = int(op.get("ttl_days") or 7)
            except Exception: ttl = 7
            op["ttl_days"] = max(1, min(30, ttl))
        op["text"] = text
        op["confidence"] = confidence
        op["evidence"] = refs
        op["reason"] = str(op.get("reason") or "")[:300]
        accepted.append(op)
    return accepted, rejected


def _cap_confidence(kind: str, value: float) -> float:
    caps = {"explicit": 0.98, "correction": 0.99, "pattern": 0.90, "hypothesis": 0.60}
    return round(min(max(value, 0.0), caps[kind]), 3)


def _status_for(kind: str, confidence: float, evidence_count: int) -> str:
    if kind in {"explicit", "correction"} and confidence >= 0.70:
        return "active"
    if kind == "pattern" and confidence >= 0.75 and evidence_count >= 2:
        return "active"
    return "hypothesis"


def _new_memory_id() -> str:
    return "lm_" + uuid.uuid4().hex[:12]


def _merge_evidence(*groups: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, int]] = set(); out: list[dict[str, Any]] = []
    for group in groups:
        for ref in group or []:
            key = (str(ref.get("session_id") or ""), int(ref.get("message_id") or 0))
            if key[0] and key[1] and key not in seen:
                seen.add(key); out.append({"session_id": key[0], "message_id": key[1]})
    return out[-20:]


def apply_operations(state: dict[str, Any], operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    applied: list[dict[str, Any]] = []
    memories = _memory_by_id(state)
    now = iso_now()
    for op in operations:
        action = op["action"]
        if action == "noop":
            continue
        kind = op["source_kind"]
        conf = _cap_confidence(kind, float(op["confidence"]))
        refs = op.get("evidence") or []
        if action == "add":
            norm = re.sub(r"\s+", " ", op["text"].strip()).casefold()
            if any(re.sub(r"\s+", " ", str(m.get("text") or "").strip()).casefold() == norm and m.get("status") in {"active","hypothesis"} for m in state["memories"]):
                continue
            item = {
                "id": _new_memory_id(), "cell": op["cell"], "text": op["text"],
                "status": _status_for(kind, conf, len(refs)), "confidence": conf,
                "source_kind": kind, "evidence_count": len(refs), "evidence": refs,
                "first_seen": now, "last_seen": now,
            }
            if op["cell"] == "temporary_context":
                item["expires_at"] = (utc_now() + timedelta(days=int(op.get("ttl_days") or 7))).isoformat()
            state["memories"].append(item); memories[item["id"]] = item
            applied.append({"action": action, "memory_id": item["id"], "cell": item["cell"]})
        elif action == "update":
            item = memories[op["memory_id"]]
            item["text"] = op["text"]; item["cell"] = op["cell"]
            item["source_kind"] = kind; item["confidence"] = conf
            item["evidence"] = _merge_evidence(item.get("evidence", []), refs)
            item["evidence_count"] = len(item["evidence"]); item["last_seen"] = now
            item["status"] = _status_for(kind, conf, item["evidence_count"])
            if op["cell"] == "temporary_context":
                item["expires_at"] = (utc_now() + timedelta(days=int(op.get("ttl_days") or 7))).isoformat()
            else:
                item.pop("expires_at", None)
            applied.append({"action": action, "memory_id": item["id"], "cell": item["cell"]})
        elif action == "merge":
            originals = [memories[mid] for mid in op["merge_ids"]]
            all_refs = _merge_evidence(*(m.get("evidence", []) for m in originals), refs)
            item = {
                "id": _new_memory_id(), "cell": op["cell"], "text": op["text"],
                "status": _status_for(kind, conf, len(all_refs)), "confidence": conf,
                "source_kind": kind, "evidence_count": len(all_refs), "evidence": all_refs,
                "first_seen": min([str(m.get("first_seen") or now) for m in originals] + [now]),
                "last_seen": now,
            }
            if op["cell"] == "temporary_context":
                item["expires_at"] = (utc_now() + timedelta(days=int(op.get("ttl_days") or 7))).isoformat()
            state["memories"].append(item); memories[item["id"]] = item
            for old in originals:
                old["status"] = "archived"; old["archived_at"] = now; old["superseded_by"] = item["id"]
            applied.append({"action": action, "memory_id": item["id"], "merged": op["merge_ids"], "cell": item["cell"]})
        elif action == "archive":
            item = memories[op["memory_id"]]
            item["status"] = "archived"; item["archived_at"] = now; item["last_seen"] = now
            applied.append({"action": action, "memory_id": item["id"], "cell": item["cell"]})
        elif action == "forget":
            item = memories[op["memory_id"]]
            text = str(item.get("text") or "")
            state["tombstones"].append({
                "memory_id": item["id"], "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "forgotten_at": now, "reason": op.get("reason") or "explicit user forget request",
            })
            state["tombstones"] = state["tombstones"][-100:]
            state["memories"] = [m for m in state["memories"] if m is not item]
            memories.pop(item["id"], None)
            applied.append({"action": action, "memory_id": item["id"], "cell": item["cell"], "forgotten_text": text})
    return applied


def expire_temporary(state: dict[str, Any]) -> int:
    now = utc_now(); count = 0
    for item in state.get("memories", []):
        if item.get("status") != "active" or item.get("cell") != "temporary_context" or not item.get("expires_at"):
            continue
        try: expires = datetime.fromisoformat(str(item["expires_at"]))
        except Exception: continue
        if expires.tzinfo is None: expires = expires.replace(tzinfo=timezone.utc)
        if expires <= now:
            item["status"] = "archived"; item["archived_at"] = iso_now(); item["archive_reason"] = "expired"
            count += 1
    return count


def compile_summary(state: dict[str, Any], max_chars: int = MAX_SUMMARY_CHARS) -> str:
    priority = [
        "communication", "boundaries", "corrections", "preferences", "goals", "projects",
        "expertise", "workflows", "decisions", "people", "identity",
        "content_text", "content_images", "content_video", "temporary_context",
    ]
    rank = {cell: i for i, cell in enumerate(priority)}
    active = [m for m in state.get("memories", []) if m.get("status") == "active"]
    active.sort(key=lambda m: (rank.get(str(m.get("cell")), 99), -float(m.get("confidence") or 0), str(m.get("last_seen") or "")), reverse=False)
    lines = [MANAGED_PREFIX, "Use these as user-specific context; newer explicit corrections override older patterns."]
    labels = {
        "communication": "Communication", "boundaries": "Boundaries", "corrections": "Corrections",
        "preferences": "Preferences", "goals": "Goals", "projects": "Projects", "expertise": "Expertise",
        "workflows": "Workflows", "decisions": "Decisions", "people": "People", "identity": "Identity",
        "content_text": "Text", "content_images": "Images", "content_video": "Video", "temporary_context": "Temporary",
    }
    for item in active:
        line = f"- {labels.get(item['cell'], item['cell'])}: {str(item.get('text') or '').strip()}"
        if len("\n".join(lines + [line])) > max_chars:
            break
        lines.append(line)
    if len(lines) == 2:
        return ""
    return "\n".join(lines)


def sync_compiled_user_memory(home: Path, summary: str) -> None:
    mem_dir = home / "memories"; ensure_private_dir(mem_dir)
    path = mem_dir / "USER.md"; lock_path = mem_dir / "USER.md.lock"
    with file_lock(lock_path):
        raw = path.read_text(encoding="utf-8") if path.exists() else ""
        entries = [x.strip() for x in raw.split(ENTRY_DELIMITER) if x.strip()]
        entries = [x for x in entries if not x.startswith(MANAGED_PREFIX)]
        if summary:
            entries.append(summary.strip())
        atomic_text(path, ENTRY_DELIMITER.join(entries) + ("\n" if entries else ""))


def create_snapshot(home: Path, state: dict[str, Any], run_id: str) -> Path:
    root = living_dir(home) / "snapshots"; ensure_private_dir(root)
    user_path = home / "memories" / "USER.md"
    payload = {"created_at": iso_now(), "run_id": run_id, "state": state,
               "user_md": user_path.read_text(encoding="utf-8") if user_path.exists() else ""}
    path = root / f"{run_id}.json"; atomic_json(path, payload); return path


def append_audit(home: Path, record: dict[str, Any]) -> None:
    path = living_dir(home) / "audit.jsonl"; ensure_private_dir(path.parent)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    with file_lock(living_dir(home) / "audit.lock"):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try: os.write(fd, line.encode("utf-8")); os.fsync(fd)
        finally: os.close(fd)


def rollback_snapshot(home: Path, owner: str, snapshot_path: Path) -> None:
    data = json.loads(snapshot_path.read_text(encoding="utf-8"))
    state = data.get("state")
    if not isinstance(state, dict) or state.get("owner") != owner:
        raise RuntimeError("snapshot_owner_mismatch")
    save_state(home, state)
    atomic_text(home / "memories" / "USER.md", str(data.get("user_md") or ""))



def scrub_forgotten_from_snapshots(home: Path, forgotten: list[dict[str, str]]) -> int:
    """Remove explicitly-forgotten content from rollback snapshots too.

    Audit logs never contain memory text. Snapshots do, so a forget operation
    must scrub them or rollback could resurrect information the user asked to
    remove.
    """
    if not forgotten:
        return 0
    ids = {str(x.get("memory_id") or "") for x in forgotten if x.get("memory_id")}
    texts = [str(x.get("forgotten_text") or "") for x in forgotten if x.get("forgotten_text")]
    root = living_dir(home) / "snapshots"
    changed = 0
    if not root.exists():
        return 0
    for path in root.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        state = data.get("state")
        dirty = False
        if isinstance(state, dict) and isinstance(state.get("memories"), list):
            before = len(state["memories"])
            state["memories"] = [m for m in state["memories"] if str(m.get("id") or "") not in ids]
            dirty = dirty or len(state["memories"]) != before
        user_md = str(data.get("user_md") or "")
        for text in texts:
            if text and text in user_md:
                user_md = user_md.replace(text, "[FORGOTTEN]")
                dirty = True
        if dirty:
            data["user_md"] = user_md
            atomic_json(path, data)
            changed += 1
    return changed

def state_metrics(state: dict[str, Any]) -> dict[str, int]:
    out = {"active": 0, "hypothesis": 0, "archived": 0, "total": 0}
    for m in state.get("memories", []):
        out["total"] += 1
        status = str(m.get("status") or "")
        if status in out: out[status] += 1
    return out
