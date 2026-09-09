"""Hybrid Russian full-history recall for Passive Secretary."""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .archive import ArchiveUnavailable, PostgresArchive
from .retrieval import (
    normalize_source_label,
    parse_source_ref,
    sanitize_attachment_metadata,
    sanitize_media_transcripts,
    sanitize_untrusted_text,
)
from .settings import Settings

QUERY_MAX_CHARS = 300
SENDER_MAX_CHARS = 160


class RecallInputError(ValueError):
    pass

def _query(value: Any) -> str:
    if not isinstance(value, str):
        raise RecallInputError("query must be a string")
    normalized = " ".join(value.split()).strip()
    if not normalized:
        raise RecallInputError("query is required")
    if len(normalized) > QUERY_MAX_CHARS:
        raise RecallInputError(f"query is limited to {QUERY_MAX_CHARS} characters")
    return normalized


def _iso_date(value: Any, label: str) -> date:
    if not isinstance(value, str):
        raise RecallInputError(f"{label} must use YYYY-MM-DD")
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise RecallInputError(f"{label} must use YYYY-MM-DD") from exc


def _bounds(args: dict[str, Any], timezone_name: str) -> tuple[datetime | None, datetime | None]:
    tz = ZoneInfo(timezone_name)
    exact = args.get("date")
    start_raw, end_raw = args.get("start_date"), args.get("end_date")
    if exact and (start_raw or end_raw):
        raise RecallInputError("use either date or start_date/end_date")
    if exact:
        d = _iso_date(exact, "date")
        return datetime.combine(d, time.min, tzinfo=tz).astimezone(timezone.utc), datetime.combine(d + timedelta(days=1), time.min, tzinfo=tz).astimezone(timezone.utc)
    if bool(start_raw) != bool(end_raw):
        raise RecallInputError("start_date and end_date must be provided together")
    if start_raw and end_raw:
        start_d = _iso_date(start_raw, "start_date")
        end_d = _iso_date(end_raw, "end_date")
        if end_d < start_d:
            raise RecallInputError("end_date must not be before start_date")
        if (end_d - start_d).days > 3650:
            raise RecallInputError("date range is limited to 3651 days")
        start = datetime.combine(start_d, time.min, tzinfo=tz).astimezone(timezone.utc)
        end = datetime.combine(end_d + timedelta(days=1), time.min, tzinfo=tz).astimezone(timezone.utc)
        return start, end
    return None, None


def _mode(value: Any) -> str:
    mode = str(value or "hybrid").strip().lower()
    if mode not in {"hybrid", "fts", "fuzzy"}:
        raise RecallInputError("mode must be hybrid, fts, or fuzzy")
    return mode


def _origin(value: Any) -> str:
    origin = str(value or "any").strip().lower()
    if origin not in {"any", "live", "history"}:
        raise RecallInputError("origin must be any, live, or history")
    return origin


def _sender(value: Any) -> str:
    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        raise RecallInputError("sender must be a string")
    return " ".join(value.split())[:SENDER_MAX_CHARS]

