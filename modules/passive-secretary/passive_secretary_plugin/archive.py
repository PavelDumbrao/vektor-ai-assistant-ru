"""Canonical PostgreSQL archive with idempotent event application."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .settings import Settings
from .retrieval import (
    normalize_query_text,
    sanitize_attachment_metadata,
    sanitize_media_transcripts,
)


ConnectFactory = Callable[..., Any]


class ArchiveUnavailable(RuntimeError):
    """Public-safe storage failure that never contains a DSN or chat body."""


@dataclass(frozen=True)
class ArchivePage:
    rows: list[dict[str, Any]]
    has_more: bool


class PostgresArchive:
    def __init__(self, settings: Settings, *, connect_factory: ConnectFactory | None = None):
        self.settings = settings
        self._connect_factory = connect_factory
        self._schema_sql = (Path(__file__).resolve().parent / "schema.sql").read_text(
            encoding="utf-8"
        )
        self._schema_ready = False
        self._schema_lock = threading.Lock()

    def _dsn(self) -> str:
        dsn = os.environ.get(self.settings.postgres_dsn_env, "").strip()
        if not dsn:
            raise ArchiveUnavailable("postgres_not_configured")
        return dsn

    def _connect(self):
        dsn = self._dsn()
        try:
            if self._connect_factory is not None:
                return self._connect_factory(dsn)
            import psycopg

            return psycopg.connect(
                dsn,
                connect_timeout=self.settings.query_timeout_seconds,
                options=f"-c statement_timeout={self.settings.query_timeout_seconds * 1000}",
                application_name="hermes-passive-secretary",
            )
        except ArchiveUnavailable:
            raise
        except Exception as exc:
            raise ArchiveUnavailable("postgres_connection_failed") from exc

    @staticmethod
    def _close(conn: Any, cursor: Any | None = None) -> None:
        try:
            if cursor is not None:
                cursor.close()
        finally:
            conn.close()

    def ensure_schema(self) -> None:
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            conn = self._connect()
            cursor = None
            try:
                cursor = conn.cursor()
                cursor.execute(self._schema_sql)
                conn.commit()
                self._schema_ready = True
            except Exception as exc:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise ArchiveUnavailable("schema_initialization_failed") from exc
            finally:
                self._close(conn, cursor)

    def apply_event(self, payload: dict[str, Any]) -> bool:
        """Apply one envelope transactionally; return False for a duplicate."""
        self.ensure_schema()
        conn = self._connect()
        cursor = None
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO passive_secretary.archive_events (
                    event_key, tenant_id, tenant_owner_id, source_id, test_run_id,
                    kind, update_id, received_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (event_key) DO NOTHING
                RETURNING event_key
                """,
                (
                    payload["event_key"],
                    payload["tenant_id"],
                    payload["tenant_owner_id"],
                    payload["source_id"],
                    payload.get("test_run_id", ""),
                    payload["kind"],
                    payload["update_id"],
                    payload["received_at"],
                ),
            )
            inserted = cursor.fetchone()
            if inserted is None:
                conn.commit()
                return False
            kind = payload["kind"]
            connection_snapshot = self._connection_snapshot_for_event(payload)
            if connection_snapshot is not None:
                self._apply_connection(cursor, connection_snapshot)
            if kind == "business_connection":
                self._apply_connection(cursor, payload)
            elif kind in {
                "business_message", "edited_business_message",
                "group_message", "edited_group_message",
            }:
                self._apply_message(cursor, payload)
            elif kind == "deleted_business_messages":
                self._apply_deleted(cursor, payload)
            elif kind == "business_media_result":
                self._apply_media_result(cursor, payload)
            else:
                raise ValueError("unsupported_event_kind")
            conn.commit()
            return True
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            if isinstance(exc, (ValueError, KeyError, TypeError)):
                raise ArchiveUnavailable("invalid_event_envelope") from exc
            raise ArchiveUnavailable("postgres_write_failed") from exc
        finally:
            self._close(conn, cursor)

    @staticmethod
    def _scope(payload: dict[str, Any]) -> tuple[Any, ...]:
        return (
            payload["tenant_id"],
            payload["tenant_owner_id"],
            payload["source_id"],
            payload.get("test_run_id", ""),
        )

    @staticmethod
    def _connection_snapshot_for_event(
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Validate an internal snapshot before applying it in the parent event."""
        if "connection_snapshot" not in payload:
            return None
        snapshot = payload.get("connection_snapshot")
        if (
            payload.get("kind")
            not in {
                "business_message",
                "edited_business_message",
                "deleted_business_messages",
            }
            or not isinstance(snapshot, dict)
        ):
            raise ValueError("invalid_connection_snapshot")
        for key in (
            "update_id",
            "tenant_id",
            "tenant_owner_id",
            "source_id",
            "test_run_id",
            "received_at",
            "connection_id",
        ):
            if snapshot.get(key) != payload.get(key):
                raise ValueError("connection_snapshot_scope_mismatch")
        if snapshot.get("enabled") is not True:
            raise ValueError("connection_snapshot_not_enabled")
        for key in (
            "owner_ref",
            "user_chat_id",
            "telegram_can_reply",
            "connection_date",
        ):
            if key not in snapshot:
                raise ValueError("incomplete_connection_snapshot")
        return snapshot

    def _apply_connection(self, cursor: Any, payload: dict[str, Any]) -> None:
        cursor.execute(
            """
            INSERT INTO passive_secretary.business_connections (
                tenant_id, tenant_owner_id, source_id, test_run_id, business_connection_id,
                owner_telegram_user_id, owner_ref, user_chat_id, enabled,
                observed_can_reply, connection_date, last_update_id, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                      %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, tenant_owner_id, source_id, test_run_id, business_connection_id)
            DO UPDATE SET owner_telegram_user_id=excluded.owner_telegram_user_id,
                          owner_ref=excluded.owner_ref,
                          user_chat_id=excluded.user_chat_id,
                          enabled=excluded.enabled,
                          observed_can_reply=excluded.observed_can_reply,
                          connection_date=excluded.connection_date,
                          last_update_id=excluded.last_update_id,
                          updated_at=excluded.updated_at
            WHERE excluded.last_update_id > passive_secretary.business_connections.last_update_id
            """,
            (
                *self._scope(payload),
                payload["connection_id"],
                payload["tenant_owner_id"],
                payload["owner_ref"],
                (
                    int(payload["user_chat_id"])
                    if payload.get("user_chat_id")
                    else None
                ),
                bool(payload["enabled"]),
                bool(payload.get("telegram_can_reply", False)),
                payload["connection_date"],
                payload["update_id"],
                payload["received_at"],
            ),
        )
    def _apply_media_result(self, cursor: Any, payload: dict[str, Any]) -> None:
        """Attach ASR output only to the exact current, non-deleted media."""
        scope = self._scope(payload)
        cursor.execute(
            """
            INSERT INTO passive_secretary.media_enrichments (
                tenant_id, tenant_owner_id, source_id, test_run_id,
                chat_id, message_id, media_index, business_connection_id,
                file_unique_id, job_id, media_kind, status, transcript, language,
                content_sha256, actual_bytes, duration_ms, processor_service,
                processor_version, processor_engine, processor_model,
                processor_quantization, source_update_id, received_at, updated_at
            )
            SELECT message.tenant_id, message.tenant_owner_id, message.source_id,
                   message.test_run_id, message.chat_id, message.message_id,
                   %s, message.business_connection_id, %s, %s, %s, %s, %s, %s,
                   %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            FROM passive_secretary.messages AS message
            WHERE message.tenant_id=%s
              AND message.tenant_owner_id=%s
              AND message.source_id=%s
              AND message.test_run_id=%s
              AND message.chat_id=%s
              AND message.message_id=%s
              AND message.business_connection_id=%s
              AND message.is_deleted=FALSE
              AND jsonb_extract_path_text(
                    message.attachment, 'items', %s, 'file_unique_id'
                  )=%s
            ON CONFLICT (
                tenant_id, tenant_owner_id, source_id, test_run_id,
                chat_id, message_id, media_index, processor_version
            ) DO UPDATE SET business_connection_id=excluded.business_connection_id,
                            file_unique_id=excluded.file_unique_id,
                            job_id=excluded.job_id,
                            media_kind=excluded.media_kind,
                            status=excluded.status,
                            transcript=excluded.transcript,
                            language=excluded.language,
                            content_sha256=excluded.content_sha256,
                            actual_bytes=excluded.actual_bytes,
                            duration_ms=excluded.duration_ms,
                            processor_service=excluded.processor_service,
                            processor_engine=excluded.processor_engine,
                            processor_model=excluded.processor_model,
                            processor_quantization=excluded.processor_quantization,
                            source_update_id=excluded.source_update_id,
                            received_at=excluded.received_at,
                            updated_at=excluded.updated_at
            WHERE excluded.source_update_id >=
                  passive_secretary.media_enrichments.source_update_id
            """,
            (
                payload["media_index"],
                payload["file_unique_id"],
                payload["job_id"],
                payload["media_kind"],
                payload["status"],
                payload.get("transcript"),
                payload.get("language"),
                payload.get("content_sha256"),
                payload.get("actual_bytes"),
                payload.get("duration_ms"),
                payload["processor_service"],
                payload["processor_version"],
                payload["processor_engine"],
                payload["processor_model"],
                payload["processor_quantization"],
                payload["update_id"],
                payload["received_at"],
                payload["received_at"],
                *scope,
                payload["chat_id"],
                payload["message_id"],
                payload["connection_id"],
                str(payload["media_index"]),
                payload["file_unique_id"],
            ),
        )

    def _apply_message(self, cursor: Any, payload: dict[str, Any]) -> None:
        scope = self._scope(payload)
        if payload["kind"] in {"edited_business_message", "edited_group_message"}:
            cursor.execute(
                """
                INSERT INTO passive_secretary.message_versions (
                    tenant_id, tenant_owner_id, source_id, test_run_id, chat_id, message_id,
                    version_observed_at, body, caption, attachment
                )
                SELECT tenant_id, tenant_owner_id, source_id, test_run_id, chat_id, message_id,
                       %s, body, caption, attachment
                FROM passive_secretary.messages
                WHERE tenant_id=%s AND tenant_owner_id=%s AND source_id=%s AND test_run_id=%s
                  AND chat_id=%s AND message_id=%s AND is_deleted=FALSE
                  AND last_update_id < %s
                """,
                (
                    payload["received_at"],
                    *scope,
                    payload["chat_id"],
                    payload["message_id"],
                    payload["update_id"],
                ),
            )
        cursor.execute(
            """
            INSERT INTO passive_secretary.messages (
                tenant_id, tenant_owner_id, source_id, test_run_id, chat_id, message_id,
                business_connection_id, source_ref, chat_label, message_ref,
                reply_to_message_id, reply_to_message_ref, media_group_id, media_group_ref,
                sender_telegram_user_id, sender_ref, sender_label, direction,
                body, caption, content_kind, attachment, sent_at, edited_at,
                is_deleted, ingest_origin, last_update_id, updated_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s,
                %s::jsonb, %s, %s, FALSE, %s, %s, %s
            )
            ON CONFLICT (tenant_id, tenant_owner_id, source_id, test_run_id, chat_id, message_id)
            DO UPDATE SET business_connection_id=excluded.business_connection_id,
                          source_ref=excluded.source_ref,
                          chat_label=excluded.chat_label,
                          message_ref=excluded.message_ref,
                          reply_to_message_id=excluded.reply_to_message_id,
                          reply_to_message_ref=excluded.reply_to_message_ref,
                          media_group_id=excluded.media_group_id,
                          media_group_ref=excluded.media_group_ref,
                          sender_telegram_user_id=excluded.sender_telegram_user_id,
                          sender_ref=excluded.sender_ref,
                          sender_label=excluded.sender_label,
                          direction=excluded.direction,
                          body=excluded.body,
                          caption=excluded.caption,
                          content_kind=excluded.content_kind,
                          attachment=excluded.attachment,
                          sent_at=excluded.sent_at,
                          edited_at=excluded.edited_at,
                          ingest_origin=excluded.ingest_origin,
                          last_update_id=excluded.last_update_id,
                          updated_at=excluded.updated_at
            WHERE passive_secretary.messages.is_deleted=FALSE
              AND excluded.last_update_id > passive_secretary.messages.last_update_id
            """,
            (
                *scope,
                payload["chat_id"],
                payload["message_id"],
                payload["connection_id"],
                payload["source_ref"],
                payload["chat_label"],
                payload["message_ref"],
                payload.get("reply_to_message_id"),
                payload.get("reply_to_message_ref"),
                payload.get("media_group_id"),
                payload.get("media_group_ref"),
                (
                    int(payload["sender_user_id"])
                    if payload.get("sender_user_id")
                    else None
                ),
                payload["sender_ref"],
                payload["sender_label"],
                payload["direction"],
                payload.get("body"),
                payload.get("caption"),
                payload["content_kind"],
                json.dumps(payload.get("attachment") or {}, ensure_ascii=False),
                payload["sent_at"],
                payload.get("edited_at"),
                (
                    "group_update"
                    if payload["kind"] in {"group_message", "edited_group_message"}
                    else "business_update"
                ),
                payload["update_id"],
                payload["received_at"],
            ),
        )
        if int(cursor.rowcount or 0) > 0:
            # An edit can replace/remove media while an older ASR job is in
            # flight.  Keep enrichments only when the current attachment at
            # the exact index still has the same storage-only unique id.
            cursor.execute(
                """
                DELETE FROM passive_secretary.media_enrichments AS enrichment
                USING passive_secretary.messages AS message
                WHERE enrichment.tenant_id=%s
                  AND enrichment.tenant_owner_id=%s
                  AND enrichment.source_id=%s
                  AND enrichment.test_run_id=%s
                  AND enrichment.chat_id=%s
                  AND enrichment.message_id=%s
                  AND message.tenant_id=enrichment.tenant_id
                  AND message.tenant_owner_id=enrichment.tenant_owner_id
                  AND message.source_id=enrichment.source_id
                  AND message.test_run_id=enrichment.test_run_id
                  AND message.chat_id=enrichment.chat_id
                  AND message.message_id=enrichment.message_id
                  AND COALESCE(
                      jsonb_extract_path_text(
                          message.attachment,
                          'items',
                          enrichment.media_index::text,
                          'file_unique_id'
                      ),
                      ''
                  ) <> enrichment.file_unique_id
                """,
                (*scope, payload["chat_id"], payload["message_id"]),
            )

    def _apply_deleted(self, cursor: Any, payload: dict[str, Any]) -> None:
        scope = self._scope(payload)
        refs = payload.get("message_refs") or []
        for index, message_id in enumerate(payload["message_ids"]):
            message_ref = refs[index] if index < len(refs) else "deleted-message"
            cursor.execute(
                """
                INSERT INTO passive_secretary.messages (
                    tenant_id, tenant_owner_id, source_id, test_run_id, chat_id, message_id,
                    business_connection_id, source_ref, chat_label, message_ref,
                    sender_ref, sender_label, direction, body, caption,
                    content_kind, attachment, sent_at, deleted_at, is_deleted,
                    ingest_origin, last_update_id, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    'deleted', 'Deleted sender', 'incoming', NULL, NULL,
                    'deleted', '{}'::jsonb, NULL, %s, TRUE,
                    'business_update', %s, %s
                )
                ON CONFLICT (tenant_id, tenant_owner_id, source_id, test_run_id, chat_id, message_id)
                DO UPDATE SET body=NULL,
                              caption=NULL,
                              attachment='{}'::jsonb,
                              reply_to_message_id=NULL,
                              reply_to_message_ref=NULL,
                              media_group_id=NULL,
                              media_group_ref=NULL,
                              deleted_at=excluded.deleted_at,
                              is_deleted=TRUE,
                              ingest_origin='business_update',
                              last_update_id=excluded.last_update_id,
                              updated_at=excluded.updated_at
                WHERE excluded.last_update_id > passive_secretary.messages.last_update_id
                """,
                (
                    *scope,
                    payload["chat_id"],
                    message_id,
                    payload["connection_id"],
                    payload["source_ref"],
                    payload["chat_label"],
                    message_ref,
                    payload["received_at"],
                    payload["update_id"],
                    payload["received_at"],
                ),
            )
            if int(cursor.rowcount or 0) <= 0:
                continue
            cursor.execute(
                """
                UPDATE passive_secretary.message_versions
                SET body=NULL, caption=NULL, attachment='{}'::jsonb
                WHERE tenant_id=%s AND tenant_owner_id=%s AND source_id=%s AND test_run_id=%s
                  AND chat_id=%s AND message_id=%s
                """,
                (*scope, payload["chat_id"], message_id),
            )
            cursor.execute(
                """
                DELETE FROM passive_secretary.media_enrichments
                WHERE tenant_id=%s AND tenant_owner_id=%s
                  AND source_id=%s AND test_run_id=%s
                  AND chat_id=%s AND message_id=%s
                """,
                (*scope, payload["chat_id"], message_id),
            )

    def enforce_retention(self, *, now: datetime | None = None) -> int:
        self.ensure_schema()
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        cutoff = current.astimezone(timezone.utc) - timedelta(days=self.settings.retention_days)
        conn = self._connect()
        cursor = None
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                DELETE FROM passive_secretary.outbound_intents
                WHERE tenant_id=%s AND tenant_owner_id = ANY(%s) AND source_id=%s AND test_run_id=%s
                  AND updated_at < %s
                """,
                (
                    self.settings.tenant_id,
                    [int(value) for value in self.settings.owner_telegram_user_ids],
                    self.settings.source_id,
                    self.settings.test_run_id,
                    cutoff,
                ),
            )
            cursor.execute(
                """
                DELETE FROM passive_secretary.messages
                WHERE tenant_id=%s AND tenant_owner_id = ANY(%s) AND source_id=%s AND test_run_id=%s
                  AND COALESCE(sent_at, deleted_at, created_at) < %s
                """,
                (
                    self.settings.tenant_id,
                    [int(value) for value in self.settings.owner_telegram_user_ids],
                    self.settings.source_id,
                    self.settings.test_run_id,
                    cutoff,
                ),
            )
            count = max(0, int(cursor.rowcount or 0))
            cursor.execute(
                """
                DELETE FROM passive_secretary.archive_events
                WHERE tenant_id=%s AND tenant_owner_id = ANY(%s) AND source_id=%s AND test_run_id=%s
                  AND received_at < %s
                """,
                (
                    self.settings.tenant_id,
                    [int(value) for value in self.settings.owner_telegram_user_ids],
                    self.settings.source_id,
                    self.settings.test_run_id,
                    cutoff,
                ),
            )
            # An active connection is required to attribute future updates,
            # but a disabled connection is only historical owner metadata.
            # Retain it no longer than message data in the same exact scope.
            cursor.execute(
                """
                DELETE FROM passive_secretary.business_connections AS connection
                WHERE connection.tenant_id=%s
                  AND connection.tenant_owner_id = ANY(%s)
                  AND connection.source_id=%s
                  AND connection.test_run_id=%s
                  AND connection.enabled=FALSE
                  AND connection.updated_at < %s
                  AND NOT EXISTS (
                      SELECT 1 FROM passive_secretary.messages AS message
                      WHERE message.tenant_id=connection.tenant_id
                        AND message.tenant_owner_id=connection.tenant_owner_id
                        AND message.source_id=connection.source_id
                        AND message.test_run_id=connection.test_run_id
                        AND message.business_connection_id=connection.business_connection_id
                  )
                """,
                (
                    self.settings.tenant_id,
                    [int(value) for value in self.settings.owner_telegram_user_ids],
                    self.settings.source_id,
                    self.settings.test_run_id,
                    cutoff,
                ),
            )
            conn.commit()
            return count
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            raise ArchiveUnavailable("retention_enforcement_failed") from exc
        finally:
            self._close(conn, cursor)

    @staticmethod
    def _dict_rows(cursor: Any, rows: Iterable[Any], columns: tuple[str, ...]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for row in rows:
            if isinstance(row, dict):
                result.append(dict(row))
            else:
                result.append(dict(zip(columns, row)))
        return result

    def query_ranges(
        self,
        ranges: list[tuple[datetime, datetime]],
        *,
        tenant_owner_id: str,
        limit: int,
        query_text: str = "",
        source_ref: str = "",
    ) -> list[dict[str, Any]]:
        """Compatibility wrapper for bounded non-paginated callers."""
        return self.query_ranges_page(
            ranges,
            tenant_owner_id=tenant_owner_id,
            limit=limit,
            query_text=query_text,
            source_ref=source_ref,
        ).rows

    def query_ranges_page(
        self,
        ranges: list[tuple[datetime, datetime]],
        *,
        tenant_owner_id: str,
        limit: int,
        query_text: str = "",
        source_ref: str = "",
        after_message_ref: str = "",
    ) -> ArchivePage:
        if not ranges:
            return ArchivePage(rows=[], has_more=False)
        self.ensure_schema()
        bounded_limit = max(1, min(int(limit), 200))
        date_clauses: list[str] = []
        scope = [
            self.settings.tenant_id,
            int(tenant_owner_id),
            self.settings.source_id,
            self.settings.test_run_id,
        ]
        params: list[Any] = [
            *scope,
            self.settings.retention_days,
        ]
        for start, end in ranges:
            if start.tzinfo is None or end.tzinfo is None or start >= end:
                raise ValueError("ranges must be aware, non-empty half-open intervals")
            date_clauses.append("(message.sent_at >= %s AND message.sent_at < %s)")
            params.extend((start.astimezone(timezone.utc), end.astimezone(timezone.utc)))
        text_clause = ""
        normalized_query = normalize_query_text(query_text)
        if normalized_query:
            text_clause = (
                " AND (message.body ILIKE %s ESCAPE '\\' "
                "OR message.caption ILIKE %s ESCAPE '\\' "
                "OR EXISTS ("
                "SELECT 1 FROM passive_secretary.media_enrichments AS searched_media "
                "WHERE searched_media.tenant_id=message.tenant_id "
                "AND searched_media.tenant_owner_id=message.tenant_owner_id "
                "AND searched_media.source_id=message.source_id "
                "AND searched_media.test_run_id=message.test_run_id "
                "AND searched_media.chat_id=message.chat_id "
                "AND searched_media.message_id=message.message_id "
                "AND searched_media.status='transcribed' "
                "AND searched_media.transcript ILIKE %s ESCAPE '\\'))"
            )
            escaped = (
                normalized_query.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            pattern = f"%{escaped}%"
            params.extend((pattern, pattern, pattern))
        source_clause = ""
        if source_ref:
            source_clause = " AND message.source_ref=%s"
            params.append(source_ref)

        cursor_cte = ""
        cursor_join = ""
        cursor_clause = ""
        if after_message_ref:
            cursor_cte = """
                WITH cursor_anchor AS (
                    SELECT sent_at, message_id
                    FROM passive_secretary.messages
                    WHERE tenant_id=%s AND tenant_owner_id=%s
                      AND source_id=%s AND test_run_id=%s
                      AND message_ref=%s
                    LIMIT 1
                )
            """
            cursor_join = " CROSS JOIN cursor_anchor"
            cursor_clause = (
                " AND (message.sent_at, message.message_id) "
                "< (cursor_anchor.sent_at, cursor_anchor.message_id)"
            )
            params = [*scope, after_message_ref, *params]
        params.append(bounded_limit + 1)
        sql = f"""
            {cursor_cte}
            SELECT message.source_ref, message.chat_label, message.message_ref,
                   message.reply_to_message_ref, message.media_group_ref,
                   message.sender_ref, message.sender_label, message.direction,
                   message.body, message.caption, message.content_kind,
                   message.attachment,
                   COALESCE((
                       SELECT jsonb_agg(
                           jsonb_build_object(
                               'media_index', enrichment.media_index,
                               'kind', enrichment.media_kind,
                               'text', enrichment.transcript,
                               'language', enrichment.language
                           ) ORDER BY enrichment.media_index
                       )
                       FROM passive_secretary.media_enrichments AS enrichment
                       WHERE enrichment.tenant_id=message.tenant_id
                         AND enrichment.tenant_owner_id=message.tenant_owner_id
                         AND enrichment.source_id=message.source_id
                         AND enrichment.test_run_id=message.test_run_id
                         AND enrichment.chat_id=message.chat_id
                         AND enrichment.message_id=message.message_id
                         AND enrichment.status='transcribed'
                   ), '[]'::jsonb) AS media_transcripts,
                   message.sent_at, message.edited_at
            FROM passive_secretary.messages AS message{cursor_join}
            WHERE message.tenant_id=%s AND message.tenant_owner_id=%s
              AND message.source_id=%s AND message.test_run_id=%s
              AND message.is_deleted=FALSE
              AND message.sent_at >= CURRENT_TIMESTAMP - make_interval(days => %s)
              AND ({' OR '.join(date_clauses)})
              {text_clause}
              {source_clause}
              {cursor_clause}
            ORDER BY message.sent_at DESC, message.message_id DESC
            LIMIT %s
        """
        conn = self._connect()
        cursor = None
        try:
            cursor = conn.cursor()
            cursor.execute(sql, tuple(params))
            raw_rows = cursor.fetchall()
            columns = (
                "source_ref",
                "chat_label",
                "message_ref",
                "reply_to_message_ref",
                "media_group_ref",
                "sender_ref",
                "sender_label",
                "direction",
                "body",
                "caption",
                "content_kind",
                "attachment",
                "media_transcripts",
                "sent_at",
                "edited_at",
            )
            rows = self._dict_rows(cursor, raw_rows, columns)
            has_more = len(rows) > bounded_limit
            safe_rows: list[dict[str, Any]] = []
            for row in rows[:bounded_limit]:
                safe_row = dict(row)
                safe_row["attachments"] = sanitize_attachment_metadata(
                    safe_row.pop("attachment", {})
                )
                safe_row["media_transcripts"] = sanitize_media_transcripts(
                    safe_row.get("media_transcripts")
                )
                safe_rows.append(safe_row)
            return ArchivePage(rows=safe_rows, has_more=has_more)
        except Exception as exc:
            raise ArchiveUnavailable("postgres_query_failed") from exc
        finally:
            self._close(conn, cursor)

    def query_sources(
        self,
        *,
        tenant_owner_id: str,
        label_query: str = "",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return recent opaque sources in the exact configured archive scope."""
        self.ensure_schema()
        bounded_limit = max(1, min(int(limit), 50))
        normalized_query = " ".join(str(label_query or "").split())[:160].lower()
        params: list[Any] = [
            self.settings.tenant_id,
            int(tenant_owner_id),
            self.settings.source_id,
            self.settings.test_run_id,
            self.settings.retention_days,
        ]
        label_expression = (
            "lower(regexp_replace(btrim(chat_label), '[[:space:]]+', ' ', 'g'))"
        )
        filter_clause = ""
        exact_order = ""
        if normalized_query:
            escaped = (
                normalized_query.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            filter_clause = (
                f"WHERE ({label_expression}=%s OR "
                f"{label_expression} LIKE %s ESCAPE '\\')"
            )
            exact_order = f"CASE WHEN {label_expression}=%s THEN 0 ELSE 1 END,"
            params.extend((normalized_query, f"%{escaped}%", normalized_query))
        params.append(bounded_limit)
        sql = f"""
            WITH distinct_sources AS (
                SELECT source_ref,
                       (ARRAY_AGG(chat_label ORDER BY sent_at DESC, message_id DESC))[1]
                           AS chat_label,
                       MAX(sent_at) AS last_message_at,
                       COUNT(*) AS message_count
                FROM passive_secretary.messages
                WHERE tenant_id=%s AND tenant_owner_id=%s AND source_id=%s AND test_run_id=%s
                  AND is_deleted=FALSE
                  AND sent_at >= CURRENT_TIMESTAMP - make_interval(days => %s)
                GROUP BY source_ref
            )
            SELECT source_ref, chat_label, last_message_at, message_count
            FROM distinct_sources
            {filter_clause}
            ORDER BY {exact_order} last_message_at DESC, source_ref
            LIMIT %s
        """
        conn = self._connect()
        cursor = None
        try:
            cursor = conn.cursor()
            cursor.execute(sql, tuple(params))
            return self._dict_rows(
                cursor,
                cursor.fetchall(),
                ("source_ref", "chat_label", "last_message_at", "message_count"),
            )
        except Exception as exc:
            raise ArchiveUnavailable("postgres_source_query_failed") from exc
        finally:
            self._close(conn, cursor)

    def _outbound_scope(self, tenant_owner_id: str) -> tuple[Any, ...]:
        owner_id = str(tenant_owner_id or "")
        if owner_id not in self.settings.owner_ids:
            raise ArchiveUnavailable("outbound_owner_scope_rejected")
        return (
            self.settings.tenant_id,
            int(owner_id),
            self.settings.source_id,
            self.settings.test_run_id,
        )

    @staticmethod
    def _one_dict(row: Any, columns: tuple[str, ...]) -> dict[str, Any] | None:
        if row is None:
            return None
        if isinstance(row, dict):
            return dict(row)
        return dict(zip(columns, row))

    @staticmethod
    def _latest_incoming_sql() -> str:
        return """
            SELECT message.chat_id,
                   message.message_id AS anchor_message_id,
                   message.business_connection_id,
                   message.chat_label
            FROM passive_secretary.messages AS message
            JOIN passive_secretary.business_connections AS connection
              ON connection.tenant_id=message.tenant_id
             AND connection.tenant_owner_id=message.tenant_owner_id
             AND connection.source_id=message.source_id
             AND connection.test_run_id=message.test_run_id
             AND connection.business_connection_id=message.business_connection_id
            WHERE message.tenant_id=%s
              AND message.tenant_owner_id=%s
              AND message.source_id=%s
              AND message.test_run_id=%s
              AND message.source_ref=%s
              AND message.direction='incoming'
              AND message.ingest_origin='business_update'
              AND message.is_deleted=FALSE
              AND message.sent_at IS NOT NULL
              AND message.sent_at <= CURRENT_TIMESTAMP
              AND message.sent_at > CURRENT_TIMESTAMP - INTERVAL '24 hours'
              AND connection.owner_telegram_user_id=message.tenant_owner_id
              AND connection.enabled=TRUE
            ORDER BY message.sent_at DESC, message.message_id DESC
            LIMIT 1
        """

    def _latest_incoming(
        self,
        cursor: Any,
        *,
        scope: tuple[Any, ...],
        source_ref: str,
    ) -> dict[str, Any] | None:
        cursor.execute(self._latest_incoming_sql(), (*scope, source_ref))
        return self._one_dict(
            cursor.fetchone(),
            (
                "chat_id",
                "anchor_message_id",
                "business_connection_id",
                "chat_label",
            ),
        )

    def prepare_reply_intent(
        self,
        *,
        tenant_owner_id: str,
        intent_id: str,
        session_ref: str,
        source_ref: str,
        message_hmac: str,
        reply_to_latest: bool,
        ttl_seconds: int,
    ) -> dict[str, Any]:
        """Create or recover one no-plaintext outbound intent.

        The database clock owns both the 24-hour Telegram eligibility window
        and the approval TTL.  Idempotency is the private-keyed payload HMAC
        plus the exact latest incoming message anchor, never a model-provided
        request identifier.
        """
        self.ensure_schema()
        scope = self._outbound_scope(tenant_owner_id)
        conn = self._connect()
        cursor = None
        try:
            cursor = conn.cursor()
            lock_material = "\0".join(
                (str(scope[0]), str(scope[1]), str(scope[2]), str(scope[3]), source_ref, message_hmac)
            )
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (lock_material,),
            )
            target = self._latest_incoming(
                cursor,
                scope=scope,
                source_ref=source_ref,
            )
            if target is None:
                conn.commit()
                return {"state": "no_recent_incoming"}

            cursor.execute(
                """
                SELECT intent_id, session_ref, status,
                       expires_at > CURRENT_TIMESTAMP AS approval_live,
                       telegram_message_id
                FROM passive_secretary.outbound_intents
                WHERE tenant_id=%s AND tenant_owner_id=%s
                  AND source_id=%s AND test_run_id=%s
                  AND source_ref=%s AND message_hmac=%s
                  AND anchor_message_id=%s
                ORDER BY CASE status
                           WHEN 'ambiguous' THEN 0
                           WHEN 'sending' THEN 1
                           WHEN 'sent' THEN 2
                           WHEN 'prepared' THEN 3
                           ELSE 4
                         END,
                         created_at DESC
                LIMIT 1
                FOR UPDATE
                """,
                (
                    *scope,
                    source_ref,
                    message_hmac,
                    int(target["anchor_message_id"]),
                ),
            )
            existing = self._one_dict(
                cursor.fetchone(),
                (
                    "intent_id",
                    "session_ref",
                    "status",
                    "approval_live",
                    "telegram_message_id",
                ),
            )
            if existing is not None:
                status = str(existing.get("status") or "")
                if status == "sending":
                    cursor.execute(
                        """
                        UPDATE passive_secretary.outbound_intents
                        SET status='ambiguous', failure_code='interrupted_send',
                            updated_at=CURRENT_TIMESTAMP
                        WHERE tenant_id=%s AND tenant_owner_id=%s
                          AND source_id=%s AND test_run_id=%s AND intent_id=%s
                          AND status='sending'
                        """,
                        (*scope, existing["intent_id"]),
                    )
                    conn.commit()
                    return {"state": "ambiguous"}
                if status in {"ambiguous", "sent"}:
                    conn.commit()
                    result = {"state": status}
                    if status == "sent":
                        result["telegram_message_id"] = existing.get(
                            "telegram_message_id"
                        )
                    return result
                if status == "prepared" and bool(existing.get("approval_live")):
                    if existing.get("session_ref") != session_ref:
                        conn.commit()
                        return {"state": "pending_other_session"}
                    conn.commit()
                    return {
                        "state": "prepared",
                        "intent_id": existing["intent_id"],
                        "chat_label": target.get("chat_label") or "Unknown chat",
                    }
                if status == "prepared":
                    cursor.execute(
                        """
                        UPDATE passive_secretary.outbound_intents
                        SET status='failed_known', failure_code='approval_expired',
                            updated_at=CURRENT_TIMESTAMP
                        WHERE tenant_id=%s AND tenant_owner_id=%s
                          AND source_id=%s AND test_run_id=%s AND intent_id=%s
                          AND status='prepared'
                        """,
                        (*scope, existing["intent_id"]),
                    )

            cursor.execute(
                """
                INSERT INTO passive_secretary.outbound_intents (
                    tenant_id, tenant_owner_id, source_id, test_run_id,
                    intent_id, session_ref, source_ref, message_hmac,
                    chat_id, business_connection_id, anchor_message_id,
                    reply_message_id, status, expires_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, 'prepared',
                    CURRENT_TIMESTAMP + make_interval(secs => %s)
                )
                """,
                (
                    *scope,
                    intent_id,
                    session_ref,
                    source_ref,
                    message_hmac,
                    int(target["chat_id"]),
                    str(target["business_connection_id"]),
                    int(target["anchor_message_id"]),
                    int(target["anchor_message_id"]) if reply_to_latest else None,
                    int(ttl_seconds),
                ),
            )
            conn.commit()
            return {
                "state": "prepared",
                "intent_id": intent_id,
                "chat_label": target.get("chat_label") or "Unknown chat",
            }
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            if isinstance(exc, ArchiveUnavailable):
                raise
            raise ArchiveUnavailable("outbound_intent_prepare_failed") from exc
        finally:
            self._close(conn, cursor)

    def claim_reply_intent(
        self,
        *,
        tenant_owner_id: str,
        intent_id: str,
        session_ref: str,
        source_ref: str,
        message_hmac: str,
    ) -> dict[str, Any]:
        """Atomically move one exact, still-eligible intent to ``sending``."""
        self.ensure_schema()
        scope = self._outbound_scope(tenant_owner_id)
        conn = self._connect()
        cursor = None
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (intent_id,),
            )
            cursor.execute(
                """
                SELECT session_ref, source_ref, message_hmac, status,
                       expires_at > CURRENT_TIMESTAMP AS approval_live,
                       chat_id, business_connection_id, anchor_message_id,
                       reply_message_id, telegram_message_id
                FROM passive_secretary.outbound_intents
                WHERE tenant_id=%s AND tenant_owner_id=%s
                  AND source_id=%s AND test_run_id=%s AND intent_id=%s
                FOR UPDATE
                """,
                (*scope, intent_id),
            )
            intent = self._one_dict(
                cursor.fetchone(),
                (
                    "session_ref",
                    "source_ref",
                    "message_hmac",
                    "status",
                    "approval_live",
                    "chat_id",
                    "business_connection_id",
                    "anchor_message_id",
                    "reply_message_id",
                    "telegram_message_id",
                ),
            )
            if intent is None:
                conn.commit()
                return {"state": "intent_not_found"}
            if not (
                hmac_compare(intent.get("session_ref"), session_ref)
                and hmac_compare(intent.get("source_ref"), source_ref)
                and hmac_compare(intent.get("message_hmac"), message_hmac)
            ):
                conn.commit()
                return {"state": "intent_contract_mismatch"}

            status = str(intent.get("status") or "")
            if status == "sent":
                conn.commit()
                return {
                    "state": "sent",
                    "telegram_message_id": intent.get("telegram_message_id"),
                }
            if status == "sending":
                cursor.execute(
                    """
                    UPDATE passive_secretary.outbound_intents
                    SET status='ambiguous', failure_code='interrupted_send',
                        updated_at=CURRENT_TIMESTAMP
                    WHERE tenant_id=%s AND tenant_owner_id=%s
                      AND source_id=%s AND test_run_id=%s AND intent_id=%s
                      AND status='sending'
                    """,
                    (*scope, intent_id),
                )
                conn.commit()
                return {"state": "ambiguous"}
            if status != "prepared":
                conn.commit()
                return {"state": status or "intent_not_sendable"}
            if not bool(intent.get("approval_live")):
                cursor.execute(
                    """
                    UPDATE passive_secretary.outbound_intents
                    SET status='failed_known', failure_code='approval_expired',
                        updated_at=CURRENT_TIMESTAMP
                    WHERE tenant_id=%s AND tenant_owner_id=%s
                      AND source_id=%s AND test_run_id=%s AND intent_id=%s
                      AND status='prepared'
                    """,
                    (*scope, intent_id),
                )
                conn.commit()
                return {"state": "approval_expired"}

            target = self._latest_incoming(
                cursor,
                scope=scope,
                source_ref=source_ref,
            )
            matches_target = target is not None and (
                int(target["chat_id"]) == int(intent["chat_id"])
                and int(target["anchor_message_id"])
                == int(intent["anchor_message_id"])
                and str(target["business_connection_id"])
                == str(intent["business_connection_id"])
            )
            if not matches_target:
                cursor.execute(
                    """
                    UPDATE passive_secretary.outbound_intents
                    SET status='failed_known', failure_code='target_changed',
                        updated_at=CURRENT_TIMESTAMP
                    WHERE tenant_id=%s AND tenant_owner_id=%s
                      AND source_id=%s AND test_run_id=%s AND intent_id=%s
                      AND status='prepared'
                    """,
                    (*scope, intent_id),
                )
                conn.commit()
                return {"state": "target_changed"}

            cursor.execute(
                """
                UPDATE passive_secretary.outbound_intents
                SET status='sending', failure_code=NULL,
                    updated_at=CURRENT_TIMESTAMP
                WHERE tenant_id=%s AND tenant_owner_id=%s
                  AND source_id=%s AND test_run_id=%s AND intent_id=%s
                  AND status='prepared' AND expires_at > CURRENT_TIMESTAMP
                """,
                (*scope, intent_id),
            )
            if int(cursor.rowcount or 0) != 1:
                conn.rollback()
                return {"state": "intent_claim_failed"}
            conn.commit()
            return {
                "state": "claimed",
                "chat_id": int(intent["chat_id"]),
                "business_connection_id": str(intent["business_connection_id"]),
                "reply_message_id": (
                    int(intent["reply_message_id"])
                    if intent.get("reply_message_id") is not None
                    else None
                ),
            }
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            if isinstance(exc, ArchiveUnavailable):
                raise
            raise ArchiveUnavailable("outbound_intent_claim_failed") from exc
        finally:
            self._close(conn, cursor)

    def finish_reply_intent(
        self,
        *,
        tenant_owner_id: str,
        intent_id: str,
        status: str,
        telegram_message_id: int | None = None,
        failure_code: str | None = None,
    ) -> bool:
        """Persist a terminal outcome using a strict state transition."""
        transitions = {
            "denied": "prepared",
            "failed_known": "sending",
            "ambiguous": "sending",
            "sent": "sending",
        }
        if status not in transitions:
            raise ValueError("unsupported_outbound_terminal_status")
        if status == "sent":
            if not isinstance(telegram_message_id, int) or telegram_message_id <= 0:
                raise ValueError("sent_status_requires_message_id")
        elif telegram_message_id is not None:
            raise ValueError("non_sent_status_forbids_message_id")
        normalized_failure = str(failure_code or "")[:80] or None
        self.ensure_schema()
        scope = self._outbound_scope(tenant_owner_id)
        conn = self._connect()
        cursor = None
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE passive_secretary.outbound_intents
                SET status=%s, telegram_message_id=%s, failure_code=%s,
                    updated_at=CURRENT_TIMESTAMP
                WHERE tenant_id=%s AND tenant_owner_id=%s
                  AND source_id=%s AND test_run_id=%s AND intent_id=%s
                  AND status=%s
                """,
                (
                    status,
                    telegram_message_id,
                    normalized_failure,
                    *scope,
                    intent_id,
                    transitions[status],
                ),
            )
            changed = int(cursor.rowcount or 0) == 1
            if changed:
                conn.commit()
            else:
                conn.rollback()
            return changed
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            raise ArchiveUnavailable("outbound_intent_finish_failed") from exc
        finally:
            self._close(conn, cursor)


def hmac_compare(left: Any, right: Any) -> bool:
    """Constant-time compare for opaque outbound contract fields."""
    import hmac

    if not isinstance(left, str) or not isinstance(right, str):
        return False
    return hmac.compare_digest(left, right)
