"""SQLite usage tracking and rate limits for keybroker."""

from __future__ import annotations

import asyncio
import calendar
import datetime
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("keybroker.usage")


def safe_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


EXCLUDED_RPM_ERRORS = {
    "lane_limit",
    "bad_path",
    "unknown_route",
    "path_not_allowed",
    "not_implemented",
    "route_unavailable",
}


@dataclass
class RequestRecord:
    ts: float
    client: str
    port: int
    route: str
    kind: str
    method: str
    path: str
    status: int
    req_bytes: int
    resp_bytes: int
    duration_ms: int
    model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    chars: int | None = None
    error: str | None = None

    def __post_init__(self):
        self.path = self.path[:200] if self.path else ""
        self.prompt_tokens = safe_int(self.prompt_tokens)
        self.completion_tokens = safe_int(self.completion_tokens)
        self.chars = safe_int(self.chars)
        self.req_bytes = safe_int(self.req_bytes) or 0
        self.resp_bytes = safe_int(self.resp_bytes) or 0
        self.duration_ms = safe_int(self.duration_ms) or 0


class MonthTotals(dict):
    """Month totals dictionary supporting attribute access and integer coercion."""

    @property
    def chars(self) -> int:
        return int(self.get("chars", 0))

    @property
    def requests(self) -> int:
        return int(self.get("requests", 0))

    @property
    def prompt_tokens(self) -> int:
        return int(self.get("prompt_tokens", 0))

    @property
    def completion_tokens(self) -> int:
        return int(self.get("completion_tokens", 0))

    @property
    def resp_bytes(self) -> int:
        return int(self.get("resp_bytes", 0))

    def __int__(self) -> int:
        return self.chars

    def __ge__(self, other: Any) -> bool:
        other_val = int(other) if not isinstance(other, dict) else other.get("chars", 0)
        return self.chars >= other_val

    def __gt__(self, other: Any) -> bool:
        other_val = int(other) if not isinstance(other, dict) else other.get("chars", 0)
        return self.chars > other_val

    def __le__(self, other: Any) -> bool:
        other_val = int(other) if not isinstance(other, dict) else other.get("chars", 0)
        return self.chars <= other_val

    def __lt__(self, other: Any) -> bool:
        other_val = int(other) if not isinstance(other, dict) else other.get("chars", 0)
        return self.chars < other_val


def _get_utc_month_range(month_str: str) -> tuple[float, float]:
    """Return (start_ts, end_ts) in UTC for 'YYYY-MM'. Raises ValueError on invalid format."""
    parts = month_str.strip().split("-")
    if len(parts) != 2:
        raise ValueError(f"Invalid month format '{month_str}', expected 'YYYY-MM'")
    try:
        year = int(parts[0])
        month = int(parts[1])
    except ValueError:
        raise ValueError(f"Invalid month format '{month_str}', expected 'YYYY-MM'")

    if not (1 <= month <= 12):
        raise ValueError(f"Month must be between 1 and 12, got {month}")

    start_dt = datetime.datetime(year, month, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)
    start_ts = start_dt.timestamp()

    if month == 12:
        end_dt = datetime.datetime(year + 1, 1, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)
    else:
        end_dt = datetime.datetime(year, month + 1, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)
    end_ts = end_dt.timestamp()

    return start_ts, end_ts


