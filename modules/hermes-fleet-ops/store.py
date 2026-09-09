#!/usr/bin/env python3
"""Root-only central operational/product analytics store for Hermes Forge."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

DEFAULT_ROOT = Path("/var/lib/proai-hermes-analytics")
DEFAULT_DB = DEFAULT_ROOT / "forge_analytics.sqlite3"


class StoreError(RuntimeError):
    pass


def _secure_root(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise StoreError("analytics_root_symlink")
    os.chown(path, 0, 0)
    os.chmod(path, 0o700)


class AnalyticsStore:
    def __init__(self, path: Path = DEFAULT_DB) -> None:
        self.path = path
        _secure_root(path.parent)
        if not path.exists():
            path.touch(mode=0o600)
        if path.is_symlink():
            raise StoreError("analytics_db_symlink")
        os.chown(path, 0, 0)
        os.chmod(path, 0o600)
        self._ensure_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _ensure_schema(self) -> None:
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS metric_packages (
                profile TEXT NOT NULL,
                package_id TEXT NOT NULL,
                period_start TEXT NOT NULL,
                period_end TEXT NOT NULL,
                generated_at TEXT NOT NULL,
                hermes_version TEXT NOT NULL,
                ingested_at TEXT NOT NULL,
                PRIMARY KEY(profile, package_id)
            );
            CREATE TABLE IF NOT EXISTS metric_counters (
                profile TEXT NOT NULL,
                package_id TEXT NOT NULL,
                metric_name TEXT NOT NULL,
                dimensions_json TEXT NOT NULL,
                value INTEGER NOT NULL CHECK(value > 0),
                PRIMARY KEY(profile, package_id, metric_name, dimensions_json),
                FOREIGN KEY(profile, package_id)
                    REFERENCES metric_packages(profile, package_id)
                    ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS runtime_health (
                profile TEXT PRIMARY KEY,
                release_id TEXT NOT NULL,
                service_state TEXT NOT NULL,
                telegram_state TEXT NOT NULL,
                error_code TEXT NOT NULL,
                needs_attention INTEGER NOT NULL,
                code_version TEXT NOT NULL,
                active_agents INTEGER NOT NULL,
                observed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS health_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile TEXT NOT NULL,
                release_id TEXT NOT NULL,
                service_state TEXT NOT NULL,
                telegram_state TEXT NOT NULL,
                error_code TEXT NOT NULL,
                needs_attention INTEGER NOT NULL,
                code_version TEXT NOT NULL,
                observed_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS health_events_profile_time
                ON health_events(profile, observed_at);
            CREATE TABLE IF NOT EXISTS update_attempts (
                attempt_id TEXT PRIMARY KEY,
                profile TEXT NOT NULL,
                track TEXT NOT NULL,
                channel TEXT NOT NULL,
                from_release TEXT NOT NULL,
                to_release TEXT NOT NULL,
                outcome TEXT NOT NULL,
                error_code TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rollout_state (
                track TEXT NOT NULL,
                channel TEXT NOT NULL,
                release_id TEXT NOT NULL,
                rollout_percent INTEGER NOT NULL,
                healthy_cycles INTEGER NOT NULL,
                paused INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(track, channel)
            );
            """)

    def package_seen(self, profile: str, package_id: str) -> bool:
        with self.connect() as db:
            return db.execute(
                "SELECT 1 FROM metric_packages WHERE profile=? AND package_id=?",
                (profile, package_id),
            ).fetchone() is not None

    def ingest_package(self, profile: str, package: dict[str, Any], ingested_at: str) -> bool:
        package_id = str(package["package_id"])
        with self.connect() as db:
            if db.execute(
                "SELECT 1 FROM metric_packages WHERE profile=? AND package_id=?",
                (profile, package_id),
            ).fetchone() is not None:
                return False
            resource = package["resource"]
            db.execute(
                "INSERT INTO metric_packages VALUES (?,?,?,?,?,?,?)",
                (profile, package_id, package["period_start"], package["period_end"],
                 package["generated_at"], resource["hermes_version"], ingested_at),
            )
            for metric in package["metrics"]:
                db.execute(
                    "INSERT INTO metric_counters VALUES (?,?,?,?,?)",
                    (profile, package_id, metric["name"],
                     json.dumps(metric["dimensions"], sort_keys=True, separators=(",", ":")),
                     int(metric["value"])),
                )
        return True

    def upsert_health(self, row: dict[str, Any]) -> None:
        state_tuple = (
            row["release_id"], row["service_state"], row["telegram_state"],
            row["error_code"], 1 if row["needs_attention"] else 0, row["code_version"],
        )
        with self.connect() as db:
            previous = db.execute(
                "SELECT release_id,service_state,telegram_state,error_code,needs_attention,code_version FROM runtime_health WHERE profile=?",
                (row["profile"],),
            ).fetchone()
            previous_tuple = tuple(previous) if previous is not None else None
            if previous_tuple != state_tuple:
                db.execute(
                    "INSERT INTO health_events(profile,release_id,service_state,telegram_state,error_code,needs_attention,code_version,observed_at) VALUES (?,?,?,?,?,?,?,?)",
                    (row["profile"], *state_tuple, row["observed_at"]),
                )
            db.execute("""
                INSERT INTO runtime_health(
                    profile, release_id, service_state, telegram_state, error_code,
                    needs_attention, code_version, active_agents, observed_at
                ) VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(profile) DO UPDATE SET
                    release_id=excluded.release_id, service_state=excluded.service_state,
                    telegram_state=excluded.telegram_state, error_code=excluded.error_code,
                    needs_attention=excluded.needs_attention, code_version=excluded.code_version,
                    active_agents=excluded.active_agents, observed_at=excluded.observed_at
            """, (
                row["profile"], row["release_id"], row["service_state"],
                row["telegram_state"], row["error_code"],
                1 if row["needs_attention"] else 0, row["code_version"],
                int(row["active_agents"]), row["observed_at"],
            ))

    def record_update_attempt(self, row: dict[str, str]) -> None:
        with self.connect() as db:
            db.execute("""
                INSERT OR IGNORE INTO update_attempts(
                    attempt_id, profile, track, channel, from_release, to_release,
                    outcome, error_code, started_at, finished_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """, tuple(row[key] for key in (
                "attempt_id", "profile", "track", "channel", "from_release",
                "to_release", "outcome", "error_code", "started_at", "finished_at",
            )))

    def get_rollout(self, track: str, channel: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM rollout_state WHERE track=? AND channel=?",
                (track, channel),
            ).fetchone()
        return dict(row) if row is not None else None

    def set_rollout(self, row: dict[str, Any]) -> None:
        with self.connect() as db:
            db.execute("""
                INSERT INTO rollout_state(
                    track, channel, release_id, rollout_percent, healthy_cycles,
                    paused, updated_at
                ) VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(track, channel) DO UPDATE SET
                    release_id=excluded.release_id,
                    rollout_percent=excluded.rollout_percent,
                    healthy_cycles=excluded.healthy_cycles,
                    paused=excluded.paused,
                    updated_at=excluded.updated_at
            """, (
                row["track"], row["channel"], row["release_id"],
                int(row["rollout_percent"]), int(row["healthy_cycles"]),
                1 if row["paused"] else 0, row["updated_at"],
            ))
