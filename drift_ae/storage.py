"""SQLite metadata store for labels, calibration commits and diagnostics."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable


class MetadataStore:
    def __init__(self, state_dir: str | Path):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.state_dir / "metadata.sqlite"
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS windows (
                    window_id TEXT PRIMARY KEY,
                    window_index INTEGER,
                    segment_id TEXT,
                    true_label INTEGER,
                    polluted_label INTEGER,
                    corrected_label TEXT,
                    effective_label INTEGER,
                    score REAL,
                    severity TEXT,
                    is_anomaly INTEGER,
                    threshold_version INTEGER,
                    feature_checksum TEXT,
                    updated_batch INTEGER
                );

                CREATE TABLE IF NOT EXISTS calibration_runs (
                    run_id TEXT PRIMARY KEY,
                    batch_id INTEGER,
                    reason TEXT,
                    status TEXT,
                    old_model_version INTEGER,
                    new_model_version INTEGER,
                    budget_json TEXT,
                    propagation_json TEXT,
                    error_code TEXT
                );

                CREATE TABLE IF NOT EXISTS diagnostics (
                    record_id TEXT PRIMARY KEY,
                    window_id TEXT,
                    batch_id INTEGER,
                    path TEXT,
                    record_checksum TEXT
                );
                """
            )
            conn.commit()

    def upsert_window(self, record: dict[str, Any]) -> None:
        effective = record.get("effective_label")
        if effective is None:
            effective = self._effective_label(
                record.get("polluted_label", 0),
                record.get("corrected_label"),
            )
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO windows(
                    window_id,window_index,segment_id,true_label,polluted_label,
                    corrected_label,effective_label,score,severity,is_anomaly,
                    threshold_version,feature_checksum,updated_batch
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(window_id) DO UPDATE SET
                    window_index=excluded.window_index,
                    segment_id=excluded.segment_id,
                    true_label=excluded.true_label,
                    polluted_label=excluded.polluted_label,
                    corrected_label=excluded.corrected_label,
                    effective_label=excluded.effective_label,
                    score=excluded.score,
                    severity=excluded.severity,
                    is_anomaly=excluded.is_anomaly,
                    threshold_version=excluded.threshold_version,
                    feature_checksum=excluded.feature_checksum,
                    updated_batch=excluded.updated_batch
                """,
                (
                    record["window_id"],
                    record.get("window_index"),
                    record.get("segment_id"),
                    record.get("true_label"),
                    record.get("polluted_label", 0),
                    record.get("corrected_label"),
                    int(effective),
                    record.get("score"),
                    record.get("severity"),
                    None if record.get("is_anomaly") is None else int(bool(record["is_anomaly"])),
                    record.get("threshold_version"),
                    record.get("feature_checksum"),
                    record.get("batch_id"),
                ),
            )
            conn.commit()

    def correct_label(self, window_id: str, corrected_label: str) -> int:
        value = 1 if corrected_label == "anomaly" else 0
        with self.connect() as conn:
            row = conn.execute(
                "SELECT polluted_label FROM windows WHERE window_id=?",
                (window_id,),
            ).fetchone()
            polluted = int(row["polluted_label"]) if row else value
            conn.execute(
                "UPDATE windows SET corrected_label=?, effective_label=? WHERE window_id=?",
                (corrected_label, value, window_id),
            )
            if conn.total_changes == 0 or row is None:
                conn.execute(
                    "INSERT INTO windows(window_id,polluted_label,corrected_label,effective_label) VALUES(?,?,?,?)",
                    (window_id, polluted, corrected_label, value),
                )
            conn.commit()
        return value

    def effective_labels(self) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute("SELECT window_id,effective_label,polluted_label,corrected_label FROM windows").fetchall()
        result = {}
        for row in rows:
            if row["corrected_label"] == "normal":
                result[row["window_id"]] = 0
            elif row["corrected_label"] == "anomaly":
                result[row["window_id"]] = 1
            else:
                result[row["window_id"]] = int(row["polluted_label"] or 0)
        return result

    def record_calibration(self, run: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO calibration_runs(
                    run_id,batch_id,reason,status,old_model_version,new_model_version,
                    budget_json,propagation_json,error_code
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    run["run_id"],
                    run.get("batch_id"),
                    run.get("reason"),
                    run.get("status", "committed"),
                    run.get("old_model_version"),
                    run.get("new_model_version"),
                    json.dumps(run.get("budget"), sort_keys=True, default=str),
                    json.dumps(run.get("propagation"), sort_keys=True, default=str),
                    run.get("error_code"),
                ),
            )
            conn.commit()

    def record_diagnostic(self, record_id: str, window_id: str, batch_id: int, path: Path, checksum: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO diagnostics(record_id,window_id,batch_id,path,record_checksum) VALUES(?,?,?,?,?)",
                (record_id, window_id, batch_id, str(path), checksum),
            )
            conn.commit()

    @staticmethod
    def _effective_label(polluted_label: int, corrected_label: str | None) -> int:
        if corrected_label == "normal":
            return 0
        if corrected_label == "anomaly":
            return 1
        return int(polluted_label)
