"""Private SQLite store for one-time Maton onboarding links."""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


TOKEN_BYTES = 32
CSRF_BYTES = 24


@dataclass(frozen=True)
class PortalSession:
    status: str
    chat_id: str
    user_id: str
    expires_at: int
    attempts: int
    csrf_hash: str | None


class SessionError(RuntimeError):
    pass


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    if not existed:
        os.chmod(path, 0o600)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            csrf_hash TEXT,
            chat_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            processing_at INTEGER,
            used_at INTEGER
        )
        """
    )
    return conn


def _cleanup(conn: sqlite3.Connection, now: int) -> None:
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now - 86_400,))
    conn.execute(
        "UPDATE sessions SET status='pending', processing_at=NULL "
        "WHERE status='processing' AND processing_at < ?",
        (now - 120,),
    )


def create_session(
    db_path: str | Path,
    *,
    chat_id: str,
    user_id: str,
    ttl_seconds: int,
) -> str:
    if not chat_id.isdigit() or not user_id.isdigit() or chat_id != user_id:
        raise SessionError("Активация Maton доступна только в личном Telegram-чате")
    now = int(time.time())
    token = secrets.token_urlsafe(TOKEN_BYTES)
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _cleanup(conn, now)
        conn.execute(
            "INSERT INTO sessions(token_hash, chat_id, user_id, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (_digest(token), chat_id, user_id, now, now + ttl_seconds),
        )
        conn.commit()
    return token


def read_session(db_path: str | Path, token: str) -> PortalSession | None:
    if not token or len(token) > 128:
        return None
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT status, chat_id, user_id, expires_at, attempts, csrf_hash "
            "FROM sessions WHERE token_hash=?",
            (_digest(token),),
        ).fetchone()
    if row is None:
        return None
    return PortalSession(**dict(row))


def issue_csrf(db_path: str | Path, token: str) -> str:
    csrf = secrets.token_urlsafe(CSRF_BYTES)
    with connect(db_path) as conn:
        changed = conn.execute(
            "UPDATE sessions SET csrf_hash=? WHERE token_hash=? AND status='pending'",
            (_digest(csrf), _digest(token)),
        ).rowcount
    if changed != 1:
        raise SessionError("Ссылка уже недоступна")
    return csrf


def begin_attempt(
    db_path: str | Path,
    *,
    token: str,
    csrf: str,
    max_attempts: int,
) -> PortalSession:
    now = int(time.time())
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _cleanup(conn, now)
        row = conn.execute(
            "SELECT status, chat_id, user_id, expires_at, attempts, csrf_hash "
            "FROM sessions WHERE token_hash=?",
            (_digest(token),),
        ).fetchone()
        if row is None:
            conn.rollback()
            raise SessionError("Ссылка недействительна")
        session = PortalSession(**dict(row))
        if session.expires_at < now:
            conn.execute(
                "UPDATE sessions SET status='expired' WHERE token_hash=?",
                (_digest(token),),
            )
            conn.commit()
            raise SessionError("Срок действия ссылки истёк")
        if session.status != "pending":
            conn.rollback()
            raise SessionError("Ссылка уже использована или заблокирована")
        if session.attempts >= max_attempts:
            conn.execute(
                "UPDATE sessions SET status='locked' WHERE token_hash=?",
                (_digest(token),),
            )
            conn.commit()
            raise SessionError("Превышено число попыток")
        if not csrf or not session.csrf_hash or not secrets.compare_digest(
            session.csrf_hash, _digest(csrf)
        ):
            conn.rollback()
            raise SessionError("Проверка формы не пройдена. Откройте ссылку заново")
        conn.execute(
            "UPDATE sessions SET status='processing', processing_at=?, "
            "attempts=attempts+1, csrf_hash=NULL WHERE token_hash=?",
            (now, _digest(token)),
        )
        conn.commit()
    return PortalSession(
        status="processing",
        chat_id=session.chat_id,
        user_id=session.user_id,
        expires_at=session.expires_at,
        attempts=session.attempts + 1,
        csrf_hash=None,
    )


def finish_attempt(db_path: str | Path, token: str, *, success: bool, max_attempts: int) -> None:
    now = int(time.time())
    with connect(db_path) as conn:
        if success:
            conn.execute(
                "UPDATE sessions SET status='used', used_at=?, processing_at=NULL "
                "WHERE token_hash=? AND status='processing'",
                (now, _digest(token)),
            )
        else:
            conn.execute(
                "UPDATE sessions SET status=CASE WHEN attempts >= ? THEN 'locked' "
                "ELSE 'pending' END, processing_at=NULL "
                "WHERE token_hash=? AND status='processing'",
                (max_attempts, _digest(token)),
            )
