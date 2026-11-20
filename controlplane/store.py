"""SQLite persistence, work journal, recovery, and read models."""

from __future__ import annotations

import copy
import fcntl
import json
import os
import socket
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from .canonical import canonical_json, sha256_hex, stable_digest
from .errors import ControlPlaneError
from .validation import Sample

SCHEMA_VERSION = "1"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS versions (
    version_id TEXT PRIMARY KEY,
    source_hash TEXT NOT NULL UNIQUE,
    snapshot_json TEXT NOT NULL,
    loaded_order INTEGER NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS logical_clocks (
    version_id TEXT PRIMARY KEY REFERENCES versions(version_id),
    watermark INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS rounds (
    round_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES versions(version_id),
    round_no INTEGER NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK(status IN ('PENDING', 'COMMITTED')),
    expected_clusters_json TEXT NOT NULL,
    policy_digest TEXT NOT NULL,
    final_results_json TEXT,
    schedule_json TEXT,
    logical_clock INTEGER
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_active_round_per_version
ON rounds(version_id) WHERE status != 'COMMITTED';

CREATE TABLE IF NOT EXISTS work_items (
    version_id TEXT NOT NULL,
    round_id TEXT NOT NULL REFERENCES rounds(round_id) ON DELETE CASCADE,
    cluster TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('PENDING', 'CLAIMED', 'COMPLETE')),
    input_digest TEXT NOT NULL,
    claim_owner TEXT,
    claim_pid INTEGER,
    claim_host TEXT,
    result_json TEXT,
    PRIMARY KEY(version_id, round_id, cluster)
);

CREATE TABLE IF NOT EXISTS round_clusters (
    round_id TEXT NOT NULL REFERENCES rounds(round_id) ON DELETE CASCADE,
    cluster TEXT NOT NULL,
    dispatch_status TEXT NOT NULL,
    config_version TEXT NOT NULL,
    effective_config_digest TEXT NOT NULL,
    precision_sensitive_count INTEGER NOT NULL,
    config_summary_json TEXT NOT NULL,
    derivations_json TEXT NOT NULL,
    precision_json TEXT NOT NULL,
    summary_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    PRIMARY KEY(round_id, cluster)
);

CREATE TABLE IF NOT EXISTS current_pointer (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    round_id TEXT NOT NULL REFERENCES rounds(round_id),
    version_id TEXT NOT NULL REFERENCES versions(version_id),
    round_no INTEGER NOT NULL,
    logical_clock INTEGER NOT NULL,
    source_hash TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class RoundInfo:
    round_id: str
    version_id: str
    round_no: int
    status: str
    expected_clusters: tuple[str, ...]
    policy_digest: str
    logical_clock: int | None


@dataclass(frozen=True)
class WorkProgress:
    expected: int
    pending: int
    claimed: int
    complete: int
    missing_clusters: tuple[str, ...]
    claimed_clusters: tuple[str, ...]

    @property
    def all_complete(self) -> bool:
        return self.expected == self.complete


class Store:
    def __init__(self, db_path: str | Path, *, create: bool):
        self.db_path = Path(db_path)
        self._lock_file: Any = None
        if create:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        elif not self.db_path.exists():
            raise ControlPlaneError(
                "E_DB_MISSING", f"state database does not exist: {self.db_path}"
            )
        try:
            self.conn = sqlite3.connect(
                str(self.db_path),
                timeout=30.0,
                isolation_level=None,
            )
        except sqlite3.Error as exc:
            raise ControlPlaneError(
                "E_DB_OPEN", f"cannot open SQLite database: {exc}"
            ) from exc
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 30000")
        self.conn.execute("PRAGMA synchronous = FULL")
        try:
            self.conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.Error as exc:
            raise ControlPlaneError(
                "E_DB_PRAGMA", f"cannot enable WAL mode: {exc}"
            ) from exc
        if create:
            self._initialize_schema()
        else:
            self._check_schema()

    @property
    def worker_id(self) -> str:
        return f"{socket.gethostname()}:{os.getpid()}"

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    @contextmanager
    def file_lock(self, *, exclusive: bool = True) -> Iterator[None]:
        lock_path = Path(str(self.db_path) + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_path, "a+", encoding="utf-8")
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        try:
            fcntl.flock(handle.fileno(), operation)
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def _initialize_schema(self) -> None:
        with self.file_lock():
            self.conn.executescript(SCHEMA_SQL)
            self.conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
                (SCHEMA_VERSION,),
            )

    def _check_schema(self) -> None:
        try:
            row = self.conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
        except sqlite3.Error as exc:
            raise ControlPlaneError(
                "E_DB_SCHEMA", f"state database schema is not initialized: {exc}"
            ) from exc
        if row is None or row["value"] != SCHEMA_VERSION:
            raise ControlPlaneError(
                "E_DB_SCHEMA",
                "unsupported or missing state database schema version",
            )

    def _write_version_tx(self, sample: Sample) -> tuple[bool, int]:
        existing = self.conn.execute(
            "SELECT version_id, source_hash, snapshot_json FROM versions "
            "WHERE version_id = ? OR source_hash = ?",
            (sample.version_id, sample.source_hash),
        ).fetchone()
        snapshot_json = canonical_json(sample.snapshot)
        if existing is not None:
            if (
                existing["version_id"] != sample.version_id
                or existing["source_hash"] != sample.source_hash
            ):
                raise ControlPlaneError(
                    "E_VERSION_COLLISION",
                    "sample content hash collides with a different stored version",
                )
            if existing["snapshot_json"] != snapshot_json:
                raise ControlPlaneError(
                    "E_VERSION_SNAPSHOT",
                    "stored sample snapshot does not match the current sample",
                )
            clock = self.conn.execute(
                "SELECT watermark FROM logical_clocks WHERE version_id = ?",
                (sample.version_id,),
            ).fetchone()
            if clock is None:
                self.conn.execute(
                    "INSERT INTO logical_clocks(version_id, watermark) VALUES(?, ?)",
                    (sample.version_id, sample.logical_clock.start),
                )
            return False, int(
                self.conn.execute(
                    "SELECT loaded_order FROM versions WHERE version_id = ?",
                    (sample.version_id,),
                ).fetchone()["loaded_order"]
            )

        loaded_order = int(
            self.conn.execute(
                "SELECT COALESCE(MAX(loaded_order), 0) + 1 AS n FROM versions"
            ).fetchone()["n"]
        )
        self.conn.execute(
            "INSERT INTO versions(version_id, source_hash, snapshot_json, loaded_order) "
            "VALUES(?, ?, ?, ?)",
            (sample.version_id, sample.source_hash, snapshot_json, loaded_order),
        )
        self.conn.execute(
            "INSERT INTO logical_clocks(version_id, watermark) VALUES(?, ?)",
            (sample.version_id, sample.logical_clock.start),
        )
        return True, loaded_order

    def store_version(self, sample: Sample) -> bool:
        with self.file_lock():
            with self.transaction():
                inserted, _ = self._write_version_tx(sample)
        return inserted

    def _round_from_row(self, row: sqlite3.Row) -> RoundInfo:
        return RoundInfo(
            round_id=row["round_id"],
            version_id=row["version_id"],
            round_no=int(row["round_no"]),
            status=row["status"],
            expected_clusters=tuple(json.loads(row["expected_clusters_json"])),
            policy_digest=row["policy_digest"],
            logical_clock=(
                int(row["logical_clock"])
                if row["logical_clock"] is not None
                else None
            ),
        )

    def ensure_round(
        self,
        sample: Sample,
        *,
        new_round: bool = False,
        expected_clusters: Sequence[str] | None = None,
    ) -> tuple[RoundInfo, bool]:
        expected = tuple(
            sorted(expected_clusters or [cluster.name for cluster in sample.clusters])
        )
        expected_json = canonical_json(list(expected))
        policy_digest = stable_digest(
            {
                "policy": sample.snapshot["distribution_policy"],
                "logical_clock": sample.snapshot["logical_clock"],
            },
            24,
        )
        with self.file_lock():
            with self.transaction():
                _, sample_order = self._write_version_tx(sample)
                current = self.conn.execute(
                    "SELECT p.round_id, p.version_id, p.logical_clock, "
                    "v.loaded_order AS version_order "
                    "FROM current_pointer p "
                    "JOIN versions v ON v.version_id = p.version_id "
                    "WHERE p.singleton = 1"
                ).fetchone()
                active = self.conn.execute(
                    "SELECT * FROM rounds WHERE version_id = ? AND status != 'COMMITTED' "
                    "ORDER BY round_no DESC LIMIT 1",
                    (sample.version_id,),
                ).fetchone()
                if active is not None:
                    if (
                        current is not None
                        and current["version_id"] != sample.version_id
                        and int(current["version_order"]) > sample_order
                    ):
                        raise ControlPlaneError(
                            "E_STALE_VERSION",
                            "refusing to resume a pending round from an older "
                            "version after a newer version was committed",
                        )
                    if active["expected_clusters_json"] != expected_json:
                        raise ControlPlaneError(
                            "E_ROUND_SCOPE",
                            "pending round scope differs from the current sample",
                        )
                    self._insert_missing_work_tx(sample, active["round_id"], expected)
                    return self._round_from_row(active), False

                if (
                    current is not None
                    and current["version_id"] != sample.version_id
                    and int(current["version_order"]) > sample_order
                ):
                    raise ControlPlaneError(
                        "E_STALE_VERSION",
                        "refusing to distribute an older version after a newer "
                        "version was committed",
                    )

                completed_same = self.conn.execute(
                    "SELECT * FROM rounds WHERE version_id = ? AND status = 'COMMITTED' "
                    "ORDER BY round_no DESC LIMIT 1",
                    (sample.version_id,),
                ).fetchone()
                if (
                    not new_round
                    and completed_same is not None
                    and current is not None
                    and current["version_id"] == sample.version_id
                ):
                    return self._round_from_row(completed_same), False
                if (
                    not new_round
                    and completed_same is not None
                    and current is not None
                    and current["version_id"] != sample.version_id
                ):
                    raise ControlPlaneError(
                        "E_STALE_VERSION",
                        "refusing to replace a newer committed round with an older version",
                    )

                round_no = int(
                    self.conn.execute(
                        "SELECT COALESCE(MAX(round_no), 0) + 1 AS n FROM rounds"
                    ).fetchone()["n"]
                )
                round_id = f"r{round_no:04d}"
                self.conn.execute(
                    "INSERT INTO rounds("
                    "round_id, version_id, round_no, status, expected_clusters_json, "
                    "policy_digest, final_results_json, schedule_json, logical_clock"
                    ") VALUES(?, ?, ?, 'PENDING', ?, ?, NULL, NULL, NULL)",
                    (round_id, sample.version_id, round_no, expected_json, policy_digest),
                )
                self._insert_missing_work_tx(sample, round_id, expected)
                row = self.conn.execute(
                    "SELECT * FROM rounds WHERE round_id = ?", (round_id,)
                ).fetchone()
                assert row is not None
                return self._round_from_row(row), True

    def _insert_missing_work_tx(
        self, sample: Sample, round_id: str, expected: Sequence[str]
    ) -> None:
        for cluster in expected:
            digest = sha256_hex(
                {
                    "version_id": sample.version_id,
                    "cluster": cluster,
                    "policy": sample.snapshot["distribution_policy"],
                }
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO work_items("
                "version_id, round_id, cluster, status, input_digest"
                ") VALUES(?, ?, ?, 'PENDING', ?)",
                (sample.version_id, round_id, cluster, digest),
            )

    def get_round(self, round_id: str) -> RoundInfo | None:
        row = self.conn.execute(
            "SELECT * FROM rounds WHERE round_id = ?", (round_id,)
        ).fetchone()
        return self._round_from_row(row) if row is not None else None

    def _claim_is_stale(self, row: Mapping[str, Any], owner: str) -> bool:
        if row["claim_owner"] == owner:
            return True
        if row["claim_host"] != socket.gethostname():
            return True
        try:
            pid = int(row["claim_pid"])
        except (TypeError, ValueError):
            return True
        if pid <= 0:
            return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        return False

    def claim_next_work(
        self,
        round_id: str,
        *,
        only_clusters: Iterable[str] | None = None,
    ) -> str | None:
        only = set(only_clusters) if only_clusters is not None else None
        with self.transaction():
            rows = self.conn.execute(
                "SELECT * FROM work_items WHERE round_id = ? "
                "AND status != 'COMPLETE' ORDER BY cluster",
                (round_id,),
            ).fetchall()
            for row in rows:
                cluster = row["cluster"]
                if only is not None and cluster not in only:
                    continue
                if row["status"] == "CLAIMED" and not self._claim_is_stale(
                    row, self.worker_id
                ):
                    continue
                self.conn.execute(
                    "UPDATE work_items SET status='CLAIMED', claim_owner=?, "
                    "claim_pid=?, claim_host=? "
                    "WHERE version_id=? AND round_id=? AND cluster=?",
                    (
                        self.worker_id,
                        os.getpid(),
                        socket.gethostname(),
                        row["version_id"],
                        round_id,
                        cluster,
                    ),
                )
                return cluster
        return None

    def complete_work(
        self,
        round_id: str,
        cluster: str,
        result: Mapping[str, Any],
    ) -> None:
        result_json = canonical_json(result)
        with self.transaction():
            cursor = self.conn.execute(
                "UPDATE work_items SET status='COMPLETE', result_json=? "
                "WHERE round_id=? AND cluster=? AND status='CLAIMED' "
                "AND claim_owner=?",
                (result_json, round_id, cluster, self.worker_id),
            )
            if cursor.rowcount != 1:
                raise ControlPlaneError(
                    "E_WORK_CLAIM_LOST",
                    f"lost work claim for cluster {cluster} in round {round_id}",
                )

    def work_progress(self, round_id: str) -> WorkProgress:
        rows = self.conn.execute(
            "SELECT cluster, status FROM work_items WHERE round_id = ? "
            "ORDER BY cluster",
            (round_id,),
        ).fetchall()
        pending = sum(1 for row in rows if row["status"] == "PENDING")
        claimed = sum(1 for row in rows if row["status"] == "CLAIMED")
        complete = sum(1 for row in rows if row["status"] == "COMPLETE")
        missing = tuple(
            row["cluster"] for row in rows if row["status"] != "COMPLETE"
        )
        claimed_clusters = tuple(
            row["cluster"] for row in rows if row["status"] == "CLAIMED"
        )
        return WorkProgress(
            expected=len(rows),
            pending=pending,
            claimed=claimed,
            complete=complete,
            missing_clusters=missing,
            claimed_clusters=claimed_clusters,
        )

    def load_complete_base_results(self, round_id: str) -> dict[str, dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT cluster, status, result_json FROM work_items "
            "WHERE round_id = ? ORDER BY cluster",
            (round_id,),
        ).fetchall()
        if not rows or any(row["status"] != "COMPLETE" for row in rows):
            raise ControlPlaneError(
                "E_WORK_INCOMPLETE",
                f"round {round_id} still has incomplete cluster work",
            )
        return {
            row["cluster"]: json.loads(row["result_json"]) for row in rows
        }

    def adjudicate_and_commit(
        self,
        sample: Sample,
        round_info: RoundInfo,
        final_factory: Callable[
            [Mapping[str, Mapping[str, Any]]],
            tuple[dict[str, dict[str, Any]], dict[str, Any]],
        ],
    ) -> RoundInfo:
        with self.file_lock():
            row = self.conn.execute(
                "SELECT * FROM rounds WHERE round_id = ?",
                (round_info.round_id,),
            ).fetchone()
            if row is None:
                raise ControlPlaneError(
                    "E_ROUND_MISSING", f"round not found: {round_info.round_id}"
                )
            if row["status"] == "COMMITTED":
                return self._round_from_row(row)

            final_results: dict[str, dict[str, Any]]
            schedule: dict[str, Any]
            if row["final_results_json"] and row["schedule_json"]:
                final_results = json.loads(row["final_results_json"])
                schedule = json.loads(row["schedule_json"])
            else:
                base_results = self.load_complete_base_results(round_info.round_id)
                final_results, schedule = final_factory(base_results)
                with self.transaction():
                    self.conn.execute(
                        "UPDATE rounds SET final_results_json=?, schedule_json=? "
                        "WHERE round_id=? AND status='PENDING'",
                        (
                            canonical_json(final_results),
                            canonical_json(schedule),
                            round_info.round_id,
                        ),
                    )
            return self._commit_round(sample, round_info.round_id, final_results, schedule)

    def _commit_round(
        self,
        sample: Sample,
        round_id: str,
        final_results: Mapping[str, Mapping[str, Any]],
        schedule: Mapping[str, Any],
    ) -> RoundInfo:
        with self.transaction():
            row = self.conn.execute(
                "SELECT * FROM rounds WHERE round_id = ?", (round_id,)
            ).fetchone()
            if row is None:
                raise ControlPlaneError("E_ROUND_MISSING", f"round not found: {round_id}")
            if row["status"] == "COMMITTED":
                return self._round_from_row(row)
            info = self._round_from_row(row)
            round_version_order = int(
                self.conn.execute(
                    "SELECT loaded_order FROM versions WHERE version_id=?",
                    (info.version_id,),
                ).fetchone()["loaded_order"]
            )
            newer_pointer = self.conn.execute(
                "SELECT v.loaded_order FROM current_pointer p "
                "JOIN versions v ON v.version_id = p.version_id "
                "WHERE p.singleton=1 AND v.loaded_order > ?",
                (round_version_order,),
            ).fetchone()
            if newer_pointer is not None:
                raise ControlPlaneError(
                    "E_STALE_VERSION",
                    "refusing to roll back the current pointer to an older version",
                )
            if set(final_results) != set(info.expected_clusters):
                raise ControlPlaneError(
                    "E_ROUND_SCOPE",
                    "adjudication results do not cover the complete round scope",
                )
            progress = self.work_progress(round_id)
            if not progress.all_complete:
                raise ControlPlaneError(
                    "E_WORK_INCOMPLETE",
                    f"round {round_id} still has incomplete cluster work",
                )
            outcome_map = schedule.get("outcomes", {})
            if set(outcome_map) != set(info.expected_clusters):
                raise ControlPlaneError(
                    "E_SCHEDULE_SCOPE",
                    "dispatch outcomes do not cover the complete round scope",
                )

            version_clock = int(
                self.conn.execute(
                    "SELECT watermark FROM logical_clocks WHERE version_id=?",
                    (sample.version_id,),
                ).fetchone()["watermark"]
            )
            pointer = self.conn.execute(
                "SELECT logical_clock FROM current_pointer WHERE singleton=1"
            ).fetchone()
            base_clock = max(
                version_clock,
                int(pointer["logical_clock"]) if pointer is not None else 0,
            )
            delta = sample.logical_clock.delta_for_round(
                info.round_no, sample.policy.logical_clock_step
            )
            new_clock = base_clock + delta

            for cluster in sorted(final_results):
                result = copy.deepcopy(dict(final_results[cluster]))
                result["round_id"] = round_id
                result["round_number"] = info.round_no
                result["logical_clock_watermark"] = new_clock
                result["dispatch"] = outcome_map[cluster]
                result_without_hash = dict(result)
                summary_hash = sha256_hex(result_without_hash)
                result["summary_hash"] = summary_hash
                config_summary = {
                    "merge_order": result["merge_order"],
                    "effective_config": result["effective_config"],
                    "dispatch_eligible": result["dispatch_eligible"],
                }
                precision_payload = {
                    "precision_sensitive": result["precision_sensitive"],
                    "records": result["precision_sensitive_records"],
                    "error_budget_exceeded": result["error_budget_exceeded"],
                }
                self.conn.execute(
                    "INSERT INTO round_clusters("
                    "round_id, cluster, dispatch_status, config_version, "
                    "effective_config_digest, precision_sensitive_count, "
                    "config_summary_json, derivations_json, precision_json, "
                    "summary_hash, result_json"
                    ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        round_id,
                        cluster,
                        result["dispatch"]["status"],
                        sample.version_id,
                        result["effective_config_digest"],
                        int(result["precision_sensitive_count"]),
                        canonical_json(config_summary),
                        canonical_json(result["derived"]),
                        canonical_json(precision_payload),
                        summary_hash,
                        canonical_json(result),
                    ),
                )
            self.conn.execute(
                "UPDATE rounds SET status='COMMITTED', logical_clock=? "
                "WHERE round_id=?",
                (new_clock, round_id),
            )
            self.conn.execute(
                "UPDATE logical_clocks SET watermark=? WHERE version_id=?",
                (new_clock, sample.version_id),
            )
            self.conn.execute(
                "INSERT INTO current_pointer("
                "singleton, round_id, version_id, round_no, logical_clock, source_hash"
                ") VALUES(1, ?, ?, ?, ?, ?) "
                "ON CONFLICT(singleton) DO UPDATE SET "
                "round_id=excluded.round_id, version_id=excluded.version_id, "
                "round_no=excluded.round_no, logical_clock=excluded.logical_clock, "
                "source_hash=excluded.source_hash",
                (
                    round_id,
                    sample.version_id,
                    info.round_no,
                    new_clock,
                    sample.source_hash,
                ),
            )
            committed = self.conn.execute(
                "SELECT * FROM rounds WHERE round_id=?", (round_id,)
            ).fetchone()
            assert committed is not None
            return self._round_from_row(committed)

    def current_round(self) -> RoundInfo | None:
        row = self.conn.execute(
            "SELECT r.* FROM current_pointer p "
            "JOIN rounds r ON r.round_id = p.round_id WHERE p.singleton=1"
        ).fetchone()
        return self._round_from_row(row) if row is not None else None

    def current_clock(self) -> int | None:
        row = self.conn.execute(
            "SELECT logical_clock FROM current_pointer WHERE singleton=1"
        ).fetchone()
        return int(row["logical_clock"]) if row is not None else None

    def pending_rounds(self) -> list[RoundInfo]:
        rows = self.conn.execute(
            "SELECT * FROM rounds WHERE status != 'COMMITTED' "
            "ORDER BY round_no, round_id"
        ).fetchall()
        return [self._round_from_row(row) for row in rows]

    def pending_round_for_version(self, version_id: str) -> RoundInfo | None:
        row = self.conn.execute(
            "SELECT * FROM rounds WHERE version_id = ? AND status != 'COMMITTED' "
            "ORDER BY round_no DESC LIMIT 1",
            (version_id,),
        ).fetchone()
        return self._round_from_row(row) if row is not None else None

    def round_results(
        self, round_id: str, clusters: Sequence[str] | None = None
    ) -> list[dict[str, Any]]:
        if clusters:
            placeholders = ",".join("?" for _ in clusters)
            rows = self.conn.execute(
                f"SELECT * FROM round_clusters WHERE round_id=? "
                f"AND cluster IN ({placeholders}) ORDER BY cluster",
                (round_id, *sorted(clusters)),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM round_clusters WHERE round_id=? ORDER BY cluster",
                (round_id,),
            ).fetchall()
        return [json.loads(row["result_json"]) for row in rows]

    def status_report(self) -> dict[str, Any]:
        pending: list[dict[str, Any]] = []
        for round_info in self.pending_rounds():
            progress = self.work_progress(round_info.round_id)
            pending.append(
                {
                    "round_id": round_info.round_id,
                    "round_number": round_info.round_no,
                    "version_id": round_info.version_id,
                    "status": round_info.status,
                    "completed_work": progress.complete,
                    "expected_work": progress.expected,
                    "claimed_work": progress.claimed,
                    "pending_work": progress.pending,
                    "missing_clusters": list(progress.missing_clusters),
                }
            )
        current = self.current_round()
        if current is None:
            return {
                "ok": True,
                "command": "status",
                "logical_clock": None,
                "round_id": None,
                "round_number": None,
                "version_id": None,
                "clusters": [],
                "precision_sensitive_total": 0,
                "pending_rounds": pending,
            }
        rows = self.conn.execute(
            "SELECT cluster, dispatch_status, config_version, "
            "effective_config_digest, precision_sensitive_count "
            "FROM round_clusters WHERE round_id=? ORDER BY cluster",
            (current.round_id,),
        ).fetchall()
        clusters = [
            {
                "cluster": row["cluster"],
                "config_version": row["config_version"],
                "effective_config_digest": row["effective_config_digest"],
                "dispatch_status": row["dispatch_status"],
                "precision_sensitive": int(row["precision_sensitive_count"]) > 0,
                "precision_sensitive_count": int(
                    row["precision_sensitive_count"]
                ),
            }
            for row in rows
        ]
        return {
            "ok": True,
            "command": "status",
            "logical_clock": current.logical_clock,
            "round_id": current.round_id,
            "round_number": current.round_no,
            "version_id": current.version_id,
            "clusters": clusters,
            "precision_sensitive_total": sum(
                cluster["precision_sensitive_count"] for cluster in clusters
            ),
            "pending_rounds": pending,
        }

    def explain_report(
        self, clusters: Sequence[str] | None = None
    ) -> dict[str, Any]:
        current = self.current_round()
        if current is None:
            raise ControlPlaneError(
                "E_NO_CURRENT_ROUND", "no committed distribution round exists"
            )
        requested = sorted(set(clusters)) if clusters else None
        results = self.round_results(current.round_id, requested)
        if requested is not None and len(results) != len(requested):
            present = {result["cluster"] for result in results}
            missing = sorted(set(requested) - present)
            raise ControlPlaneError(
                "E_UNKNOWN_CLUSTER",
                f"cluster(s) not present in current round: {', '.join(missing)}",
            )
        sensitivity: dict[str, dict[str, Any]] = {}
        for result in results:
            for record in result["precision_sensitive_records"]:
                sensitivity[record["id"]] = record
        return {
            "ok": True,
            "command": "explain",
            "round_id": current.round_id,
            "round_number": current.round_no,
            "version_id": current.version_id,
            "logical_clock": current.logical_clock,
            "clusters": results,
            "precision_sensitive_records": [
                sensitivity[key] for key in sorted(sensitivity)
            ],
        }

    def verify_report(self, sample: Sample | None = None) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []

        def add(name: str, ok: bool, detail: str | None = None) -> None:
            check: dict[str, Any] = {"name": name, "ok": ok}
            if detail:
                check["detail"] = detail
            checks.append(check)

        add("schema_version", True)
        pointer = self.conn.execute(
            "SELECT * FROM current_pointer WHERE singleton=1"
        ).fetchone()
        add("current_pointer_present", pointer is not None)
        if pointer is None:
            return {
                "ok": not any(not check["ok"] for check in checks),
                "command": "verify",
                "checks": checks,
            }

        round_row = self.conn.execute(
            "SELECT * FROM rounds WHERE round_id=?", (pointer["round_id"],)
        ).fetchone()
        add("pointer_round_exists", round_row is not None)
        if round_row is None:
            return {
                "ok": False,
                "command": "verify",
                "checks": checks,
            }
        round_info = self._round_from_row(round_row)
        add("pointer_round_committed", round_info.status == "COMMITTED")
        add(
            "pointer_version_matches",
            round_info.version_id == pointer["version_id"],
        )
        add(
            "pointer_round_no_matches",
            round_info.round_no == int(pointer["round_no"]),
        )
        add(
            "pointer_clock_matches_round",
            round_info.logical_clock == int(pointer["logical_clock"]),
        )

        version_row = self.conn.execute(
            "SELECT * FROM versions WHERE version_id=?",
            (round_info.version_id,),
        ).fetchone()
        add("version_exists", version_row is not None)
        if version_row is not None:
            stored_snapshot = json.loads(
                version_row["snapshot_json"], parse_float=Decimal
            )
            add(
                "version_snapshot_hash",
                sha256_hex(stored_snapshot) == version_row["source_hash"],
            )
            add(
                "pointer_source_hash_matches",
                version_row["source_hash"] == pointer["source_hash"],
            )
        if sample is not None:
            add(
                "sample_version_matches",
                sample.version_id == round_info.version_id,
                (
                    f"database={round_info.version_id} "
                    f"sample={sample.version_id}"
                ),
            )

        rows = self.conn.execute(
            "SELECT * FROM round_clusters WHERE round_id=? ORDER BY cluster",
            (round_info.round_id,),
        ).fetchall()
        result_clusters = [row["cluster"] for row in rows]
        add(
            "round_scope_complete",
            tuple(result_clusters) == round_info.expected_clusters,
            (
                f"results={len(result_clusters)} "
                f"expected={len(round_info.expected_clusters)}"
            ),
        )
        progress = self.work_progress(round_info.round_id)
        add("work_all_complete", progress.all_complete)
        digest_ok = True
        version_ok = True
        clock_ok = True
        for row in rows:
            result = json.loads(row["result_json"])
            stored_hash = result.get("summary_hash")
            result_without_hash = dict(result)
            result_without_hash.pop("summary_hash", None)
            if (
                sha256_hex(result_without_hash) != stored_hash
                or row["summary_hash"] != stored_hash
            ):
                digest_ok = False
            if result.get("config_version") != pointer["version_id"]:
                version_ok = False
            if result.get("logical_clock_watermark") != int(pointer["logical_clock"]):
                clock_ok = False
        add("all_result_hashes_valid", digest_ok)
        add("no_mixed_config_versions", version_ok)
        add("all_results_share_clock", clock_ok)
        pointer_clock = self.conn.execute(
            "SELECT watermark FROM logical_clocks WHERE version_id=?",
            (round_info.version_id,),
        ).fetchone()
        add(
            "version_clock_not_behind_pointer",
            pointer_clock is not None
            and int(pointer_clock["watermark"]) >= int(pointer["logical_clock"]),
        )
        ok = all(check["ok"] for check in checks)
        return {
            "ok": ok,
            "command": "verify",
            "round_id": round_info.round_id,
            "version_id": round_info.version_id,
            "logical_clock": round_info.logical_clock,
            "checks": checks,
        }