class HybridRecall:
    def __init__(self, settings: Settings, archive: PostgresArchive):
        self.settings = settings
        self.archive = archive

    def search(self, args: dict[str, Any], *, owner_id: str) -> str:
        query = _query(args.get("query"))
        mode = _mode(args.get("mode"))
        origin = _origin(args.get("origin"))
        sender = _sender(args.get("sender"))
        source_ref = parse_source_ref(args.get("source_ref"))
        start, end = _bounds(args, self.settings.timezone)
        try:
            limit = max(1, min(int(args.get("limit", 30)), 100))
        except (TypeError, ValueError) as exc:
            raise RecallInputError("limit must be an integer") from exc
        self.archive.ensure_schema()
        conn = self.archive._connect()
        cursor = None
        try:
            cursor = conn.cursor()
            effective_mode = "fts" if mode == "hybrid" else mode
            rows = self._execute(
                cursor,
                query=query,
                mode=effective_mode,
                origin=origin,
                sender=sender,
                source_ref=source_ref,
                start=start,
                end=end,
                owner_id=owner_id,
                limit=limit,
            )
            if mode == "hybrid" and len(rows) < limit:
                fuzzy_rows = self._execute(
                    cursor,
                    query=query,
                    mode="fuzzy",
                    origin=origin,
                    sender=sender,
                    source_ref=source_ref,
                    start=start,
                    end=end,
                    owner_id=owner_id,
                    limit=limit,
                )
                rows = self._merge_hybrid_rows(rows, fuzzy_rows, limit=limit)
            return self._render(rows, query=query, mode=mode, origin=origin, start=start, end=end)
        except RecallInputError:
            raise
        except Exception as exc:
            raise ArchiveUnavailable("postgres_recall_failed") from exc
        finally:
            self.archive._close(conn, cursor)

    @staticmethod
    def _merge_hybrid_rows(
        fts_rows: list[dict[str, Any]],
        fuzzy_rows: list[dict[str, Any]],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for row in [*fts_rows, *fuzzy_rows]:
            key = str(row.get("message_ref") or "")
            if not key:
                continue
            existing = merged.get(key)
            if existing is None:
                merged[key] = dict(row)
                continue
            existing["fts_rank"] = max(float(existing.get("fts_rank") or 0), float(row.get("fts_rank") or 0))
            existing["fuzzy_rank"] = max(float(existing.get("fuzzy_rank") or 0), float(row.get("fuzzy_rank") or 0))
            existing["exact_boost"] = max(float(existing.get("exact_boost") or 0), float(row.get("exact_boost") or 0))
            fts = float(existing["fts_rank"])
            fuzzy = float(existing["fuzzy_rank"])
            exact = float(existing["exact_boost"])
            existing["match_score"] = (
                0.20 * exact
                + 0.65 * (fts / (fts + 0.10) if fts > 0 else 0)
                + 0.25 * fuzzy
            )
        return sorted(
            merged.values(),
            key=lambda row: (float(row.get("match_score") or 0), row.get("sent_at") or datetime.min.replace(tzinfo=timezone.utc)),
            reverse=True,
        )[:limit]

    def _execute(
        self,
        cursor: Any,
        *,
        query: str,
        mode: str,
        origin: str,
        sender: str,
        source_ref: str,
        start: datetime | None,
        end: datetime | None,
        owner_id: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        filters = [
            "message.tenant_id=%s",
            "message.tenant_owner_id=%s",
            "message.source_id=%s",
            "message.test_run_id=%s",
            "message.is_deleted=FALSE",
            "message.sent_at IS NOT NULL",
        ]
        scope_params: list[Any] = [
            self.settings.tenant_id,
            int(owner_id),
            self.settings.source_id,
            self.settings.test_run_id,
        ]
        if source_ref:
            filters.append("message.source_ref=%s")
            scope_params.append(source_ref)
        if start is not None and end is not None:
            filters.append("message.sent_at >= %s AND message.sent_at < %s")
            scope_params.extend((start, end))
        if origin == "history":
            filters.append("message.ingest_origin='history_backfill'")
        elif origin == "live":
            filters.append("message.ingest_origin IN ('group_update','business_update')")
        if sender:
            filters.append("position(lower(%s) in lower(message.sender_label)) > 0")
            scope_params.append(sender)

        where = " AND ".join(filters)
        text_expr = "coalesce(message.body,'') || ' ' || coalesce(message.caption,'')"
        candidate_limit = max(50, min(150, limit * 4))
        branches: list[str] = []
        params: list[Any] = [query, query]

        if mode in {"fts", "hybrid"}:
            branches.append(f"""(
              SELECT message.tenant_id, message.tenant_owner_id, message.source_id,
                     message.test_run_id, message.chat_id, message.message_id,
                     ts_rank_cd(to_tsvector('russian'::regconfig, {text_expr}), q.tsq, 32) AS fts_rank,
                     0::real AS fuzzy_rank
              FROM passive_secretary.messages message CROSS JOIN q
              WHERE {where}
                AND to_tsvector('russian'::regconfig, {text_expr}) @@ q.tsq
              ORDER BY fts_rank DESC, message.sent_at DESC
              LIMIT %s
            )""")
            params.extend([*scope_params, candidate_limit])
            branches.append(f"""(
              SELECT message.tenant_id, message.tenant_owner_id, message.source_id,
                     message.test_run_id, message.chat_id, message.message_id,
                     max(ts_rank_cd(to_tsvector('russian'::regconfig, coalesce(e.transcript,'')), q.tsq, 32)) AS fts_rank,
                     0::real AS fuzzy_rank
              FROM passive_secretary.messages message
              JOIN passive_secretary.media_enrichments e
                ON e.tenant_id=message.tenant_id
               AND e.tenant_owner_id=message.tenant_owner_id
               AND e.source_id=message.source_id
               AND e.test_run_id=message.test_run_id
               AND e.chat_id=message.chat_id AND e.message_id=message.message_id
              CROSS JOIN q
              WHERE {where} AND e.status='transcribed'
                AND to_tsvector('russian'::regconfig, coalesce(e.transcript,'')) @@ q.tsq
              GROUP BY message.tenant_id, message.tenant_owner_id, message.source_id,
                       message.test_run_id, message.chat_id, message.message_id, message.sent_at
              ORDER BY fts_rank DESC, message.sent_at DESC
              LIMIT %s
            )""")
            params.extend([*scope_params, candidate_limit])

        if mode in {"fuzzy", "hybrid"}:
            branches.append(f"""(
              SELECT message.tenant_id, message.tenant_owner_id, message.source_id,
                     message.test_run_id, message.chat_id, message.message_id,
                     0::real AS fts_rank,
                     word_similarity(q.rawq, {text_expr}) AS fuzzy_rank
              FROM passive_secretary.messages message CROSS JOIN q
              WHERE {where} AND q.rawq <%% ({text_expr})
              ORDER BY fuzzy_rank DESC, message.sent_at DESC
              LIMIT %s
            )""")
            params.extend([*scope_params, candidate_limit])
            branches.append(f"""(
              SELECT message.tenant_id, message.tenant_owner_id, message.source_id,
                     message.test_run_id, message.chat_id, message.message_id,
                     0::real AS fts_rank,
                     max(word_similarity(q.rawq, coalesce(e.transcript,''))) AS fuzzy_rank
              FROM passive_secretary.messages message
              JOIN passive_secretary.media_enrichments e
                ON e.tenant_id=message.tenant_id
               AND e.tenant_owner_id=message.tenant_owner_id
               AND e.source_id=message.source_id
               AND e.test_run_id=message.test_run_id
               AND e.chat_id=message.chat_id AND e.message_id=message.message_id
              CROSS JOIN q
              WHERE {where} AND e.status='transcribed'
                AND q.rawq <%% coalesce(e.transcript,'')
              GROUP BY message.tenant_id, message.tenant_owner_id, message.source_id,
                       message.test_run_id, message.chat_id, message.message_id, message.sent_at
              ORDER BY fuzzy_rank DESC, message.sent_at DESC
              LIMIT %s
            )""")
            params.extend([*scope_params, candidate_limit])

        params.append(limit)
        sql = f"""
        WITH q AS (
          SELECT websearch_to_tsquery('russian'::regconfig, %s) AS tsq, %s::text AS rawq
        ), candidates_raw AS (
          {' UNION ALL '.join(branches)}
        ), candidates AS (
          SELECT tenant_id, tenant_owner_id, source_id, test_run_id, chat_id, message_id,
                 max(fts_rank) AS fts_rank, max(fuzzy_rank) AS fuzzy_rank
          FROM candidates_raw
          GROUP BY tenant_id, tenant_owner_id, source_id, test_run_id, chat_id, message_id
        ), scored AS (
          SELECT message.source_ref, message.chat_label, message.message_ref,
                 message.sender_ref, message.sender_label, message.direction,
                 message.body, message.caption, message.content_kind,
                 message.attachment, message.sent_at, message.edited_at,
                 message.ingest_origin,
                 COALESCE((SELECT jsonb_agg(jsonb_build_object(
                     'media_index', e.media_index, 'kind', e.media_kind,
                     'text', e.transcript, 'language', e.language
                   ) ORDER BY e.media_index)
                   FROM passive_secretary.media_enrichments e
                   WHERE e.tenant_id=message.tenant_id
                     AND e.tenant_owner_id=message.tenant_owner_id
                     AND e.source_id=message.source_id
                     AND e.test_run_id=message.test_run_id
                     AND e.chat_id=message.chat_id AND e.message_id=message.message_id
                     AND e.status='transcribed'), '[]'::jsonb) AS media_transcripts,
                 candidates.fts_rank, candidates.fuzzy_rank,
                 CASE WHEN position(lower(q.rawq) in lower({text_expr})) > 0
                      THEN 1.0 ELSE 0.0 END AS exact_boost
          FROM candidates
          JOIN passive_secretary.messages message
            ON message.tenant_id=candidates.tenant_id
           AND message.tenant_owner_id=candidates.tenant_owner_id
           AND message.source_id=candidates.source_id
           AND message.test_run_id=candidates.test_run_id
           AND message.chat_id=candidates.chat_id
           AND message.message_id=candidates.message_id
          CROSS JOIN q
        ), ranked AS (
          SELECT *,
            (0.20 * exact_boost
             + 0.65 * CASE WHEN fts_rank > 0 THEN fts_rank / (fts_rank + 0.10) ELSE 0 END
             + 0.25 * fuzzy_rank) AS match_score
          FROM scored
        )
        SELECT * FROM ranked
        ORDER BY match_score DESC, sent_at DESC, message_ref
        LIMIT %s
        """
        cursor.execute("SET LOCAL pg_trgm.word_similarity_threshold = 0.40")
        cursor.execute(sql, tuple(params))
        columns = [d.name if hasattr(d, "name") else d[0] for d in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def _render(
        self,
        rows: list[dict[str, Any]],
        *,
        query: str,
        mode: str,
        origin: str,
        start: datetime | None,
        end: datetime | None,
    ) -> str:
        tz = ZoneInfo(self.settings.timezone)
        records: list[dict[str, Any]] = []
        for row in rows:
            body = row.get("body") if row.get("body") is not None else row.get("caption")
            ingest = str(row.get("ingest_origin") or "")
            records.append({
                "local_time": row["sent_at"].astimezone(tz).isoformat(timespec="seconds") if row.get("sent_at") else "",
                "source_ref": row.get("source_ref") or "",
                "source_label": normalize_source_label(row.get("chat_label")) or "Telegram chat",
                "message_ref": row.get("message_ref") or "",
                "sender_label": normalize_source_label(row.get("sender_label")) or "Telegram user",
                "body": sanitize_untrusted_text(body, max_chars=3000, preserve_newlines=True),
                "content_kind": sanitize_untrusted_text(row.get("content_kind"), max_chars=32),
                "attachments": sanitize_attachment_metadata(row.get("attachment")),
                "transcripts": sanitize_media_transcripts(row.get("media_transcripts"), max_chars=3000),
                "origin": "IMPORTED_HISTORY" if ingest == "history_backfill" else "LIVE",
                "match_score": round(float(row.get("match_score") or 0), 4),
                "match": {
                    "fts": round(float(row.get("fts_rank") or 0), 4),
                    "fuzzy": round(float(row.get("fuzzy_rank") or 0), 4),
                    "exact": bool(float(row.get("exact_boost") or 0) > 0),
                },
            })
        payload = {
            "ok": True,
            "trust": "UNTRUSTED_DATA",
            "query": query,
            "search_mode": mode,
            "origin_filter": origin,
            "timezone": self.settings.timezone,
            "date_window": {
                "start_utc": start.isoformat() if start else None,
                "end_exclusive_utc": end.isoformat() if end else None,
            },
            "record_count": len(records),
            "records": records,
            "analysis_contract": {
                "source_separation": "Never present IMPORTED_HISTORY as current LIVE status without fresh evidence.",
                "search": "Use lexical evidence first. If results are weak, retry with synonyms or a narrower source/date window.",
                "answer": "Answer naturally, cite source label and date in prose, and distinguish fact from inference.",
            },
        }
        return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


RECALL_TOOL_SCHEMA = {
    "name": "passive_secretary_recall",
    "description": (
        "Recall facts from the owner's full Telegram archive using Russian stemming, "
        "full-text ranking and typo-tolerant fuzzy matching. Use this for historical "
        "questions, topics, people, promises and concepts when the owner does not know "
        "the exact date. Imported history and live messages are explicitly labeled."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": QUERY_MAX_CHARS},
            "mode": {"type": "string", "enum": ["hybrid", "fts", "fuzzy"], "default": "hybrid"},
            "origin": {"type": "string", "enum": ["any", "live", "history"], "default": "any"},
            "date": {"type": "string", "description": "Optional exact Moscow date YYYY-MM-DD."},
            "start_date": {"type": "string", "description": "Optional inclusive Moscow range start YYYY-MM-DD."},
            "end_date": {"type": "string", "description": "Optional inclusive Moscow range end YYYY-MM-DD."},
            "source_ref": {
                "type": "string",
                "description": "Optional opaque source_ref from passive_secretary_sources; omit for all chats.",
            },
            "sender": {
                "type": "string",
                "maxLength": SENDER_MAX_CHARS,
                "description": "Optional sender display-name substring, for example Павел Бебов or Olga Moon.",
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 30},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}
