"""SQLite persistence for the operation catalog, replay batches and snapshots."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .errors import StateError
from .segment import LogRecord, Segment, encode_record_payload
from .util import ensure_dir


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS operations (
    commit_seq INTEGER PRIMARY KEY,
    txn_id INTEGER NOT NULL,
    commit_ts_ms INTEGER,
    txn_size_bytes INTEGER,
    format_version INTEGER NOT NULL,
    segment_id INTEGER,
    segment_offset INTEGER,
    segment_filename TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS operation_payloads (
    commit_seq INTEGER PRIMARY KEY,
    payload BLOB NOT NULL,
    FOREIGN KEY (commit_seq) REFERENCES operations(commit_seq) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS replay_state (
    session_id TEXT PRIMARY KEY,
    params_version TEXT NOT NULL,
    estimator_version TEXT NOT NULL,
    replay_rate INTEGER NOT NULL,
    concurrency INTEGER NOT NULL,
    clock_origin_ms INTEGER NOT NULL,
    log_digest TEXT NOT NULL,
    segment_versions_json TEXT NOT NULL,
    last_commit_seq INTEGER NOT NULL,
    sample_count INTEGER NOT NULL,
    known_delay_count INTEGER NOT NULL,
    unknown_delay_count INTEGER NOT NULL,
    unknown_batch_count INTEGER NOT NULL,
    updated_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS replay_batches (
    session_id TEXT NOT NULL,
    batch_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    start_commit_seq INTEGER NOT NULL,
    end_commit_seq INTEGER NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    commit_seqs_json TEXT NOT NULL,
    PRIMARY KEY (session_id, batch_id)
);

CREATE TABLE IF NOT EXISTS delay_samples (
    session_id TEXT NOT NULL,
    sample_index INTEGER NOT NULL,
    commit_seq INTEGER NOT NULL,
    batch_id INTEGER NOT NULL,
    commit_version INTEGER NOT NULL,
    commit_ts_ms INTEGER,
    completion_ts_ms INTEGER NOT NULL,
    delay_ms INTEGER,
    status TEXT NOT NULL,
    PRIMARY KEY (session_id, commit_seq),
    UNIQUE (session_id, sample_index)
);

CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    manifest_json TEXT NOT NULL
);
"""


