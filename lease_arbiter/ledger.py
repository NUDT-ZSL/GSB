from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator

from .errors import BizError, Code
from .models import Bundle


SCHEMA_VERSION = 1


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def seal_report(report: dict[str, Any]) -> dict[str, Any]:
    body = {key: value for key, value in report.items() if key != "report_digest"}
    digest = sha256_text(canonical_json(body))
    sealed = dict(body)
    sealed["report_digest"] = digest
    return sealed


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


@contextlib.contextmanager
def exclusive_lock(path: Path, timeout_seconds: float = 15.0) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    deadline = time.monotonic() + timeout_seconds
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise BizError(
                        Code.LOCK_TIMEOUT,
                        "timed out waiting for the ledger file lock",
                        details={"lock_path": str(path)},
                    )
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


class Ledger:
    def __init__(
        self,
        db_path: str | Path,
        pointer_path: str | Path,
        reports_dir: str | Path,
        *,
        lock_timeout_seconds: float = 15.0,
    ):
        self.db_path = Path(db_path)
        self.pointer_path = Path(pointer_path)
        self.reports_dir = Path(reports_dir)
        self.lock_path = Path(f"{self.db_path}.lock")
        self.lock_timeout_seconds = lock_timeout_seconds

    def lock(self) -> contextlib.AbstractContextManager[None]:
        return exclusive_lock(self.lock_path, self.lock_timeout_seconds)

    def connect(self, *, create: bool = False) -> sqlite3.Connection:
        if not create and not self.db_path.exists():
            raise BizError(
                Code.DB_NOT_LOADED,
                "ledger database does not exist; run load first",
                details={"db_path": str(self.db_path)},
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.db_path,
            timeout=self.lock_timeout_seconds,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @staticmethod
    def create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE cards (
                card_id TEXT PRIMARY KEY,
                total_compute_shares INTEGER NOT NULL CHECK(total_compute_shares > 0),
                total_memory_shares INTEGER NOT NULL CHECK(total_memory_shares > 0),
                granularity INTEGER NOT NULL CHECK(granularity > 0),
                health TEXT NOT NULL,
                topology_domain TEXT NOT NULL
            );
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                compute_shares INTEGER NOT NULL CHECK(compute_shares > 0),
                memory_shares INTEGER NOT NULL CHECK(memory_shares > 0),
                lease_duration_ms INTEGER NOT NULL CHECK(lease_duration_ms > 0),
                priority TEXT NOT NULL,
                priority_rank INTEGER NOT NULL,
                quota_group TEXT NOT NULL,
                preemptible INTEGER NOT NULL,
                allow_cross_card INTEGER NOT NULL,
                arrival_round INTEGER NOT NULL,
                state TEXT NOT NULL,
                last_round INTEGER,
                failure_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE quota_groups (
                quota_group TEXT PRIMARY KEY,
                limit_compute INTEGER NOT NULL CHECK(limit_compute > 0),
                consumed_compute INTEGER NOT NULL DEFAULT 0 CHECK(consumed_compute >= 0)
            );
            CREATE TABLE leases (
                lease_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL REFERENCES jobs(job_id),
                segment_index INTEGER NOT NULL,
                card_id TEXT NOT NULL REFERENCES cards(card_id),
                compute_start INTEGER NOT NULL,
                compute_length INTEGER NOT NULL,
                memory_start INTEGER NOT NULL,
                memory_length INTEGER NOT NULL,
                alignment INTEGER NOT NULL,
                generation INTEGER NOT NULL CHECK(generation >= 1),
                status TEXT NOT NULL,
                granted_round INTEGER NOT NULL,
                granted_clock_ms INTEGER NOT NULL,
                expires_at_ms INTEGER NOT NULL,
                last_renewed_round INTEGER,
                priority TEXT NOT NULL,
                priority_rank INTEGER NOT NULL,
                quota_group TEXT NOT NULL,
                preemptible INTEGER NOT NULL,
                UNIQUE(job_id, segment_index, granted_round)
            );
            CREATE TABLE occupancy_ranges (
                card_id TEXT NOT NULL REFERENCES cards(card_id),
                dimension TEXT NOT NULL CHECK(dimension IN ('compute','memory')),
                start_share INTEGER NOT NULL,
                length_share INTEGER NOT NULL,
                lease_id TEXT NOT NULL REFERENCES leases(lease_id),
                PRIMARY KEY(card_id, dimension, start_share)
            );
            CREATE TABLE occupancy_slots (
                card_id TEXT NOT NULL REFERENCES cards(card_id),
                dimension TEXT NOT NULL CHECK(dimension IN ('compute','memory')),
                slot_index INTEGER NOT NULL,
                lease_id TEXT NOT NULL REFERENCES leases(lease_id),
                PRIMARY KEY(card_id, dimension, slot_index)
            );
            CREATE TABLE refunds (
                refund_id TEXT PRIMARY KEY,
                lease_id TEXT NOT NULL UNIQUE REFERENCES leases(lease_id),
                job_id TEXT NOT NULL,
                card_id TEXT NOT NULL,
                round_no INTEGER NOT NULL,
                clock_ms INTEGER NOT NULL,
                allocated_compute INTEGER NOT NULL,
                allocated_memory INTEGER NOT NULL,
                consumed_compute INTEGER NOT NULL,
                consumed_memory INTEGER NOT NULL,
                refund_compute INTEGER NOT NULL,
                refund_memory INTEGER NOT NULL,
                status TEXT NOT NULL,
                reason TEXT NOT NULL
            );
            CREATE TABLE fragments (
                fragment_id TEXT PRIMARY KEY,
                round_no INTEGER NOT NULL,
                card_id TEXT NOT NULL,
                dimension TEXT NOT NULL,
                start_share INTEGER NOT NULL,
                length_share INTEGER NOT NULL,
                source_lease_id TEXT NOT NULL,
                digest TEXT NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE rounds (
                commit_key TEXT PRIMARY KEY,
                round_no INTEGER NOT NULL,
                operation TEXT NOT NULL,
                clock_ms INTEGER NOT NULL,
                ledger_version INTEGER NOT NULL UNIQUE,
                report_json TEXT NOT NULL,
                report_digest TEXT NOT NULL,
                state_digest TEXT NOT NULL
            );
            CREATE TABLE events (
                commit_key TEXT NOT NULL,
                seq INTEGER NOT NULL,
                event_id TEXT NOT NULL,
                round_no INTEGER NOT NULL,
                kind TEXT NOT NULL,
                job_id TEXT,
                lease_id TEXT,
                card_id TEXT,
                code TEXT,
                payload_json TEXT NOT NULL,
                PRIMARY KEY(commit_key, seq)
            );
            CREATE TABLE injections (
                anomaly_id TEXT PRIMARY KEY,
                stage TEXT NOT NULL,
                type TEXT NOT NULL,
                round_no INTEGER NOT NULL,
                card_id TEXT NOT NULL,
                job_id TEXT,
                enabled INTEGER NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE recovery_log (
                sequence INTEGER PRIMARY KEY,
                action TEXT NOT NULL,
                details_json TEXT NOT NULL
            );
            CREATE TABLE anomaly_fires (
                anomaly_id TEXT NOT NULL,
                commit_key TEXT NOT NULL,
                round_no INTEGER NOT NULL,
                card_id TEXT NOT NULL,
                code TEXT NOT NULL,
                details_json TEXT NOT NULL,
                PRIMARY KEY(anomaly_id, commit_key)
            );
            CREATE INDEX idx_leases_status ON leases(status, expires_at_ms);
            CREATE INDEX idx_leases_job ON leases(job_id, status);
            """
        )

    def load_bundle(self, bundle: Bundle, *, force: bool = False) -> dict[str, Any]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if self.db_path.exists() and not force:
            raise BizError(
                Code.PROTOCOL,
                "ledger database already exists; use --force to replace it",
                details={"db_path": str(self.db_path)},
            )
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        temp_path = self.db_path.with_name(f".{self.db_path.name}.tmp")
        for suffix in ("", "-wal", "-shm"):
            Path(f"{temp_path}{suffix}").unlink(missing_ok=True)
        connection = sqlite3.connect(temp_path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("BEGIN IMMEDIATE")
            self.create_schema(connection)
            meta = {
                "schema_version": str(SCHEMA_VERSION),
                "ledger_version": "0",
                "clock_watermark_ms": "0",
                "last_commit_key": "",
                "config_json": canonical_json(
                    {
                        "quota_group_compute_limits": bundle.config.quota_group_compute_limits,
                        "alignment_granularity": bundle.config.alignment_granularity,
                        "renewal_grace_ms": bundle.config.renewal_grace_ms,
                        "fragmentation_threshold": bundle.config.fragmentation_threshold,
                        "clock_steps_ms": list(bundle.config.clock_steps_ms),
                        "clock_rollback_tolerance_ms": bundle.config.clock_rollback_tolerance_ms,
                    }
                ),
                "source_hashes": canonical_json(bundle.source_hashes),
            }
            connection.executemany(
                "INSERT INTO meta(key, value) VALUES(?, ?)", sorted(meta.items())
            )
            connection.executemany(
                """
                INSERT INTO cards(
                    card_id, total_compute_shares, total_memory_shares, granularity,
                    health, topology_domain
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        card.card_id,
                        card.total_compute_shares,
                        card.total_memory_shares,
                        card.granularity,
                        card.health,
                        card.topology_domain,
                    )
                    for card in bundle.cards
                ],
            )
            connection.executemany(
                "INSERT INTO quota_groups(quota_group, limit_compute, consumed_compute) VALUES(?, ?, 0)",
                sorted(bundle.config.quota_group_compute_limits.items()),
            )
            from .models import priority_rank

            connection.executemany(
                """
                INSERT INTO jobs(
                    job_id, compute_shares, memory_shares, lease_duration_ms, priority,
                    priority_rank, quota_group, preemptible, allow_cross_card,
                    arrival_round, state, last_round, failure_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, '{}')
                """,
                [
                    (
                        job.job_id,
                        job.compute_shares,
                        job.memory_shares,
                        job.lease_duration_ms,
                        job.priority,
                        priority_rank(job.priority),
                        job.quota_group,
                        int(job.preemptible),
                        int(job.allow_cross_card),
                        job.arrival_round,
                    )
                    for job in bundle.jobs
                ],
            )
            connection.executemany(
                """
                INSERT INTO injections(
                    anomaly_id, stage, type, round_no, card_id, job_id, enabled, payload_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        item.anomaly_id,
                        item.stage,
                        item.type,
                        item.round_no,
                        item.card_id,
                        item.job_id,
                        int(item.enabled),
                        canonical_json(item.payload),
                    )
                    for item in bundle.anomalies
                ],
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        for suffix in ("-wal", "-shm"):
            Path(f"{self.db_path}{suffix}").unlink(missing_ok=True)
        os.replace(temp_path, self.db_path)
        directory_fd = os.open(self.db_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        pointer = self._pointer_payload(
            version=0,
            commit_key="",
            round_no=0,
            report_digest="",
            state_digest=sha256_text(canonical_json({"version": 0, "state": "loaded"})),
        )
        atomic_write_json(self.pointer_path, pointer)
        return {
            "version": 0,
            "cards": len(bundle.cards),
            "jobs": len(bundle.jobs),
            "anomalies": len(bundle.anomalies),
            "pointer": str(self.pointer_path),
        }

    def _pointer_payload(
        self,
        *,
        version: int,
        commit_key: str,
        round_no: int,
        report_digest: str,
        state_digest: str,
    ) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "version": version,
            "commit_key": commit_key,
            "round": round_no,
            "report_digest": report_digest,
            "state_digest": state_digest,
            "db_path": str(self.db_path.resolve()),
            "updated_by": "lease_arbiter",
        }

    def meta(self, connection: sqlite3.Connection, key: str) -> str:
        row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            raise BizError(
                Code.DB_NOT_LOADED,
                "ledger metadata is incomplete",
                details={"missing_key": key},
            )
        return str(row["value"])

    def set_meta(self, connection: sqlite3.Connection, key: str, value: str) -> None:
        connection.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def state_digest(self, connection: sqlite3.Connection) -> str:
        leases = [
            dict(row)
            for row in connection.execute(
                """
                SELECT lease_id, job_id, segment_index, card_id, compute_start,
                       compute_length, memory_start, memory_length, generation, status
                FROM leases ORDER BY lease_id
                """
            )
        ]
        quotas = [
            dict(row)
            for row in connection.execute(
                "SELECT quota_group, limit_compute, consumed_compute "
                "FROM quota_groups ORDER BY quota_group"
            )
        ]
        refunds = [
            dict(row)
            for row in connection.execute(
                "SELECT refund_id, lease_id, refund_compute, refund_memory, status "
                "FROM refunds ORDER BY refund_id"
            )
        ]
        jobs = [
            dict(row)
            for row in connection.execute(
                "SELECT job_id, state, last_round FROM jobs ORDER BY job_id"
            )
        ]
        watermark = self.meta(connection, "clock_watermark_ms")
        return sha256_text(
            canonical_json(
                {
                    "leases": leases,
                    "quotas": quotas,
                    "refunds": refunds,
                    "jobs": jobs,
                    "clock_watermark_ms": int(watermark),
                }
            )
        )

    def report_path(self, commit_key: str) -> Path:
        safe = commit_key.replace(":", "-").replace("/", "-")
        return self.reports_dir / f"commit-{safe}.json"

    def write_report(self, commit_key: str, report: dict[str, Any]) -> Path:
        path = self.report_path(commit_key)
        atomic_write_json(path, report)
        return path

    def committed_round(
        self, connection: sqlite3.Connection, commit_key: str
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM rounds WHERE commit_key = ?", (commit_key,)
        ).fetchone()

    def insert_round(
        self,
        connection: sqlite3.Connection,
        *,
        commit_key: str,
        round_no: int,
        operation: str,
        clock_ms: int,
        report: dict[str, Any],
        state_digest: str,
    ) -> int:
        current = int(self.meta(connection, "ledger_version"))
        version = current + 1
        report["ledger_version"] = version
        sealed = seal_report(report)
        report.update(sealed)
        report_text = canonical_json(sealed)
        connection.execute(
            """
            INSERT INTO rounds(
                commit_key, round_no, operation, clock_ms, ledger_version,
                report_json, report_digest, state_digest
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                commit_key,
                round_no,
                operation,
                clock_ms,
                version,
                report_text,
                sealed["report_digest"],
                state_digest,
            ),
        )
        self.set_meta(connection, "ledger_version", str(version))
        self.set_meta(connection, "last_commit_key", commit_key)
        return version

    def insert_events(
        self,
        connection: sqlite3.Connection,
        *,
        commit_key: str,
        round_no: int,
        events: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        sealed_events: list[dict[str, Any]] = []
        for seq, event in enumerate(events, start=1):
            event_id = f"evt-{round_no:04d}-{seq:04d}-{event['kind']}"
            payload = {key: value for key, value in event.items() if key != "event_id"}
            sealed = {"seq": seq, "event_id": event_id, **payload}
            sealed_events.append(sealed)
            connection.execute(
                """
                INSERT INTO events(
                    commit_key, seq, event_id, round_no, kind, job_id,
                    lease_id, card_id, code, payload_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    commit_key,
                    seq,
                    event_id,
                    round_no,
                    sealed["kind"],
                    sealed.get("job_id"),
                    sealed.get("lease_id"),
                    sealed.get("card_id"),
                    sealed.get("code"),
                    canonical_json(sealed),
                ),
            )
        return sealed_events

    def record_anomaly_fires(
        self,
        connection: sqlite3.Connection,
        *,
        commit_key: str,
        round_no: int,
        anomalies: list[dict[str, Any]],
    ) -> None:
        for anomaly in anomalies:
            connection.execute(
                """
                INSERT OR IGNORE INTO anomaly_fires(
                    anomaly_id, commit_key, round_no, card_id, code, details_json
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    anomaly.get("anomaly_id", "unknown"),
                    commit_key,
                    round_no,
                    anomaly.get("card_id", ""),
                    anomaly.get("code", Code.PROTOCOL),
                    canonical_json(anomaly),
                ),
            )

    def publish_commit(
        self,
        connection: sqlite3.Connection,
        *,
        commit_key: str,
        version: int,
        round_no: int,
        report: dict[str, Any],
    ) -> Path:
        path = self.write_report(commit_key, report)
        pointer = self._pointer_payload(
            version=version,
            commit_key=commit_key,
            round_no=round_no,
            report_digest=report["report_digest"],
            state_digest=report["state_digest"],
        )
        atomic_write_json(self.pointer_path, pointer)
        return path

    def latest_round(self, connection: sqlite3.Connection) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM rounds ORDER BY ledger_version DESC LIMIT 1"
        ).fetchone()

    def recover(self, connection: sqlite3.Connection) -> dict[str, Any]:
        version = int(self.meta(connection, "ledger_version"))
        latest = self.latest_round(connection)
        actions: list[str] = []
        if latest is None:
            expected = self._pointer_payload(
                version=version,
                commit_key="",
                round_no=0,
                report_digest="",
                state_digest=sha256_text(
                    canonical_json({"version": version, "state": "loaded"})
                ),
            )
        else:
            report = json.loads(latest["report_json"])
            report_path = self.report_path(latest["commit_key"])
            if not report_path.exists():
                atomic_write_json(report_path, report)
                actions.append("restored_missing_report")
            else:
                try:
                    disk = json.loads(report_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    disk = None
                if disk != report:
                    atomic_write_json(report_path, report)
                    actions.append("repaired_report")
            expected = self._pointer_payload(
                version=version,
                commit_key=latest["commit_key"],
                round_no=latest["round_no"],
                report_digest=latest["report_digest"],
                state_digest=latest["state_digest"],
            )

        pointer: dict[str, Any] | None = None
        if self.pointer_path.exists():
            try:
                pointer = json.loads(self.pointer_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pointer = None
        if pointer is None or pointer != expected:
            if pointer and int(pointer.get("version", -1)) > version:
                raise BizError(
                    Code.LEDGER_CONSISTENCY,
                    "ledger pointer is newer than the database",
                    details={
                        "pointer_version": pointer.get("version"),
                        "db_version": version,
                    },
                )
            atomic_write_json(self.pointer_path, expected)
            actions.append("rewrote_pointer")

        if actions:
            sequence = (
                connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM recovery_log"
                ).fetchone()[0]
            )
            connection.execute(
                "INSERT INTO recovery_log(sequence, action, details_json) VALUES(?, ?, ?)",
                (
                    sequence,
                    ",".join(actions),
                    canonical_json(
                        {
                            "version": version,
                            "commit_key": expected["commit_key"],
                            "actions": actions,
                        }
                    ),
                ),
            )
        return {
            "version": version,
            "latest_commit_key": expected["commit_key"],
            "actions": actions,
            "pointer": str(self.pointer_path),
        }

    def pointer_snapshot(self) -> dict[str, Any] | None:
        if not self.pointer_path.exists():
            return None
        try:
            return json.loads(self.pointer_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
