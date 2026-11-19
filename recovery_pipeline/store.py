"""SQLite-backed persistent state for the recovery pipeline.

All multi-row writes happen inside a single transaction (BEGIN IMMEDIATE)
so readers only ever observe fully committed stages. The store keeps:
plan versions with a single current pointer (whole-package replacement);
per-service stage, committed action watermark, traffic ladder position;
action intents (to tell committed from half-committed after a crash);
migration decisions, logical-clock watermark and an audit log.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from .models import canonical_json

SCHEMA = """
CREATE TABLE IF NOT EXISTS plan_versions (
    version INTEGER PRIMARY KEY,
    content_digest TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    migration_json TEXT NOT NULL,
    is_current INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS service_stage (
    plan_version INTEGER NOT NULL,
    service TEXT NOT NULL,
    stage TEXT NOT NULL,
    traffic_rung INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (plan_version, service)
);
CREATE TABLE IF NOT EXISTS action_intent (
    plan_version INTEGER NOT NULL,
    service TEXT NOT NULL,
    action_id TEXT NOT NULL,
    action_version TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (plan_version, service, action_id)
);
CREATE TABLE IF NOT EXISTS action_watermark (
    plan_version INTEGER NOT NULL,
    service TEXT NOT NULL,
    action_id TEXT NOT NULL,
    action_version TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    clock_ms INTEGER NOT NULL,
    PRIMARY KEY (plan_version, service, action_id)
);
CREATE TABLE IF NOT EXISTS decision_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_version INTEGER NOT NULL,
    service TEXT NOT NULL,
    decision TEXT NOT NULL,
    trigger_source TEXT NOT NULL,
    clock_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS clock_watermark (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    clock_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS decision_uniq ON decision_log
    (plan_version, service, decision, trigger_source);
"""


class Store:
    def __init__(self, db_path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        conn = self.connect()
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def current_plan_version(self):
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT version FROM plan_versions WHERE is_current = 1"
            ).fetchone()
            return row["version"] if row else None
        finally:
            conn.close()

    def install_plan(self, version: int, plan_view: dict,
                     migration_map: dict) -> None:
        """Stage the new plan rows and flip the current pointer atomically."""
        plan_json = canonical_json(plan_view)
        digest = hashlib.sha256(plan_json.encode("utf-8")).hexdigest()
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR REPLACE INTO plan_versions"
                " (version, content_digest, plan_json, migration_json, is_current)"
                " VALUES (?, ?, ?, ?, 0)",
                (version, digest, plan_json, canonical_json(migration_map)),
            )
            conn.execute("UPDATE plan_versions SET is_current = 0")
            conn.execute(
                "UPDATE plan_versions SET is_current = 1 WHERE version = ?",
                (version,),
            )
            for service in plan_view["services"]:
                conn.execute(
                    "INSERT OR IGNORE INTO service_stage"
                    " (plan_version, service, stage, traffic_rung)"
                    " VALUES (?, ?, 'pending', 0)",
                    (version, service),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def load_plan_view(self, version: int) -> dict:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT plan_json, migration_json FROM plan_versions"
                " WHERE version = ?", (version,),
            ).fetchone()
            if row is None:
                raise KeyError(f"plan version {version} not found")
            view = json.loads(row["plan_json"])
            view["migration_map"] = json.loads(row["migration_json"])
            return view
        finally:
            conn.close()

    def audit(self, event: str, detail: dict) -> None:
        conn = self.connect()
        try:
            conn.execute(
                "INSERT INTO audit_log (event, detail_json) VALUES (?, ?)",
                (event, canonical_json(detail)),
            )
            conn.commit()
        finally:
            conn.close()

    def audit_events(self) -> list:
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT event, detail_json FROM audit_log ORDER BY seq"
            ).fetchall()
            return [(r["event"], json.loads(r["detail_json"])) for r in rows]
        finally:
            conn.close()
