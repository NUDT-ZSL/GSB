from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .aggregate import decode_jsonable, dumps_json
from .errors import BusinessRuleError, ErrorCode
from .model import FormSpec


def form_to_json(form: FormSpec) -> str:
    return dumps_json(asdict(form))


def form_from_json(payload: str | dict[str, Any]) -> FormSpec:
    raw = json.loads(payload) if isinstance(payload, str) else payload
    data = decode_jsonable(raw)
    return FormSpec(
        form_id=data["form_id"],
        window_order=tuple(data.get("window_order", [])),
        operators=dict(data.get("operators", {})),
        group_orders={key: tuple(value) for key, value in data.get("group_orders", {}).items()},
        switch_cost_ms=data.get("switch_cost_ms"),
        setup_cost_ms=data.get("setup_cost_ms"),
        projected_runtime_ms=data.get("projected_runtime_ms"),
        target_version=data.get("target_version"),
    )


class RuntimeStorage:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.init_schema()

    def close(self) -> None:
        self.conn.close()

    def init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS batches (
                seq INTEGER PRIMARY KEY,
                table_name TEXT NOT NULL,
                window_id TEXT NOT NULL,
                arrival_clock INTEGER NOT NULL,
                row_count INTEGER NOT NULL,
                digest TEXT NOT NULL,
                summary_json TEXT NOT NULL,
                metrics_json TEXT NOT NULL,
                committed_clock INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS window_state (
                window_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                watermark_seq INTEGER NOT NULL,
                form_version INTEGER NOT NULL,
                state_json TEXT NOT NULL,
                result_json TEXT,
                updated_clock INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS form_pointer (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                version INTEGER NOT NULL,
                form_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                updated_clock INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS form_versions (
                version INTEGER PRIMARY KEY,
                form_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_clock INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS drift_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT NOT NULL UNIQUE,
                window_id TEXT NOT NULL,
                table_name TEXT NOT NULL,
                clock_ms INTEGER NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS precision_flags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                flag_key TEXT NOT NULL UNIQUE,
                clock_ms INTEGER NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ledger_snapshot (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                payload_json TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: Any) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )

    def clock_watermark(self) -> int:
        value = self.get_meta("clock_watermark_ms", "0")
        return int(value or 0)

    def initialize_form(self, form: FormSpec, clock_ms: int) -> int:
        payload = form_to_json(form)
        with self.conn:
            row = self.conn.execute("SELECT version FROM form_pointer WHERE singleton=1").fetchone()
            if row is not None:
                return int(row["version"])
            self.conn.execute(
                "INSERT INTO form_pointer(singleton, version, form_id, payload_json, updated_clock) "
                "VALUES(1, 1, ?, ?, ?)",
                (form.form_id, payload, clock_ms),
            )
            self.conn.execute(
                "INSERT INTO form_versions(version, form_id, payload_json, created_clock) VALUES(1, ?, ?, ?)",
                (form.form_id, payload, clock_ms),
            )
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES('clock_watermark_ms', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=MAX(CAST(value AS INTEGER), CAST(excluded.value AS INTEGER))",
                (str(clock_ms),),
            )
        return 1

    def current_form(self) -> tuple[int, FormSpec] | None:
        row = self.conn.execute("SELECT version, payload_json FROM form_pointer WHERE singleton=1").fetchone()
        if row is None:
            return None
        return int(row["version"]), form_from_json(row["payload_json"])

    def get_form_version(self, version: int) -> FormSpec | None:
        row = self.conn.execute("SELECT payload_json FROM form_versions WHERE version=?", (version,)).fetchone()
        return form_from_json(row["payload_json"]) if row else None

    def switch_form(
        self,
        new_form: FormSpec,
        clock_ms: int,
        *,
        requested_version: int | None = None,
        metadata: dict[str, str] | None = None,
    ) -> int:
        payload = form_to_json(new_form)
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            row = self.conn.execute("SELECT version FROM form_pointer WHERE singleton=1").fetchone()
            if row is None:
                current_version = 0
            else:
                current_version = int(row["version"])
            if requested_version is not None and requested_version <= current_version:
                raise BusinessRuleError(
                    ErrorCode.FORM_VERSION_ROLLBACK,
                    "form pointer version rollback is rejected",
                    {"current_version": current_version, "requested_version": requested_version},
                )
            new_version = requested_version or current_version + 1
            if new_version <= current_version:
                raise BusinessRuleError(
                    ErrorCode.FORM_VERSION_ROLLBACK,
                    "form pointer version must increase",
                    {"current_version": current_version, "new_version": new_version},
                )
            existing = self.conn.execute(
                "SELECT version FROM form_versions WHERE version=?", (new_version,)
            ).fetchone()
            if existing is not None:
                same = self.conn.execute(
                    "SELECT payload_json FROM form_versions WHERE version=?", (new_version,)
                ).fetchone()
                if same["payload_json"] != payload:
                    raise BusinessRuleError(
                        ErrorCode.FORM_VERSION_CONFLICT,
                        f"form version {new_version} already exists with a different payload",
                    )
            else:
                self.conn.execute(
                    "INSERT INTO form_versions(version, form_id, payload_json, created_clock) VALUES(?, ?, ?, ?)",
                    (new_version, new_form.form_id, payload, clock_ms),
                )
            self.conn.execute(
                "INSERT INTO form_pointer(singleton, version, form_id, payload_json, updated_clock) "
                "VALUES(1, ?, ?, ?, ?) "
                "ON CONFLICT(singleton) DO UPDATE SET version=excluded.version, form_id=excluded.form_id, "
                "payload_json=excluded.payload_json, updated_clock=excluded.updated_clock",
                (new_version, new_form.form_id, payload, clock_ms),
            )
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES('clock_watermark_ms', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=MAX(CAST(value AS INTEGER), CAST(excluded.value AS INTEGER))",
                (str(clock_ms),),
            )
            for key, value in (metadata or {}).items():
                self.conn.execute(
                    "INSERT INTO meta(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, str(value)),
                )
            self.conn.commit()
            return new_version
        except Exception:
            self.conn.rollback()
            raise

    def commit_batch(
        self,
        *,
        seq: int,
        table_name: str,
        window_id: str,
        arrival_clock: int,
        row_count: int,
        digest: str,
        summary: dict[str, Any],
        metrics: dict[str, Any],
        state_json: str,
        form_version: int,
        committed_clock: int,
    ) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO batches(seq, table_name, window_id, arrival_clock, row_count, digest, "
                "summary_json, metrics_json, committed_clock) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    seq,
                    table_name,
                    window_id,
                    arrival_clock,
                    row_count,
                    digest,
                    json.dumps(summary, ensure_ascii=False, sort_keys=True),
                    json.dumps(metrics, ensure_ascii=False, sort_keys=True, default=str),
                    committed_clock,
                ),
            )
            self.conn.execute(
                "INSERT INTO window_state(window_id, status, watermark_seq, form_version, state_json, "
                "result_json, updated_clock) VALUES(?, 'open', ?, ?, ?, NULL, ?) "
                "ON CONFLICT(window_id) DO UPDATE SET status='open', watermark_seq=excluded.watermark_seq, "
                "form_version=excluded.form_version, state_json=excluded.state_json, "
                "updated_clock=excluded.updated_clock",
                (window_id, seq, form_version, state_json, committed_clock),
            )
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES('clock_watermark_ms', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=MAX(CAST(value AS INTEGER), CAST(excluded.value AS INTEGER))",
                (str(max(committed_clock, arrival_clock)),),
            )

    def batch_exists(self, seq: int) -> bool:
        return self.conn.execute("SELECT 1 FROM batches WHERE seq=?", (seq,)).fetchone() is not None

    def committed_seqs(self) -> list[int]:
        return [int(row["seq"]) for row in self.conn.execute("SELECT seq FROM batches ORDER BY seq")]

    def batch_rows(self, window_id: str | None = None) -> list[sqlite3.Row]:
        if window_id is None:
            return list(self.conn.execute("SELECT * FROM batches ORDER BY seq"))
        return list(
            self.conn.execute("SELECT * FROM batches WHERE window_id=? ORDER BY seq", (window_id,))
        )

    def get_window_row(self, window_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM window_state WHERE window_id=?", (window_id,)).fetchone()

    def finalize_window(self, window_id: str, result: dict[str, Any], clock_ms: int) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE window_state SET status='finalized', result_json=?, updated_clock=? WHERE window_id=?",
                (dumps_json(result), clock_ms, window_id),
            )
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES('clock_watermark_ms', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=MAX(CAST(value AS INTEGER), CAST(excluded.value AS INTEGER))",
                (str(clock_ms),),
            )

    def finalize_window_and_switch(
        self,
        *,
        window_id: str,
        result: dict[str, Any],
        clock_ms: int,
        new_form: FormSpec | None,
        requested_version: int | None = None,
        metadata: dict[str, str] | None = None,
    ) -> int | None:
        """Atomically finalize a window and optionally replace the form pointer."""
        payload = form_to_json(new_form) if new_form is not None else None
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute(
                "UPDATE window_state SET status='finalized', result_json=?, updated_clock=? WHERE window_id=?",
                (dumps_json(result), clock_ms, window_id),
            )
            new_version: int | None = None
            if new_form is not None:
                row = self.conn.execute("SELECT version FROM form_pointer WHERE singleton=1").fetchone()
                current_version = int(row["version"]) if row else 0
                if requested_version is not None and requested_version <= current_version:
                    raise BusinessRuleError(
                        ErrorCode.FORM_VERSION_ROLLBACK,
                        "form pointer version rollback is rejected",
                        {"current_version": current_version, "requested_version": requested_version},
                    )
                new_version = requested_version or current_version + 1
                version_row = self.conn.execute(
                    "SELECT payload_json FROM form_versions WHERE version=?", (new_version,)
                ).fetchone()
                if version_row is None:
                    self.conn.execute(
                        "INSERT INTO form_versions(version, form_id, payload_json, created_clock) "
                        "VALUES(?, ?, ?, ?)",
                        (new_version, new_form.form_id, payload, clock_ms),
                    )
                self.conn.execute(
                    "INSERT INTO form_pointer(singleton, version, form_id, payload_json, updated_clock) "
                    "VALUES(1, ?, ?, ?, ?) "
                    "ON CONFLICT(singleton) DO UPDATE SET version=excluded.version, form_id=excluded.form_id, "
                    "payload_json=excluded.payload_json, updated_clock=excluded.updated_clock",
                    (new_version, new_form.form_id, payload, clock_ms),
                )
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES('clock_watermark_ms', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=MAX(CAST(value AS INTEGER), CAST(excluded.value AS INTEGER))",
                (str(clock_ms),),
            )
            for key, value in (metadata or {}).items():
                self.conn.execute(
                    "INSERT INTO meta(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, str(value)),
                )
            self.conn.commit()
            return new_version
        except Exception:
            self.conn.rollback()
            raise

    def save_drift_event(self, event: dict[str, Any]) -> bool:
        with self.conn:
            cursor = self.conn.execute(
                "INSERT OR IGNORE INTO drift_events(event_key, window_id, table_name, clock_ms, payload_json) "
                "VALUES(?, ?, ?, ?, ?)",
                (
                    event["event_key"],
                    event["window_id"],
                    event["table_name"],
                    int(event["clock_ms"]),
                    dumps_json(event),
                ),
            )
            return cursor.rowcount > 0

    def last_drift_clock(self) -> int | None:
        row = self.conn.execute("SELECT MAX(clock_ms) AS clock FROM drift_events").fetchone()
        return None if row["clock"] is None else int(row["clock"])

    def save_precision_flag(self, flag: dict[str, Any], clock_ms: int) -> bool:
        with self.conn:
            cursor = self.conn.execute(
                "INSERT OR IGNORE INTO precision_flags(flag_key, clock_ms, payload_json) VALUES(?, ?, ?)",
                (flag["flag_key"], clock_ms, dumps_json(flag)),
            )
            return cursor.rowcount > 0

    def save_ledger(self, ledger_dict: dict[str, Any]) -> None:
        payload = dumps_json(ledger_dict)
        with self.conn:
            self.conn.execute(
                "INSERT INTO ledger_snapshot(singleton, payload_json) VALUES(1, ?) "
                "ON CONFLICT(singleton) DO UPDATE SET payload_json=excluded.payload_json",
                (payload,),
            )

    def recovery_snapshot(self) -> dict[str, Any]:
        batches = [dict(row) for row in self.batch_rows()]
        windows = [dict(row) for row in self.conn.execute("SELECT * FROM window_state ORDER BY window_id")]
        form = self.current_form()
        drift = [
            dict(row)
            for row in self.conn.execute("SELECT event_key, window_id, table_name, clock_ms FROM drift_events ORDER BY id")
        ]
        precision = [
            dict(row) for row in self.conn.execute("SELECT flag_key, clock_ms FROM precision_flags ORDER BY id")
        ]
        return {
            "committed_seqs": [item["seq"] for item in batches],
            "committed_batch_count": len(batches),
            "windows": [
                {
                    "window_id": row["window_id"],
                    "status": row["status"],
                    "watermark_seq": row["watermark_seq"],
                    "form_version": row["form_version"],
                    "has_result": bool(row["result_json"]),
                }
                for row in windows
            ],
            "current_form_version": form[0] if form else None,
            "current_form_id": form[1].form_id if form else None,
            "drift_events": drift,
            "precision_flags": precision,
            "clock_watermark_ms": self.clock_watermark(),
        }
