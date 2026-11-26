from __future__ import annotations

import fcntl
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .hashing import canonical_dumps


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS revisions (
    revision_id TEXT PRIMARY KEY,
    parent_revision_id TEXT,
    patch_id TEXT,
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    report_hash TEXT NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    created_clock_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS revision_reports (
    revision_id TEXT PRIMARY KEY REFERENCES revisions(revision_id),
    report_json TEXT NOT NULL,
    report_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS work_units (
    unit_key TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('running','committed')),
    attempt INTEGER NOT NULL DEFAULT 1,
    input_hash TEXT NOT NULL,
    result_hash TEXT,
    logical_clock_ms INTEGER NOT NULL,
    updated_clock_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS window_assignments (
    revision_id TEXT NOT NULL,
    request_seq INTEGER NOT NULL,
    window_no INTEGER NOT NULL,
    slot_index INTEGER NOT NULL,
    service_name TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    digest TEXT NOT NULL,
    PRIMARY KEY(revision_id, request_seq)
);
CREATE TABLE IF NOT EXISTS window_watermarks (
    revision_id TEXT NOT NULL,
    window_no INTEGER NOT NULL,
    watermark_clock_ms INTEGER NOT NULL,
    request_count INTEGER NOT NULL,
    window_start_ms INTEGER NOT NULL,
    window_end_ms INTEGER NOT NULL,
    summary_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(revision_id, window_no)
);
CREATE TABLE IF NOT EXISTS window_summaries (
    revision_id TEXT NOT NULL,
    window_no INTEGER NOT NULL,
    instance_id TEXT NOT NULL,
    service_name TEXT NOT NULL,
    request_count INTEGER NOT NULL,
    request_set_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(revision_id, window_no, instance_id)
);
CREATE TABLE IF NOT EXISTS ledger_entries (
    revision_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    entry_key TEXT NOT NULL,
    window_no INTEGER NOT NULL,
    instance_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(revision_id, entry_key),
    UNIQUE(revision_id, seq)
);
CREATE TABLE IF NOT EXISTS metric_values (
    revision_id TEXT NOT NULL,
    value_key TEXT NOT NULL,
    window_no INTEGER NOT NULL,
    instance_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    fixed_value INTEGER NOT NULL,
    decimal_value TEXT NOT NULL,
    scale INTEGER NOT NULL,
    value_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(revision_id, value_key)
);
CREATE TABLE IF NOT EXISTS threshold_view (
    revision_id TEXT NOT NULL,
    threshold_key TEXT NOT NULL,
    service_level TEXT NOT NULL,
    metric TEXT NOT NULL,
    active INTEGER NOT NULL,
    source_version TEXT NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    generation INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(revision_id, threshold_key)
);
CREATE TABLE IF NOT EXISTS merge_decisions (
    revision_id TEXT NOT NULL,
    threshold_key TEXT NOT NULL,
    reason TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(revision_id, threshold_key)
);
CREATE TABLE IF NOT EXISTS dependency_edges (
    revision_id TEXT NOT NULL,
    edge_key TEXT NOT NULL,
    service_level TEXT NOT NULL,
    depends_on_service_level TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(revision_id, edge_key)
);
CREATE TABLE IF NOT EXISTS conclusions (
    revision_id TEXT NOT NULL,
    conclusion_key TEXT NOT NULL,
    window_no INTEGER NOT NULL,
    instance_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    capacity_state TEXT NOT NULL,
    capacity_ok INTEGER,
    conclusion_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(revision_id, conclusion_key)
);
CREATE TABLE IF NOT EXISTS precision_comparisons (
    revision_id TEXT NOT NULL,
    comparison_key TEXT NOT NULL,
    obs_key TEXT NOT NULL,
    metric TEXT NOT NULL,
    fixed_value TEXT NOT NULL,
    decimal_value TEXT NOT NULL,
    absolute_deviation TEXT NOT NULL,
    relative_deviation TEXT NOT NULL,
    conclusion_flipped INTEGER NOT NULL,
    ledger_seqs_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(revision_id, comparison_key)
);
CREATE TABLE IF NOT EXISTS sensitivity_records (
    revision_id TEXT NOT NULL,
    sensitivity_key TEXT NOT NULL,
    metric TEXT NOT NULL,
    pair_kind TEXT NOT NULL,
    sensitive INTEGER NOT NULL,
    would_flip INTEGER NOT NULL,
    record_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(revision_id, sensitivity_key)
);
CREATE TABLE IF NOT EXISTS invalidation_records (
    revision_id TEXT NOT NULL,
    patch_id TEXT NOT NULL,
    closure_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(revision_id, patch_id)
);
CREATE TABLE IF NOT EXISTS logical_clock_watermark (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    clock_ms INTEGER NOT NULL,
    revision_id TEXT NOT NULL
);
"""


class StateStore:
    def __init__(self, out_dir: str | Path):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.out_dir / "capacity_state.sqlite"
        self.lock_path = self.out_dir / "engine.lock"
        self._lock_handle = None

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO logical_clock_watermark(id, clock_ms, revision_id) VALUES (1, -1, 'none')"
            )
            conn.commit()

    @contextmanager
    def exclusive_lock(self) -> Iterator[None]:
        self._lock_handle = self.lock_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_EX)
            self._lock_handle.seek(0)
            self._lock_handle.truncate()
            self._lock_handle.write(f"pid={__import__('os').getpid()}\n")
            self._lock_handle.flush()
            yield
        finally:
            fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)
            self._lock_handle.close()
            self._lock_handle = None

    def is_revision_committed(self, revision_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT state FROM revisions WHERE revision_id = ?", (revision_id,)
            ).fetchone()
        return bool(row and row["state"] == "committed")

    def load_report(self, revision_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT report_json FROM revision_reports WHERE revision_id = ?", (revision_id,)
            ).fetchone()
        if not row:
            raise KeyError(revision_id)
        return json.loads(row["report_json"])

    def claim_unit(
        self,
        *,
        unit_key: str,
        revision_id: str,
        stage: str,
        input_hash: str,
        logical_clock_ms: int,
    ) -> bool:
        """Return True when an already-committed unit can be skipped."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT state, attempt FROM work_units WHERE unit_key = ?", (unit_key,)
            ).fetchone()
            if row and row["state"] == "committed":
                conn.commit()
                return True
            if row is None:
                conn.execute(
                    """
                    INSERT INTO work_units(unit_key, revision_id, stage, state, attempt,
                                          input_hash, logical_clock_ms, updated_clock_ms)
                    VALUES (?, ?, ?, 'running', 1, ?, ?, ?)
                    """,
                    (unit_key, revision_id, stage, input_hash, logical_clock_ms, logical_clock_ms),
                )
            else:
                conn.execute(
                    """
                    UPDATE work_units
                       SET state='running', attempt=attempt+1, input_hash=?,
                           logical_clock_ms=?, updated_clock_ms=?
                     WHERE unit_key=?
                    """,
                    (input_hash, logical_clock_ms, logical_clock_ms, unit_key),
                )
            conn.commit()
        return False

    def complete_unit(self, unit_key: str, result_hash: str) -> None:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE work_units SET state='committed', result_hash=? WHERE unit_key=?",
                (result_hash, unit_key),
            )
            conn.commit()

    def save_window_stage(self, report: dict[str, Any]) -> None:
        revision_id = report["revision_id"]
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for assignment in report["window_assignments"]:
                digest = assignment.get("digest") or assignment_digest(assignment)
                conn.execute(
                    """
                    INSERT OR REPLACE INTO window_assignments
                    (revision_id, request_seq, window_no, slot_index, service_name, instance_id, digest)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        revision_id,
                        assignment["request_seq"],
                        assignment["window_no"],
                        assignment["slot_index"],
                        assignment["service_name"],
                        assignment["instance_id"],
                        digest,
                    ),
                )
            for watermark in report["window_watermarks"]:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO window_watermarks
                    (revision_id, window_no, watermark_clock_ms, request_count,
                     window_start_ms, window_end_ms, summary_digest, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        revision_id,
                        watermark["window_no"],
                        watermark["watermark_clock_ms"],
                        watermark["request_count"],
                        watermark["window_start_ms"],
                        watermark["window_end_ms"],
                        watermark["summary_digest"],
                        canonical_dumps(watermark),
                    ),
                )
            for summary in report["window_summaries"]:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO window_summaries
                    (revision_id, window_no, instance_id, service_name, request_count,
                     request_set_digest, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        revision_id,
                        summary["window_no"],
                        summary["instance_id"],
                        summary["service_name"],
                        summary["request_count"],
                        summary["request_set_digest"],
                        canonical_dumps(summary),
                    ),
                )
            conn.commit()

    def save_conversion_stage(self, report: dict[str, Any]) -> None:
        revision_id = report["revision_id"]
        evaluation = report["evaluation"]
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._upsert_ledger_and_values(conn, revision_id, evaluation)
            conn.commit()

    def save_threshold_stage(self, report: dict[str, Any]) -> None:
        revision_id = report["revision_id"]
        merge = report["threshold_merge"]
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._upsert_thresholds(conn, revision_id, merge)
            conn.commit()

    def save_revision_report(self, report: dict[str, Any]) -> None:
        revision_id = report["revision_id"]
        evaluation = report["evaluation"]
        merge = report["threshold_merge"]
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT OR REPLACE INTO revisions
                (revision_id, parent_revision_id, patch_id, kind, state, report_hash,
                 logical_clock_ms, created_clock_ms)
                VALUES (?, ?, ?, ?, 'committed', ?, ?, ?)
                """,
                (
                    revision_id,
                    report.get("parent_revision_id"),
                    report.get("patch_id"),
                    report["kind"],
                    report["report_digest"],
                    report["logical_clock_watermark"],
                    report["logical_clock_watermark"],
                ),
            )
            conn.execute(
                "INSERT OR REPLACE INTO revision_reports(revision_id, report_json, report_hash) VALUES (?, ?, ?)",
                (revision_id, canonical_dumps(report), report["report_digest"]),
            )
            self._upsert_ledger_and_values(conn, revision_id, evaluation)
            self._upsert_thresholds(conn, revision_id, merge)
            for conclusion in evaluation["conclusions"]:
                key = f"{conclusion['obs_key']}:{conclusion['metric']}"
                capacity_ok = conclusion.get("capacity_ok")
                conn.execute(
                    """
                    INSERT OR REPLACE INTO conclusions
                    (revision_id, conclusion_key, window_no, instance_id, metric,
                     capacity_state, capacity_ok, conclusion_digest, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        revision_id,
                        key,
                        conclusion["window_no"],
                        conclusion["instance_id"],
                        conclusion["metric"],
                        conclusion["capacity_state"],
                        None if capacity_ok is None else int(capacity_ok),
                        conclusion["conclusion_digest"],
                        canonical_dumps(conclusion),
                    ),
                )
                flipped = bool(conclusion.get("conclusion_flipped_between_fixed_and_decimal", False))
                matching_value = next(
                    item
                    for item in evaluation["values"]
                    if item["obs_key"] == conclusion["obs_key"] and item["metric"] == conclusion["metric"]
                )
                comparison_key = key
                conn.execute(
                    """
                    INSERT OR REPLACE INTO precision_comparisons
                    (revision_id, comparison_key, obs_key, metric, fixed_value, decimal_value,
                     absolute_deviation, relative_deviation, conclusion_flipped,
                     ledger_seqs_json, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        revision_id,
                        comparison_key,
                        conclusion["obs_key"],
                        conclusion["metric"],
                        matching_value["fixed_physical_value"],
                        matching_value["decimal_value"],
                        matching_value["absolute_deviation"],
                        matching_value["relative_deviation"],
                        int(flipped),
                        canonical_dumps(matching_value["ledger_seqs"]),
                        canonical_dumps({"value": matching_value, "conclusion": conclusion}),
                    ),
                )
            for record in evaluation["sensitivity_assessments"]:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO sensitivity_records
                    (revision_id, sensitivity_key, metric, pair_kind, sensitive, would_flip,
                     record_digest, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        revision_id,
                        record["sensitivity_key"],
                        record["metric"],
                        record["pair_kind"],
                        int(record["sensitive"]),
                        int(record["would_flip_at_higher_scale"]),
                        record["record_digest"],
                        canonical_dumps(record),
                    ),
                )
            if report.get("invalidation"):
                invalidation = report["invalidation"]
                conn.execute(
                    """
                    INSERT OR REPLACE INTO invalidation_records
                    (revision_id, patch_id, closure_digest, payload_json)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        revision_id,
                        report["patch_id"],
                        invalidation["closure_digest"],
                        canonical_dumps(invalidation),
                    ),
                )
            conn.execute(
                """
                INSERT INTO logical_clock_watermark(id, clock_ms, revision_id)
                VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    clock_ms=excluded.clock_ms,
                    revision_id=excluded.revision_id
                """,
                (report["logical_clock_watermark"], revision_id),
            )
            if report.get("patch_id"):
                conn.execute(
                    """
                    UPDATE work_units
                       SET state='committed', result_hash=?, updated_clock_ms=?
                     WHERE unit_key=?
                    """,
                    (
                        report["report_digest"],
                        report["logical_clock_watermark"],
                        f"patch_commit:{report['patch_id']}",
                    ),
                )
            conn.commit()

    def _upsert_ledger_and_values(
        self, conn: sqlite3.Connection, revision_id: str, evaluation: dict[str, Any]
    ) -> None:
        for entry in evaluation["ledger_entries"]:
            conn.execute(
                """
                INSERT OR REPLACE INTO ledger_entries
                (revision_id, seq, entry_key, window_no, instance_id, metric, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    revision_id,
                    int(entry["seq"]),
                    entry["entry_key"],
                    int(entry["window_no"]),
                    entry["instance_id"],
                    entry["metric"],
                    canonical_dumps(entry),
                ),
            )
        for value in evaluation["values"]:
            key = f"{value['obs_key']}:{value['metric']}"
            conn.execute(
                """
                INSERT OR REPLACE INTO metric_values
                (revision_id, value_key, window_no, instance_id, metric, fixed_value,
                 decimal_value, scale, value_digest, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    revision_id,
                    key,
                    value["window_no"],
                    value["instance_id"],
                    value["metric"],
                    int(value["fixed_value"]),
                    value["decimal_value"],
                    int(value["scale"]),
                    value["value_digest"],
                    canonical_dumps(value),
                ),
            )

    def _upsert_thresholds(
        self, conn: sqlite3.Connection, revision_id: str, merge: dict[str, Any]
    ) -> None:
        for threshold in merge["thresholds"].values():
            conn.execute(
                """
                INSERT OR REPLACE INTO threshold_view
                (revision_id, threshold_key, service_level, metric, active, source_version,
                 logical_clock_ms, generation, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    revision_id,
                    threshold["threshold_key"],
                    threshold["service_level"],
                    threshold["metric"],
                    int(bool(threshold.get("active"))),
                    threshold["source_version"],
                    int(threshold["logical_clock_ms"]),
                    int(threshold["generation"]),
                    canonical_dumps(threshold),
                ),
            )
        for decision in merge["decisions"]:
            conn.execute(
                """
                INSERT OR REPLACE INTO merge_decisions
                (revision_id, threshold_key, reason, payload_json)
                VALUES (?, ?, ?, ?)
                """,
                (revision_id, decision["threshold_key"], decision["reason"], canonical_dumps(decision)),
            )
        for edge in merge["dependency_edges"]:
            conn.execute(
                """
                INSERT OR REPLACE INTO dependency_edges
                (revision_id, edge_key, service_level, depends_on_service_level, payload_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    revision_id,
                    edge["edge_key"],
                    edge["service_level"],
                    edge["depends_on_service_level"],
                    canonical_dumps(edge),
                ),
            )


def assignment_digest(assignment: dict[str, Any]) -> str:
    from .hashing import sha256_object

    return sha256_object(
        {
            "request_seq": assignment["request_seq"],
            "window_no": assignment["window_no"],
            "instance_id": assignment["instance_id"],
        }
    )