def _utc_now_text() -> str:
    # Wall time is informational only and is never used by deterministic replay.
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ProgressStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        ensure_dir(path.parent)
        self.connection = sqlite3.connect(str(path), timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(SCHEMA)
        self.connection.commit()

    def close(self) -> None:
        try:
            self.connection.close()
        except sqlite3.Error:
            pass

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def set_meta(self, key: str, value: Any) -> None:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, encoded),
            )

    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.connection.execute(
            "SELECT value FROM meta WHERE key=?", (key,)
        ).fetchone()
        if row is None:
            return default
        return json.loads(row["value"])

    def replace_operations(self, segments: Sequence[Segment]) -> None:
        rows: list[tuple[Any, ...]] = []
        payload_rows: list[tuple[bytes, int]] = []
        for segment in sorted(segments, key=lambda item: (item.segment_id, item.filename)):
            for record in segment.records:
                rows.append(
                    (
                        record.commit_seq,
                        record.txn_id,
                        record.commit_ts_ms,
                        record.txn_size_bytes,
                        record.format_version,
                        record.segment_id if record.segment_id is not None else segment.segment_id,
                        record.segment_offset,
                        segment.filename,
                    )
                )
                payload_rows.append(
                    (
                        record.commit_seq,
                        encode_record_payload(
                            record,
                            segment_id=segment.segment_id,
                            frame_offset=record.segment_offset,
                            filename=segment.filename,
                        ),
                    )
                )
        with self.transaction() as conn:
            conn.execute("DELETE FROM operation_payloads")
            conn.execute("DELETE FROM operations")
            conn.executemany(
                "INSERT INTO operations("
                "commit_seq, txn_id, commit_ts_ms, txn_size_bytes, format_version, "
                "segment_id, segment_offset, segment_filename"
                ") VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.executemany(
                "INSERT INTO operation_payloads(commit_seq, payload) VALUES(?, ?)",
                payload_rows,
            )

    def upsert_operations(self, records: Iterable[LogRecord], filename: str) -> None:
        with self.transaction() as conn:
            for record in records:
                conn.execute(
                    "INSERT INTO operations("
                    "commit_seq, txn_id, commit_ts_ms, txn_size_bytes, format_version, "
                    "segment_id, segment_offset, segment_filename"
                    ") VALUES(?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(commit_seq) DO UPDATE SET "
                    "txn_id=excluded.txn_id, commit_ts_ms=excluded.commit_ts_ms, "
                    "txn_size_bytes=excluded.txn_size_bytes, "
                    "format_version=excluded.format_version, "
                    "segment_id=excluded.segment_id, "
                    "segment_offset=excluded.segment_offset, "
                    "segment_filename=excluded.segment_filename",
                    (
                        record.commit_seq,
                        record.txn_id,
                        record.commit_ts_ms,
                        record.txn_size_bytes,
                        record.format_version,
                        record.segment_id,
                        record.segment_offset,
                        filename,
                    ),
                )
                payload = encode_record_payload(
                    record,
                    segment_id=record.segment_id,
                    frame_offset=record.segment_offset,
                    filename=filename,
                )
                conn.execute(
                    "INSERT INTO operation_payloads(commit_seq, payload) VALUES(?, ?) "
                    "ON CONFLICT(commit_seq) DO UPDATE SET payload=excluded.payload",
                    (record.commit_seq, payload),
                )

    def operation_rows(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM operations ORDER BY commit_seq"
        ).fetchall()
        return [dict(row) for row in rows]

    def operation_payloads(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT p.commit_seq, p.payload, o.txn_id, o.format_version, "
            "o.segment_filename FROM operation_payloads p "
            "JOIN operations o ON o.commit_seq=p.commit_seq ORDER BY p.commit_seq"
        ).fetchall()
        return [dict(row) for row in rows]

    def reset_session(
        self,
        *,
        session_id: str,
        params_version: str,
        estimator_version: str,
        replay_rate: int,
        concurrency: int,
        clock_origin_ms: int,
        log_digest: str,
        segment_versions: Sequence[int],
    ) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM delay_samples WHERE session_id=?", (session_id,))
            conn.execute("DELETE FROM replay_batches WHERE session_id=?", (session_id,))
            conn.execute("DELETE FROM replay_state WHERE session_id=?", (session_id,))
            conn.execute(
                "INSERT INTO replay_state("
                "session_id, params_version, estimator_version, replay_rate, concurrency, "
                "clock_origin_ms, log_digest, segment_versions_json, last_commit_seq, "
                "sample_count, known_delay_count, unknown_delay_count, unknown_batch_count, "
                "updated_utc"
                ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, 0, ?)",
                (
                    session_id,
                    params_version,
                    estimator_version,
                    replay_rate,
                    concurrency,
                    clock_origin_ms,
                    log_digest,
                    json.dumps(sorted(segment_versions)),
                    _utc_now_text(),
                ),
            )

    def get_state(self, session_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM replay_state WHERE session_id=?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        state = dict(row)
        state["segment_versions"] = json.loads(state.pop("segment_versions_json"))
        return state

    def update_session_topology(
        self,
        session_id: str,
        *,
        log_digest: str,
        segment_versions: Sequence[int],
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE replay_state SET log_digest=?, segment_versions_json=?, "
                "updated_utc=? WHERE session_id=?",
                (
                    log_digest,
                    json.dumps(sorted(segment_versions), separators=(",", ":")),
                    _utc_now_text(),
                    session_id,
                ),
            )

    def begin_batch(
        self,
        *,
        session_id: str,
        batch_id: int,
        start_commit_seq: int,
        end_commit_seq: int,
        start_ms: int,
        end_ms: int,
        commit_seqs: Sequence[int],
    ) -> None:
        with self.transaction() as conn:
            state = conn.execute(
                "SELECT session_id FROM replay_state WHERE session_id=?", (session_id,)
            ).fetchone()
            if state is None:
                raise StateError("replay session does not exist")
            existing = conn.execute(
                "SELECT status FROM replay_batches WHERE session_id=? AND batch_id=?",
                (session_id, batch_id),
            ).fetchone()
            if existing is not None and existing["status"] == "COMPLETE":
                raise StateError("cannot reopen a completed replay batch")
            conn.execute(
                "INSERT INTO replay_batches("
                "session_id, batch_id, status, start_commit_seq, end_commit_seq, "
                "start_ms, end_ms, commit_seqs_json"
                ") VALUES(?, ?, 'IN_PROGRESS', ?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id, batch_id) DO UPDATE SET "
                "status='IN_PROGRESS', start_commit_seq=excluded.start_commit_seq, "
                "end_commit_seq=excluded.end_commit_seq, start_ms=excluded.start_ms, "
                "end_ms=excluded.end_ms, commit_seqs_json=excluded.commit_seqs_json",
                (
                    session_id,
                    batch_id,
                    start_commit_seq,
                    end_commit_seq,
                    start_ms,
                    end_ms,
                    json.dumps(list(commit_seqs), separators=(",", ":")),
                ),
            )
            updated = _utc_now_text()
            conn.execute(
                "UPDATE replay_state SET updated_utc=? WHERE session_id=?",
                (updated, session_id),
            )

    def commit_batch(
        self,
        *,
        session_id: str,
        batch_id: int,
        samples: Sequence[dict[str, Any]],
    ) -> None:
        with self.transaction() as conn:
            batch = conn.execute(
                "SELECT * FROM replay_batches WHERE session_id=? AND batch_id=?",
                (session_id, batch_id),
            ).fetchone()
            if batch is None:
                raise StateError("batch must be registered before it can be committed")
            if batch["status"] != "IN_PROGRESS":
                raise StateError(
                    f"batch {batch_id} is {batch['status']}, not IN_PROGRESS"
                )
            max_index_row = conn.execute(
                "SELECT COALESCE(MAX(sample_index), 0) AS value FROM delay_samples "
                "WHERE session_id=?",
                (session_id,),
            ).fetchone()
            next_index = int(max_index_row["value"])
            for sample in samples:
                next_index += 1
                conn.execute(
                    "INSERT INTO delay_samples("
                    "session_id, sample_index, commit_seq, batch_id, commit_version, "
                    "commit_ts_ms, completion_ts_ms, delay_ms, status"
                    ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        session_id,
                        next_index,
                        int(sample["commit_seq"]),
                        batch_id,
                        int(sample["commit_version"]),
                        sample.get("commit_ts_ms"),
                        int(sample["completion_ts_ms"]),
                        sample.get("delay_ms"),
                        str(sample["status"]),
                    ),
                )
            conn.execute(
                "UPDATE replay_batches SET status='COMPLETE' "
                "WHERE session_id=? AND batch_id=?",
                (session_id, batch_id),
            )
            counts = conn.execute(
                "SELECT COUNT(*) AS sample_count, "
                "SUM(CASE WHEN delay_ms IS NULL THEN 0 ELSE 1 END) AS known_count, "
                "SUM(CASE WHEN delay_ms IS NULL THEN 1 ELSE 0 END) AS unknown_count "
                "FROM delay_samples WHERE session_id=?",
                (session_id,),
            ).fetchone()
            unknown_batches = conn.execute(
                "SELECT COUNT(*) AS value FROM replay_batches "
                "WHERE session_id=? AND status='UNKNOWN'",
                (session_id,),
            ).fetchone()["value"]
            conn.execute(
                "UPDATE replay_state SET last_commit_seq=?, sample_count=?, "
                "known_delay_count=?, unknown_delay_count=?, unknown_batch_count=?, "
                "updated_utc=? WHERE session_id=?",
                (
                    int(batch["end_commit_seq"]),
                    int(counts["sample_count"] or 0),
                    int(counts["known_count"] or 0),
                    int(counts["unknown_count"] or 0),
                    int(unknown_batches or 0),
                    _utc_now_text(),
                    session_id,
                ),
            )

    def mark_incomplete_unknown(self, session_id: str) -> dict[str, Any]:
        with self.transaction() as conn:
            state = conn.execute(
                "SELECT * FROM replay_state WHERE session_id=?", (session_id,)
            ).fetchone()
            if state is None:
                return {
                    "recovered_last_commit_seq": None,
                    "unknown_batch_ids": [],
                    "unknown_batch_count": 0,
                    "rebuilt_sample_count": 0,
                    "known_delay_count": 0,
                    "unknown_delay_count": 0,
                    "state_existed": False,
                }
            incomplete = conn.execute(
                "SELECT batch_id, commit_seqs_json FROM replay_batches "
                "WHERE session_id=? AND status='IN_PROGRESS' ORDER BY batch_id",
                (session_id,),
            ).fetchall()
            unknown_ids: list[int] = []
            for batch in incomplete:
                seqs = json.loads(batch["commit_seqs_json"])
                unknown_ids.append(int(batch["batch_id"]))
                if seqs:
                    placeholders = ",".join("?" for _ in seqs)
                    conn.execute(
                        f"DELETE FROM delay_samples WHERE session_id=? "
                        f"AND commit_seq IN ({placeholders})",
                        [session_id, *seqs],
                    )
                conn.execute(
                    "UPDATE replay_batches SET status='UNKNOWN' "
                    "WHERE session_id=? AND batch_id=?",
                    (session_id, batch["batch_id"]),
                )
            counts = conn.execute(
                "SELECT COUNT(*) AS sample_count, "
                "SUM(CASE WHEN delay_ms IS NULL THEN 0 ELSE 1 END) AS known_count, "
                "SUM(CASE WHEN delay_ms IS NULL THEN 1 ELSE 0 END) AS unknown_count "
                "FROM delay_samples WHERE session_id=?",
                (session_id,),
            ).fetchone()
            unknown_rows = conn.execute(
                "SELECT batch_id FROM replay_batches "
                "WHERE session_id=? AND status='UNKNOWN' ORDER BY batch_id",
                (session_id,),
            ).fetchall()
            unknown_total = conn.execute(
                "SELECT COUNT(*) AS value FROM replay_batches "
                "WHERE session_id=? AND status='UNKNOWN'",
                (session_id,),
            ).fetchone()["value"]
            conn.execute(
                "UPDATE replay_state SET sample_count=?, known_delay_count=?, "
                "unknown_delay_count=?, unknown_batch_count=?, updated_utc=? "
                "WHERE session_id=?",
                (
                    int(counts["sample_count"] or 0),
                    int(counts["known_count"] or 0),
                    int(counts["unknown_count"] or 0),
                    int(unknown_total or 0),
                    _utc_now_text(),
                    session_id,
                ),
            )
            return {
                "recovered_last_commit_seq": int(state["last_commit_seq"]),
                "unknown_batch_ids": [int(row["batch_id"]) for row in unknown_rows],
                "newly_marked_unknown_batch_ids": sorted(unknown_ids),
                "unknown_batch_count": int(unknown_total or 0),
                "rebuilt_sample_count": int(counts["sample_count"] or 0),
                "known_delay_count": int(counts["known_count"] or 0),
                "unknown_delay_count": int(counts["unknown_count"] or 0),
                "state_existed": True,
            }

    def get_batches(self, session_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM replay_batches WHERE session_id=? ORDER BY batch_id",
            (session_id,),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["commit_seqs"] = json.loads(item.pop("commit_seqs_json"))
            result.append(item)
        return result

    def get_samples(self, session_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT sample_index, commit_seq, batch_id, commit_version, commit_ts_ms, "
            "completion_ts_ms, delay_ms, status FROM delay_samples "
            "WHERE session_id=? ORDER BY sample_index",
            (session_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def progress_snapshot(self, session_id: str) -> dict[str, Any] | None:
        state = self.get_state(session_id)
        if state is None:
            return None
        batches = self.get_batches(session_id)
        return {
            "session_id": session_id,
            "state": state,
            "batches": batches,
            "completed_batch_ids": [
                batch["batch_id"] for batch in batches if batch["status"] == "COMPLETE"
            ],
            "unknown_batch_ids": [
                batch["batch_id"] for batch in batches if batch["status"] == "UNKNOWN"
            ],
            "in_progress_batch_ids": [
                batch["batch_id"]
                for batch in batches
                if batch["status"] == "IN_PROGRESS"
            ],
        }

    def replace_session_snapshot(
        self,
        session_id: str,
        *,
        progress: dict[str, Any] | None,
        samples: Sequence[dict[str, Any]],
    ) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM delay_samples WHERE session_id=?", (session_id,))
            conn.execute("DELETE FROM replay_batches WHERE session_id=?", (session_id,))
            conn.execute("DELETE FROM replay_state WHERE session_id=?", (session_id,))
            if progress is not None:
                state = progress["state"]
                conn.execute(
                    "INSERT INTO replay_state("
                    "session_id, params_version, estimator_version, replay_rate, concurrency, "
                    "clock_origin_ms, log_digest, segment_versions_json, last_commit_seq, "
                    "sample_count, known_delay_count, unknown_delay_count, unknown_batch_count, "
                    "updated_utc"
                    ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        session_id,
                        state["params_version"],
                        state["estimator_version"],
                        int(state["replay_rate"]),
                        int(state["concurrency"]),
                        int(state["clock_origin_ms"]),
                        state["log_digest"],
                        json.dumps(state["segment_versions"], separators=(",", ":")),
                        int(state["last_commit_seq"]),
                        int(state["sample_count"]),
                        int(state["known_delay_count"]),
                        int(state["unknown_delay_count"]),
                        int(state["unknown_batch_count"]),
                        state.get("updated_utc") or _utc_now_text(),
                    ),
                )
                for batch in progress.get("batches", []):
                    conn.execute(
                        "INSERT INTO replay_batches("
                        "session_id, batch_id, status, start_commit_seq, end_commit_seq, "
                        "start_ms, end_ms, commit_seqs_json"
                        ") VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            session_id,
                            int(batch["batch_id"]),
                            batch["status"],
                            int(batch["start_commit_seq"]),
                            int(batch["end_commit_seq"]),
                            int(batch["start_ms"]),
                            int(batch["end_ms"]),
                            json.dumps(batch.get("commit_seqs", []), separators=(",", ":")),
                        ),
                    )
            for sample in samples:
                conn.execute(
                    "INSERT INTO delay_samples("
                    "session_id, sample_index, commit_seq, batch_id, commit_version, "
                    "commit_ts_ms, completion_ts_ms, delay_ms, status"
                    ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        session_id,
                        int(sample["sample_index"]),
                        int(sample["commit_seq"]),
                        int(sample["batch_id"]),
                        int(sample["commit_version"]),
                        sample.get("commit_ts_ms"),
                        int(sample["completion_ts_ms"]),
                        sample.get("delay_ms"),
                        sample["status"],
                    ),
                )

    def clear_session(self, session_id: str) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM delay_samples WHERE session_id=?", (session_id,))
            conn.execute("DELETE FROM replay_batches WHERE session_id=?", (session_id,))
            conn.execute("DELETE FROM replay_state WHERE session_id=?", (session_id,))

    def register_snapshot(
        self, snapshot_id: str, path: Path, manifest: dict[str, Any]
    ) -> None:
        manifest_json = json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO snapshots(snapshot_id, path, manifest_json) VALUES(?, ?, ?) "
                "ON CONFLICT(snapshot_id) DO UPDATE SET path=excluded.path, "
                "manifest_json=excluded.manifest_json",
                (snapshot_id, str(path), manifest_json),
            )

    def list_snapshots(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT snapshot_id, path, manifest_json FROM snapshots ORDER BY snapshot_id"
        ).fetchall()
        return [
            {
                "snapshot_id": row["snapshot_id"],
                "path": row["path"],
                "manifest": json.loads(row["manifest_json"]),
            }
            for row in rows
        ]

    def get_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT snapshot_id, path, manifest_json FROM snapshots WHERE snapshot_id=?",
            (snapshot_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "snapshot_id": row["snapshot_id"],
            "path": row["path"],
            "manifest": json.loads(row["manifest_json"]),
        }