class UsageTracker:
    """Manages SQLite usage tracking with async background queue."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._conn = sqlite3.connect(db_path, isolation_level=None)
        self._init_db()
        self._queue: asyncio.Queue[RequestRecord | None] = asyncio.Queue()
        self._writer_task: asyncio.Task[None] | None = None
        self._closed = False
        self.dropped_records: int = 0

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    def _init_db(self) -> None:
        """Create tables and indexes, set WAL mode."""
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL,
                client TEXT,
                port INT,
                route TEXT,
                kind TEXT,
                method TEXT,
                path TEXT,
                status INT,
                req_bytes INT,
                resp_bytes INT,
                duration_ms INT,
                model TEXT,
                prompt_tokens INT,
                completion_tokens INT,
                chars INT,
                error TEXT
            );
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_requests_client_ts ON requests(client, ts);"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_requests_kind_ts ON requests(kind, ts);"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_requests_port_ts ON requests(port, ts);"
        )

    def start(self) -> None:
        """Start the background writer task."""
        if self._writer_task is None or self._writer_task.done():
            self._writer_task = asyncio.create_task(self._writer_loop())

    async def _writer_loop(self) -> None:
        """Background task writing queued records to SQLite."""
        while True:
            item = await self._queue.get()
            if item is None:
                self._queue.task_done()
                break

            batch = [item]
            sentinel_encountered = False
            # Gather up to 100 pending records to batch insert
            while not self._queue.empty() and len(batch) < 100:
                nxt = self._queue.get_nowait()
                if nxt is None:
                    sentinel_encountered = True
                    self._queue.task_done()
                    break
                batch.append(nxt)

            try:
                self._insert_batch(batch)
            except Exception:
                logger.exception("Failed to insert usage records batch into DB, retrying row-by-row")
                for rec in batch:
                    try:
                        self._insert_batch([rec])
                    except Exception:
                        self.dropped_records += 1
            finally:
                for _ in range(len(batch)):
                    self._queue.task_done()

            if sentinel_encountered:
                break

    def _insert_batch(self, batch: list[RequestRecord]) -> None:
        sql = """
            INSERT INTO requests (
                ts, client, port, route, kind, method, path, status,
                req_bytes, resp_bytes, duration_ms, model, prompt_tokens,
                completion_tokens, chars, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        rows = [
            (
                r.ts,
                r.client,
                r.port,
                r.route,
                r.kind,
                r.method,
                r.path,
                r.status,
                r.req_bytes,
                r.resp_bytes,
                r.duration_ms,
                r.model,
                r.prompt_tokens,
                r.completion_tokens,
                r.chars,
                r.error,
            )
            for r in batch
        ]
        self._conn.execute("BEGIN IMMEDIATE;")
        try:
            self._conn.executemany(sql, rows)
            self._conn.execute("COMMIT;")
        except Exception:
            self._conn.execute("ROLLBACK;")
            raise

    def record(self, row: RequestRecord) -> None:
        """Non-blocking record enqueue for proxy."""
        if not self._closed:
            self._queue.put_nowait(row)

    def rpm_count(self, port: int, now: float | None = None) -> int:
        """Return count of successful/upstream requests in the last 60 seconds for given port."""
        if now is None:
            now = time.time()
        window_start = now - 60.0

        placeholders = ",".join("?" for _ in EXCLUDED_RPM_ERRORS)
        sql = f"""
            SELECT COUNT(*) FROM requests
            WHERE port = ? AND ts >= ? AND ts <= ?
              AND (error IS NULL OR error = '' OR error NOT IN ({placeholders}))
        """
        params = (port, window_start, now, *EXCLUDED_RPM_ERRORS)
        cursor = self._conn.execute(sql, params)
        row = cursor.fetchone()
        db_count = row[0] if row else 0

        # Include pending records in queue
        pending_count = sum(
            1
            for item in list(self._queue._queue)
            if item is not None
            and item.port == port
            and window_start <= item.ts <= now
            and (not item.error or item.error not in EXCLUDED_RPM_ERRORS)
        )
        return db_count + pending_count

    def month_totals(self, client: str, kind: str, month: str) -> MonthTotals:
        """Return totals for client and kind in calendar month YYYY-MM (UTC)."""
        start_ts, end_ts = _get_utc_month_range(month)
        cursor = self._conn.execute(
            """
            SELECT
                COUNT(*) as reqs,
                COALESCE(SUM(chars), 0) as chars,
                COALESCE(SUM(prompt_tokens), 0) as p_tokens,
                COALESCE(SUM(completion_tokens), 0) as c_tokens,
                COALESCE(SUM(resp_bytes), 0) as r_bytes
            FROM requests
            WHERE client = ? AND kind = ? AND ts >= ? AND ts < ?
            """,
            (client, kind, start_ts, end_ts),
        )
        row = cursor.fetchone()
        requests = row[0] if row else 0
        chars = row[1] if row else 0
        p_tokens = row[2] if row else 0
        c_tokens = row[3] if row else 0
        r_bytes = row[4] if row else 0

        # Incorporate pending records in queue
        for item in list(self._queue._queue):
            if (
                item is not None
                and item.client == client
                and item.kind == kind
                and start_ts <= item.ts < end_ts
            ):
                requests += 1
                chars += item.chars or 0
                p_tokens += item.prompt_tokens or 0
                c_tokens += item.completion_tokens or 0
                r_bytes += item.resp_bytes or 0

        return MonthTotals(
            requests=requests,
            chars=chars,
            prompt_tokens=p_tokens,
            completion_tokens=c_tokens,
            resp_bytes=r_bytes,
        )

    def aggregate(self, client: str | None, month: str) -> list[dict[str, Any]]:
        """Return aggregated usage by route for month (and optional client)."""
        start_ts, end_ts = _get_utc_month_range(month)

        if client:
            sql = """
                SELECT
                    route,
                    COUNT(*) as requests,
                    COUNT(CASE WHEN status >= 400 THEN 1 END) as errors,
                    COALESCE(SUM(chars), 0) as chars,
                    COALESCE(SUM(prompt_tokens), 0) as prompt_tokens,
                    COALESCE(SUM(completion_tokens), 0) as completion_tokens,
                    COALESCE(SUM(resp_bytes), 0) as resp_bytes
                FROM requests
                WHERE client = ? AND ts >= ? AND ts < ?
                GROUP BY route
                ORDER BY route ASC
            """
            params: tuple[Any, ...] = (client, start_ts, end_ts)
        else:
            sql = """
                SELECT
                    route,
                    COUNT(*) as requests,
                    COUNT(CASE WHEN status >= 400 THEN 1 END) as errors,
                    COALESCE(SUM(chars), 0) as chars,
                    COALESCE(SUM(prompt_tokens), 0) as prompt_tokens,
                    COALESCE(SUM(completion_tokens), 0) as completion_tokens,
                    COALESCE(SUM(resp_bytes), 0) as resp_bytes
                FROM requests
                WHERE ts >= ? AND ts < ?
                GROUP BY route
                ORDER BY route ASC
            """
            params = (start_ts, end_ts)

        cursor = self._conn.execute(sql, params)
        route_map: dict[str, dict[str, Any]] = {}
        for row in cursor.fetchall():
            route_name = row[0]
            route_map[route_name] = {
                "route": route_name,
                "requests": row[1],
                "errors": row[2],
                "chars": row[3],
                "prompt_tokens": row[4],
                "completion_tokens": row[5],
                "resp_bytes": row[6],
            }

        # Merge pending items in queue
        for item in list(self._queue._queue):
            if item is None:
                continue
            if client and item.client != client:
                continue
            if not (start_ts <= item.ts < end_ts):
                continue
            route_name = item.route
            if route_name not in route_map:
                route_map[route_name] = {
                    "route": route_name,
                    "requests": 0,
                    "errors": 0,
                    "chars": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "resp_bytes": 0,
                }
            agg = route_map[route_name]
            agg["requests"] += 1
            if item.status >= 400:
                agg["errors"] += 1
            agg["chars"] += item.chars or 0
            agg["prompt_tokens"] += item.prompt_tokens or 0
            agg["completion_tokens"] += item.completion_tokens or 0
            agg["resp_bytes"] += item.resp_bytes or 0

        return sorted(list(route_map.values()), key=lambda x: str(x["route"]))

    def purge_older_than(self, days: int = 395) -> int:
        """Delete records older than days (default ~13 months)."""
        cutoff_ts = time.time() - (days * 86400.0)
        cursor = self._conn.execute("DELETE FROM requests WHERE ts < ?", (cutoff_ts,))
        return cursor.rowcount

    async def flush(self) -> None:
        """Wait until all pending items in queue are written to SQLite."""
        if self._closed:
            return
        await self._queue.join()

    async def close(self) -> None:
        """Drain queue, stop writer task and close SQLite connection."""
        if self._closed:
            return
        self._closed = True
        if self._writer_task and not self._writer_task.done():
            await self._queue.put(None)
            await self._writer_task
        self._conn.close()
