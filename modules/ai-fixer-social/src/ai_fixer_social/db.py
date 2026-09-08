from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .models import ReplyDecision


def utc_now() -> datetime:
    return datetime.now(UTC)


def poll_aggregate(poll: dict) -> dict:
    keys = {"id", "question", "type", "is_anonymous", "is_closed", "allows_multiple_answers",
            "total_voter_count", "correct_option_ids", "explanation", "open_period", "close_date"}
    result = {key: value for key, value in poll.items() if key in keys}
    if "options" in poll:
        result["options"] = [{key: option[key] for key in ("text", "voter_count") if key in option}
                             for option in poll["options"] if isinstance(option, dict)]
    return result


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def close(self) -> None:
        self.conn.close()

    def _migrate(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS thread_roots (
                chat_id INTEGER NOT NULL,
                root_message_id INTEGER NOT NULL,
                channel_post_id INTEGER,
                root_text TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                PRIMARY KEY (chat_id, root_message_id)
            );

            CREATE TABLE IF NOT EXISTS messages (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                thread_id INTEGER,
                user_id INTEGER,
                is_bot INTEGER NOT NULL DEFAULT 0,
                text TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                PRIMARY KEY (chat_id, message_id)
            );

            CREATE TABLE IF NOT EXISTS decisions (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                risk TEXT NOT NULL,
                confidence REAL NOT NULL,
                intent TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                reply_text TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                PRIMARY KEY (chat_id, message_id)
            );

            CREATE TABLE IF NOT EXISTS replies (
                chat_id INTEGER NOT NULL,
                source_message_id INTEGER NOT NULL,
                reply_message_id INTEGER NOT NULL,
                thread_id INTEGER,
                user_id INTEGER,
                created_at TEXT NOT NULL,
                PRIMARY KEY (chat_id, source_message_id)
            );

            CREATE TABLE IF NOT EXISTS publications (
                request_id TEXT PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                public_url TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS outbound_attempts (
                action_id TEXT PRIMARY KEY,
                payload_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                result_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS telegram_objects (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                message_json TEXT NOT NULL,
                poll_id TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(chat_id,message_id)
            );
            CREATE INDEX IF NOT EXISTS idx_telegram_objects_poll ON telegram_objects(poll_id);
            CREATE TABLE IF NOT EXISTS telegram_revisions (
                action_id TEXT PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                before_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_messages_thread
                ON messages(chat_id, thread_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_replies_created
                ON replies(created_at);
            CREATE INDEX IF NOT EXISTS idx_replies_thread
                ON replies(thread_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_replies_user
                ON replies(user_id, created_at);
            """
        )
        self.conn.commit()

        from .editor import migrate
        migrate(self)

    def get_int(self, key: str) -> int | None:
        row = self.conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        try:
            return int(row["value"])
        except ValueError:
            return None

    def set_int(self, key: str, value: int) -> None:
        self.conn.execute(
            """
            INSERT INTO state(key,value,updated_at) VALUES(?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, str(value), utc_now().isoformat()),
        )
        self.conn.commit()

    def remember_root(
        self,
        chat_id: int,
        root_message_id: int,
        *,
        channel_post_id: int | None = None,
        root_text: str = "",
        created_at: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO thread_roots(chat_id,root_message_id,channel_post_id,root_text,created_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(chat_id,root_message_id) DO UPDATE SET
              channel_post_id=COALESCE(excluded.channel_post_id,thread_roots.channel_post_id),
              root_text=CASE WHEN excluded.root_text<>'' THEN excluded.root_text ELSE thread_roots.root_text END
            """,
            (
                chat_id,
                root_message_id,
                channel_post_id,
                root_text[:8000],
                created_at or utc_now().isoformat(),
            ),
        )
        self.conn.commit()

    def root_exists(self, chat_id: int, root_message_id: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM thread_roots WHERE chat_id=? AND root_message_id=?",
            (chat_id, root_message_id),
        ).fetchone()
        return row is not None

    def root_text(self, chat_id: int, root_message_id: int | None) -> str:
        if root_message_id is None:
            return ""
        row = self.conn.execute(
            "SELECT root_text FROM thread_roots WHERE chat_id=? AND root_message_id=?",
            (chat_id, root_message_id),
        ).fetchone()
        return str(row["root_text"]) if row else ""

    def record_message(
        self,
        *,
        chat_id: int,
        message_id: int,
        thread_id: int | None,
        user_id: int | None,
        is_bot: bool,
        text: str,
        created_at: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO messages(chat_id,message_id,thread_id,user_id,is_bot,text,created_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            (chat_id, message_id, thread_id, user_id, int(is_bot), text[:8000], created_at),
        )
        self.conn.commit()

    def is_processed(self, chat_id: int, message_id: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM decisions WHERE chat_id=? AND message_id=?",
            (chat_id, message_id),
        ).fetchone()
        return row is not None

    def recent_thread_context(self, chat_id: int, thread_id: int | None, limit: int = 8) -> list[str]:
        if thread_id is None:
            return []
        rows = self.conn.execute(
            """
            SELECT text FROM messages
            WHERE chat_id=? AND thread_id=? AND text<>''
            ORDER BY created_at DESC LIMIT ?
            """,
            (chat_id, thread_id, limit),
        ).fetchall()
        return [str(row["text"])[:1200] for row in reversed(rows)]

    def record_decision(self, chat_id: int, message_id: int, decision: ReplyDecision) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO decisions(
              chat_id,message_id,action,risk,confidence,intent,reason,reply_text,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                chat_id,
                message_id,
                decision.action,
                decision.risk,
                decision.confidence,
                decision.intent,
                decision.reason,
                decision.reply_text,
                utc_now().isoformat(),
            ),
        )
        self.conn.commit()

    def record_simple_decision(
        self,
        chat_id: int,
        message_id: int,
        *,
        action: str,
        risk: str,
        reason: str,
    ) -> None:
        self.record_decision(
            chat_id,
            message_id,
            ReplyDecision(action, risk, 1.0, "", reason, ""),
        )

    def record_reply(
        self,
        *,
        chat_id: int,
        source_message_id: int,
        reply_message_id: int,
        thread_id: int | None,
        user_id: int | None,
    ) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO replies(
              chat_id,source_message_id,reply_message_id,thread_id,user_id,created_at
            ) VALUES(?,?,?,?,?,?)
            """,
            (chat_id, source_message_id, reply_message_id, thread_id, user_id, utc_now().isoformat()),
        )
        self.conn.commit()

    def reply_limits_ok(
        self,
        *,
        thread_id: int | None,
        user_id: int | None,
        per_hour: int,
        per_thread_hour: int,
        per_day: int,
        user_cooldown_minutes: int,
    ) -> tuple[bool, str]:
        now = utc_now()
        hour = (now - timedelta(hours=1)).isoformat()
        day = (now - timedelta(days=1)).isoformat()
        cooldown = (now - timedelta(minutes=user_cooldown_minutes)).isoformat()

        total_hour = self.conn.execute(
            "SELECT COUNT(*) AS c FROM replies WHERE created_at>=?", (hour,)
        ).fetchone()["c"]
        if total_hour >= per_hour:
            return False, "global_hourly_limit"

        total_day = self.conn.execute(
            "SELECT COUNT(*) AS c FROM replies WHERE created_at>=?", (day,)
        ).fetchone()["c"]
        if total_day >= per_day:
            return False, "global_daily_limit"

        if thread_id is not None:
            thread_hour = self.conn.execute(
                "SELECT COUNT(*) AS c FROM replies WHERE thread_id=? AND created_at>=?",
                (thread_id, hour),
            ).fetchone()["c"]
            if thread_hour >= per_thread_hour:
                return False, "thread_hourly_limit"

        if user_id is not None:
            user_recent = self.conn.execute(
                "SELECT COUNT(*) AS c FROM replies WHERE user_id=? AND created_at>=?",
                (user_id, cooldown),
            ).fetchone()["c"]
            if user_recent:
                return False, "user_cooldown"
        return True, "ok"

    @staticmethod
    def payload_hash(text: str, photo_digest: str = "") -> str:
        return hashlib.sha256((text + "\n" + photo_digest).encode()).hexdigest()

    def get_publication(self, request_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM publications WHERE request_id=?", (request_id,)
        ).fetchone()

    def record_publication(
        self,
        *,
        request_id: str,
        channel_id: int,
        message_id: int,
        public_url: str,
        payload_hash: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO publications(request_id,channel_id,message_id,public_url,payload_hash,created_at)
            VALUES(?,?,?,?,?,?)
            """,
            (request_id, channel_id, message_id, public_url, payload_hash, utc_now().isoformat()),
        )
        self.conn.commit()

    def stats(self) -> dict[str, int]:
        tables = ["thread_roots", "messages", "decisions", "replies", "publications"]
        return {
            table: int(self.conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"])
            for table in tables
        }

    def record_telegram_object(self, chat_id: int, message: dict, kind: str) -> None:
        message = dict(message)
        if isinstance(message.get("poll"), dict):
            message["poll"] = poll_aggregate(message["poll"])
        poll = message.get("poll") or {}
        self.conn.execute(
            "INSERT INTO telegram_objects VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(chat_id,message_id) DO UPDATE SET kind=excluded.kind, "
            "message_json=excluded.message_json,poll_id=excluded.poll_id,updated_at=excluded.updated_at",
            (chat_id, int(message["message_id"]), kind, json.dumps(message, ensure_ascii=False),
             str(poll["id"]) if poll.get("id") is not None else None, utc_now().isoformat()),
        )
        self.conn.commit()

    def telegram_object(self, chat_id: int, message_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM telegram_objects WHERE chat_id=? AND message_id=?",
                                (chat_id, message_id)).fetchone()
        return {**dict(row), "message": json.loads(row["message_json"])} if row else None

    def update_poll(self, poll: dict) -> bool:
        if not isinstance(poll, dict) or not isinstance(poll.get("id"), str):
            return False
        rows = self.conn.execute("SELECT * FROM telegram_objects WHERE poll_id=?", (poll["id"],)).fetchall()
        for row in rows:
            message = json.loads(row["message_json"])
            message["poll"] = {**message.get("poll", {}), **poll_aggregate(poll)}
            self.record_telegram_object(row["chat_id"], message, row["kind"])
        return bool(rows)

    def remember_revision(self, action_id: str, obj: dict) -> None:
        self.conn.execute("INSERT INTO telegram_revisions VALUES(?,?,?,?,?)",
                          (action_id, obj["chat_id"], obj["message_id"], obj["message_json"], utc_now().isoformat()))
        self.conn.commit()
