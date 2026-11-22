"""SQLite durability for rounds, ledger revisions, views, and recovery."""

from __future__ import annotations

import contextlib
import fcntl
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .canon import canonical_text, json_digest, sha256_hex
from .engine import (
    VERDICT_AUTO_CONVERGE,
    VERDICT_CONVERGED,
    VERDICT_MANUAL_INTERVENTION,
    VERDICT_TOLERATE,
    analyze_observations,
    drift_prior_key,
)
from .errors import (
    E_CLOCK_REGRESSION,
    E_ROUND_CONFLICT,
    DriftError,
)

SCHEMA_VERSION = 1

LEDGER_FIELDS = (
    "entry_id",
    "service",
    "instance_id",
    "drift_type",
    "status",
    "verdict",
    "field_path",
    "expected",
    "actual",
    "basis",
    "evidence",
    "decision",
    "occurrences",
    "first_seen_clock_ms",
    "last_seen_clock_ms",
    "cooldown_until_clock_ms",
    "opened_clock_ms",
    "closed_clock_ms",
    "created_round_id",
    "updated_round_id",
    "previous_entry_id",
    "entry_version",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS round_runs (
    round_id TEXT PRIMARY KEY,
    service TEXT NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    expected_digest TEXT NOT NULL,
    policy_digest TEXT NOT NULL,
    expected_json TEXT NOT NULL,
    policy_json TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    observations_json TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL,
    created_wall_ms INTEGER NOT NULL,
    updated_wall_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS rounds (
    round_id TEXT PRIMARY KEY,
    service TEXT NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    expected_snapshot_id TEXT NOT NULL,
    expected_snapshot_digest TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    policy_digest TEXT NOT NULL,
    input_snapshot_digest TEXT NOT NULL,
    participating_json TEXT NOT NULL,
    started_json TEXT NOT NULL,
    abandoned_json TEXT NOT NULL,
    outcomes_json TEXT NOT NULL,
    decision_stats_json TEXT NOT NULL,
    drift_type_stats_json TEXT NOT NULL,
    replay_inputs_json TEXT NOT NULL,
    computed_json TEXT NOT NULL,
    instance_view_digest TEXT NOT NULL,
    ledger_watermark_version INTEGER NOT NULL,
    committed_wall_ms INTEGER NOT NULL,
    UNIQUE(service, logical_clock_ms, expected_snapshot_digest, policy_digest)
);

CREATE TABLE IF NOT EXISTS ledger_entries (
    entry_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id TEXT NOT NULL UNIQUE,
    service TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    drift_type TEXT NOT NULL,
    status TEXT NOT NULL,
    verdict TEXT NOT NULL,
    field_path TEXT NOT NULL,
    expected_json TEXT NOT NULL,
    actual_json TEXT NOT NULL,
    basis TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    occurrences INTEGER NOT NULL,
    first_seen_clock_ms INTEGER NOT NULL,
    last_seen_clock_ms INTEGER NOT NULL,
    cooldown_until_clock_ms INTEGER NOT NULL,
    opened_clock_ms INTEGER NOT NULL,
    closed_clock_ms INTEGER,
    created_round_id TEXT NOT NULL,
    updated_round_id TEXT NOT NULL,
    previous_entry_id TEXT,
    entry_version INTEGER NOT NULL,
    canonical_digest TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_ledger_lookup
ON ledger_entries(service, instance_id, drift_type, entry_seq);

CREATE INDEX IF NOT EXISTS ix_ledger_service
ON ledger_entries(service, entry_seq);

CREATE TABLE IF NOT EXISTS ledger_revisions (
    entry_id TEXT NOT NULL,
    entry_version INTEGER NOT NULL,
    round_id TEXT NOT NULL,
    clock_ms INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    canonical_digest TEXT NOT NULL,
    PRIMARY KEY(entry_id, entry_version)
);

CREATE TABLE IF NOT EXISTS ledger_evidence (
    evidence_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id TEXT NOT NULL,
    round_id TEXT NOT NULL,
    clock_ms INTEGER NOT NULL,
    entry_version INTEGER NOT NULL,
    evidence_json TEXT NOT NULL,
    canonical_digest TEXT NOT NULL,
    UNIQUE(entry_id, round_id)
);

CREATE TABLE IF NOT EXISTS ledger_events (
    event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id TEXT NOT NULL,
    round_id TEXT NOT NULL,
    clock_ms INTEGER NOT NULL,
    entry_version INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    details_json TEXT NOT NULL,
    canonical_digest TEXT NOT NULL,
    UNIQUE(entry_id, entry_version)
);

CREATE TABLE IF NOT EXISTS round_actions (
    round_id TEXT NOT NULL,
    action_seq INTEGER NOT NULL,
    action_type TEXT NOT NULL,
    entry_id TEXT NOT NULL,
    entry_version INTEGER NOT NULL,
    details_json TEXT NOT NULL,
    PRIMARY KEY(round_id, action_seq),
    UNIQUE(round_id, entry_id, entry_version)
);

CREATE TABLE IF NOT EXISTS instance_views (
    service TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    round_id TEXT NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    status TEXT NOT NULL,
    error_code TEXT,
    report_version INTEGER,
    report_digest TEXT,
    report_clock_ms INTEGER,
    healthy INTEGER,
    view_json TEXT NOT NULL,
    updated_wall_ms INTEGER NOT NULL,
    PRIMARY KEY(service, instance_id)
);

CREATE TABLE IF NOT EXISTS instance_state (
    service TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    last_reported_version INTEGER,
    last_report_clock_ms INTEGER,
    anomaly_count INTEGER NOT NULL DEFAULT 0,
    last_round_id TEXT,
    updated_wall_ms INTEGER NOT NULL,
    PRIMARY KEY(service, instance_id)
);

CREATE TABLE IF NOT EXISTS service_state (
    service TEXT PRIMARY KEY,
    last_committed_round_id TEXT,
    watermark_clock_ms INTEGER NOT NULL,
    ledger_watermark_version INTEGER NOT NULL,
    instance_view_digest TEXT NOT NULL,
    round_count INTEGER NOT NULL,
    updated_wall_ms INTEGER NOT NULL
);
"""


def _json_load(value: str | None, default: Any = None) -> Any:
    import json

    if value is None:
        return default
    return json.loads(value)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _safe_name(value: str) -> str:
    return sha256_hex(value)[:24]


@contextlib.contextmanager
def file_lock(path: str | Path) -> Iterator[None]:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_dir = self.path.parent / f".{self.path.name}.locks"
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=15.0,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=15000")
        return connection

    def _initialize(self) -> None:
        lock_path = self.lock_dir / "schema.lock"
        with file_lock(lock_path):
            connection = self._connect()
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                connection.executescript(SCHEMA)
                connection.execute(
                    "INSERT OR IGNORE INTO schema_meta(key, value) VALUES(?, ?)",
                    ("schema_version", str(SCHEMA_VERSION)),
                )
                row = connection.execute(
                    "SELECT value FROM schema_meta WHERE key='schema_version'"
                ).fetchone()
                if row is None or int(row["value"]) != SCHEMA_VERSION:
                    raise RuntimeError("unsupported database schema version")
            finally:
                connection.close()

    @contextlib.contextmanager
    def read_connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    @contextlib.contextmanager
    def write_connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def service_lock(self, service: str) -> contextlib.AbstractContextManager[None]:
        return file_lock(self.lock_dir / f"service-{_safe_name(service)}.lock")

    def get_round(self, round_id: str) -> dict[str, Any] | None:
        with self.read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM rounds WHERE round_id=?", (round_id,)
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        for key in (
            "participating_json",
            "started_json",
            "abandoned_json",
            "outcomes_json",
            "decision_stats_json",
            "drift_type_stats_json",
            "replay_inputs_json",
            "computed_json",
        ):
            result[key.removesuffix("_json")] = _json_load(result.pop(key))
        return result

    def get_round_run(self, round_id: str) -> dict[str, Any] | None:
        with self.read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM round_runs WHERE round_id=?", (round_id,)
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["expected"] = _json_load(result.pop("expected_json"))
        result["policy"] = _json_load(result.pop("policy_json"))
        result["plan"] = _json_load(result.pop("plan_json"))
        result["observations"] = _json_load(result.pop("observations_json"), {})
        return result

    def list_unfinished_runs(self) -> list[dict[str, Any]]:
        with self.read_connection() as connection:
            rows = connection.execute(
                "SELECT round_id FROM round_runs WHERE state<>'COMMITTED' ORDER BY created_wall_ms, round_id"
            ).fetchall()
        return [self.get_round_run(row["round_id"]) for row in rows if row is not None]  # type: ignore[list-item]

    def prepare_round_run(
        self,
        *,
        round_id: str,
        service: dict[str, Any],
        logical_clock_ms: int,
        expected_digest: str,
        policy_digest: str,
        expected_json: str,
        policy_json: str,
        plan: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        """Create or resume a durable pre-commit round journal."""

        with self.write_connection() as connection:
            committed = connection.execute(
                "SELECT round_id FROM rounds WHERE round_id=?", (round_id,)
            ).fetchone()
            if committed is not None:
                return "COMMITTED", self.get_round(round_id) or {}

            state_row = connection.execute(
                "SELECT watermark_clock_ms FROM service_state WHERE service=?",
                (service["service"],),
            ).fetchone()
            if state_row is not None and logical_clock_ms < state_row["watermark_clock_ms"]:
                raise DriftError(
                    E_CLOCK_REGRESSION,
                    "round logical clock is older than the committed service watermark",
                    details={
                        "service": service["service"],
                        "round_clock_ms": logical_clock_ms,
                        "watermark_clock_ms": state_row["watermark_clock_ms"],
                    },
                )

            existing = connection.execute(
                "SELECT * FROM round_runs WHERE round_id=?", (round_id,)
            ).fetchone()
            if existing is not None:
                if (
                    existing["service"] != service["service"]
                    or existing["logical_clock_ms"] != logical_clock_ms
                    or existing["expected_digest"] != expected_digest
                    or existing["policy_digest"] != policy_digest
                ):
                    raise DriftError(
                        E_ROUND_CONFLICT,
                        "round_id is already bound to different inputs",
                        details={"round_id": round_id},
                    )
                return existing["state"], self.get_round_run(round_id) or {}

            now = _now_ms()
            connection.execute(
                """
                INSERT INTO round_runs(
                    round_id, service, logical_clock_ms, expected_digest,
                    policy_digest, expected_json, policy_json, plan_json,
                    observations_json, state, created_wall_ms, updated_wall_ms
                ) VALUES(?,?,?,?,?,?,?,?,?,'PREPARED',?,?)
                """,
                (
                    round_id,
                    service["service"],
                    logical_clock_ms,
                    expected_digest,
                    policy_digest,
                    expected_json,
                    policy_json,
                    canonical_text(plan),
                    "{}",
                    now,
                    now,
                ),
            )
        return "PREPARED", self.get_round_run(round_id) or {}

    def save_observations(
        self, round_id: str, observations: dict[str, Any], state: str
    ) -> None:
        with self.write_connection() as connection:
            cursor = connection.execute(
                """
                UPDATE round_runs
                SET observations_json=?, state=?, updated_wall_ms=?
                WHERE round_id=?
                """,
                (canonical_text(observations), state, _now_ms(), round_id),
            )
            if cursor.rowcount != 1:
                raise DriftError(
                    E_ROUND_CONFLICT,
                    "round run journal disappeared before observation save",
                    details={"round_id": round_id},
                )

    def previous_successful_version(
        self, service: str, instance_id: str
    ) -> int | None:
        with self.read_connection() as connection:
            row = connection.execute(
                """
                SELECT last_reported_version FROM instance_state
                WHERE service=? AND instance_id=?
                """,
                (service, instance_id),
            ).fetchone()
        return None if row is None else row["last_reported_version"]

    def previous_report_clock(
        self, service: str, instance_id: str
    ) -> int | None:
        with self.read_connection() as connection:
            row = connection.execute(
                """
                SELECT last_report_clock_ms FROM instance_state
                WHERE service=? AND instance_id=?
                """,
                (service, instance_id),
            ).fetchone()
        return None if row is None else row["last_report_clock_ms"]

    def _load_ledger_context(
        self, connection: sqlite3.Connection, service_name: str
    ) -> dict[str, dict[str, dict[str, Any]]]:
        rows = connection.execute(
            "SELECT * FROM ledger_entries WHERE service=? ORDER BY entry_seq",
            (service_name,),
        ).fetchall()
        context: dict[str, dict[str, dict[str, Any]]] = {}
        for row in rows:
            entry = self._entry_from_row(row)
            context.setdefault(entry["instance_id"], {})[
                drift_prior_key(entry["drift_type"], entry["field_path"])
            ] = entry
        return context

    @staticmethod
    def _entry_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "entry_id": row["entry_id"],
            "service": row["service"],
            "instance_id": row["instance_id"],
            "drift_type": row["drift_type"],
            "status": row["status"],
            "verdict": row["verdict"],
            "field_path": row["field_path"],
            "expected": _json_load(row["expected_json"]),
            "actual": _json_load(row["actual_json"]),
            "basis": row["basis"],
            "evidence": _json_load(row["evidence_json"]),
            "decision": _json_load(row["decision_json"]),
            "occurrences": row["occurrences"],
            "first_seen_clock_ms": row["first_seen_clock_ms"],
            "last_seen_clock_ms": row["last_seen_clock_ms"],
            "cooldown_until_clock_ms": row["cooldown_until_clock_ms"],
            "opened_clock_ms": row["opened_clock_ms"],
            "closed_clock_ms": row["closed_clock_ms"],
            "created_round_id": row["created_round_id"],
            "updated_round_id": row["updated_round_id"],
            "previous_entry_id": row["previous_entry_id"],
            "entry_version": row["entry_version"],
            "canonical_digest": row["canonical_digest"],
        }

    @staticmethod
    def _entry_payload(entry: dict[str, Any]) -> dict[str, Any]:
        return {field: entry[field] for field in LEDGER_FIELDS}

    def _write_revision(
        self,
        connection: sqlite3.Connection,
        entry: dict[str, Any],
        round_id: str,
        clock_ms: int,
    ) -> str:
        payload = self._entry_payload(entry)
        digest = json_digest(payload)
        connection.execute(
            """
            INSERT INTO ledger_revisions(
                entry_id, entry_version, round_id, clock_ms, payload_json, canonical_digest
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                entry["entry_id"],
                entry["entry_version"],
                round_id,
                clock_ms,
                canonical_text(payload),
                digest,
            ),
        )
        return digest

    def _write_event(
        self,
        connection: sqlite3.Connection,
        entry: dict[str, Any],
        round_id: str,
        clock_ms: int,
        event_type: str,
        details: dict[str, Any],
    ) -> None:
        payload = {
            "entry_id": entry["entry_id"],
            "entry_version": entry["entry_version"],
            "round_id": round_id,
            "clock_ms": clock_ms,
            "event_type": event_type,
            "details": details,
        }
        connection.execute(
            """
            INSERT INTO ledger_events(
                entry_id, round_id, clock_ms, entry_version,
                event_type, details_json, canonical_digest
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                entry["entry_id"],
                round_id,
                clock_ms,
                entry["entry_version"],
                event_type,
                canonical_text(details),
                json_digest(payload),
            ),
        )

    def _write_evidence(
        self,
        connection: sqlite3.Connection,
        entry: dict[str, Any],
        round_id: str,
        clock_ms: int,
        evidence: dict[str, Any],
    ) -> None:
        payload = {
            "entry_id": entry["entry_id"],
            "entry_version": entry["entry_version"],
            "round_id": round_id,
            "clock_ms": clock_ms,
            "evidence": evidence,
        }
        connection.execute(
            """
            INSERT INTO ledger_evidence(
                entry_id, round_id, clock_ms, entry_version,
                evidence_json, canonical_digest
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                entry["entry_id"],
                round_id,
                clock_ms,
                entry["entry_version"],
                canonical_text(evidence),
                json_digest(payload),
            ),
        )

    @staticmethod
    def _status_for_verdict(verdict: str) -> str:
        return "TOLERATED" if verdict == VERDICT_TOLERATE else "OPEN"

    @staticmethod
    def _event_for_status(prior_status: str | None, status: str) -> str:
        if prior_status == "CLOSED":
            return "REOPENED"
        if status == "TOLERATED":
            return "TOLERATED"
        if prior_status == "TOLERATED" and status == "OPEN":
            return "ESCALATED"
        if prior_status is None:
            return "OPENED"
        return "UPDATED"

    def _upsert_drift_entry(
        self,
        connection: sqlite3.Connection,
        *,
        service: dict[str, Any],
        instance: dict[str, Any],
        report: dict[str, Any],
        drift: dict[str, Any],
        prior: dict[str, Any] | None,
        policy: dict[str, Any],
        round_id: str,
        clock_ms: int,
        expected_snapshot_id: str,
        action_seq: int,
        actions: list[dict[str, Any]],
    ) -> None:
        verdict = drift["decision"]["verdict"]
        status = self._status_for_verdict(verdict)
        evidence = {
            "source": {
                "kind": "loopback_http_report",
                "endpoint": instance["endpoint"],
                "expected_snapshot_id": expected_snapshot_id,
                "policy_id": policy["policy_id"],
            },
            "field_path": drift["field_path"],
            "expected": drift["expected"],
            "actual": drift["actual"],
            "basis": drift["basis"],
            "drift_evidence": drift["evidence"],
            "decision": drift["decision"],
            "report": {
                "version": report.get("version"),
                "digest": report.get("digest"),
                "logical_clock_ms": report.get("logical_clock_ms"),
                "healthy": report.get("healthy"),
                "missing_fields": report.get("missing_fields", []),
                "report_digest": json_digest(report),
            },
            "observed_round_clock_ms": clock_ms,
        }
        if prior is not None and clock_ms <= prior["cooldown_until_clock_ms"]:
            entry = dict(prior)
            prior_status = prior["status"]
            entry["status"] = "REOPENED" if prior_status == "CLOSED" else status
            entry["verdict"] = verdict
            entry["field_path"] = drift["field_path"]
            entry["expected"] = drift["expected"]
            entry["actual"] = drift["actual"]
            entry["basis"] = drift["basis"]
            entry["evidence"] = evidence
            entry["decision"] = drift["decision"]
            entry["occurrences"] = prior["occurrences"] + 1
            entry["last_seen_clock_ms"] = max(prior["last_seen_clock_ms"], clock_ms)
            entry["cooldown_until_clock_ms"] = (
                entry["last_seen_clock_ms"] + policy["dedup_cooldown_ms"]
            )
            entry["closed_clock_ms"] = None
            entry["updated_round_id"] = round_id
            entry["entry_version"] = prior["entry_version"] + 1
            event_type = self._event_for_status(prior_status, status)
            action_type = "REOPEN" if prior_status == "CLOSED" else "DEDUP"
        else:
            previous_entry_id = None if prior is None else prior["entry_id"]
            entry_key = {
                "service": service["service"],
                "instance_id": instance["instance_id"],
                "drift_type": drift["drift_type"],
                "field_path": drift["field_path"],
                "clock_ms": clock_ms,
                "previous_entry_id": previous_entry_id,
            }
            entry = {
                "entry_id": "drift-" + sha256_hex(entry_key)[:24],
                "service": service["service"],
                "instance_id": instance["instance_id"],
                "drift_type": drift["drift_type"],
                "status": status,
                "verdict": verdict,
                "field_path": drift["field_path"],
                "expected": drift["expected"],
                "actual": drift["actual"],
                "basis": drift["basis"],
                "evidence": evidence,
                "decision": drift["decision"],
                "occurrences": 1,
                "first_seen_clock_ms": clock_ms,
                "last_seen_clock_ms": clock_ms,
                "cooldown_until_clock_ms": clock_ms + policy["dedup_cooldown_ms"],
                "opened_clock_ms": clock_ms,
                "closed_clock_ms": None,
                "created_round_id": round_id,
                "updated_round_id": round_id,
                "previous_entry_id": previous_entry_id,
                "entry_version": 1,
            }
            event_type = "OPENED" if status == "OPEN" else "TOLERATED"
            action_type = "CREATE"

        digest = self._write_revision(connection, entry, round_id, clock_ms)
        if action_type == "CREATE":
            connection.execute(
                """
                INSERT INTO ledger_entries(
                    entry_id, service, instance_id, drift_type, status, verdict,
                    field_path, expected_json, actual_json, basis, evidence_json,
                    decision_json, occurrences, first_seen_clock_ms,
                    last_seen_clock_ms, cooldown_until_clock_ms, opened_clock_ms,
                    closed_clock_ms, created_round_id, updated_round_id,
                    previous_entry_id, entry_version, canonical_digest
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    entry["entry_id"],
                    entry["service"],
                    entry["instance_id"],
                    entry["drift_type"],
                    entry["status"],
                    entry["verdict"],
                    entry["field_path"],
                    canonical_text(entry["expected"]),
                    canonical_text(entry["actual"]),
                    entry["basis"],
                    canonical_text(entry["evidence"]),
                    canonical_text(entry["decision"]),
                    entry["occurrences"],
                    entry["first_seen_clock_ms"],
                    entry["last_seen_clock_ms"],
                    entry["cooldown_until_clock_ms"],
                    entry["opened_clock_ms"],
                    entry["closed_clock_ms"],
                    entry["created_round_id"],
                    entry["updated_round_id"],
                    entry["previous_entry_id"],
                    entry["entry_version"],
                    digest,
                ),
            )
        else:
            connection.execute(
                """
                UPDATE ledger_entries SET
                    status=?, verdict=?, field_path=?, expected_json=?,
                    actual_json=?, basis=?, evidence_json=?, decision_json=?,
                    occurrences=?, last_seen_clock_ms=?,
                    cooldown_until_clock_ms=?, closed_clock_ms=?,
                    updated_round_id=?, entry_version=?, canonical_digest=?
                WHERE entry_id=?
                """,
                (
                    entry["status"],
                    entry["verdict"],
                    entry["field_path"],
                    canonical_text(entry["expected"]),
                    canonical_text(entry["actual"]),
                    entry["basis"],
                    canonical_text(entry["evidence"]),
                    canonical_text(entry["decision"]),
                    entry["occurrences"],
                    entry["last_seen_clock_ms"],
                    entry["cooldown_until_clock_ms"],
                    entry["closed_clock_ms"],
                    entry["updated_round_id"],
                    entry["entry_version"],
                    digest,
                    entry["entry_id"],
                ),
            )
        self._write_event(
            connection,
            entry,
            round_id,
            clock_ms,
            event_type,
            {
                "drift_type": drift["drift_type"],
                "verdict": verdict,
                "reason_code": drift["decision"]["reason_code"],
            },
        )
        self._write_evidence(connection, entry, round_id, clock_ms, evidence)
        action = {
            "action_seq": action_seq,
            "action_type": action_type,
            "entry_id": entry["entry_id"],
            "entry_version": entry["entry_version"],
            "drift_type": drift["drift_type"],
        }
        connection.execute(
            """
            INSERT INTO round_actions(
                round_id, action_seq, action_type, entry_id,
                entry_version, details_json
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                round_id,
                action_seq,
                action_type,
                entry["entry_id"],
                entry["entry_version"],
                canonical_text(action),
            ),
        )
        actions.append(action)

    def _close_entry(
        self,
        connection: sqlite3.Connection,
        *,
        entry: dict[str, Any],
        round_id: str,
        clock_ms: int,
        action_seq: int,
        actions: list[dict[str, Any]],
        reason: str = "the successful report no longer contains this drift type",
    ) -> None:
        if entry["status"] == "CLOSED":
            return
        updated = dict(entry)
        updated["status"] = "CLOSED"
        updated["closed_clock_ms"] = clock_ms
        updated["updated_round_id"] = round_id
        updated["entry_version"] = entry["entry_version"] + 1
        evidence = {
            "kind": "CLOSE",
            "basis": reason,
            "round_id": round_id,
            "observed_round_clock_ms": clock_ms,
            "prior_status": entry["status"],
            "source": entry.get("evidence", {}).get("source")
            if isinstance(entry.get("evidence"), dict)
            else None,
        }
        updated["evidence"] = evidence
        digest = self._write_revision(connection, updated, round_id, clock_ms)
        connection.execute(
            """
            UPDATE ledger_entries SET status='CLOSED', closed_clock_ms=?,
                updated_round_id=?, entry_version=?, evidence_json=?,
                canonical_digest=?
            WHERE entry_id=?
            """,
            (
                clock_ms,
                round_id,
                updated["entry_version"],
                canonical_text(evidence),
                digest,
                entry["entry_id"],
            ),
        )
        self._write_event(
            connection,
            updated,
            round_id,
            clock_ms,
            "CLOSED",
            {"drift_type": entry["drift_type"], "prior_status": entry["status"]},
        )
        self._write_evidence(connection, updated, round_id, clock_ms, evidence)
        action = {
            "action_seq": action_seq,
            "action_type": "CLOSE",
            "entry_id": updated["entry_id"],
            "entry_version": updated["entry_version"],
            "drift_type": updated["drift_type"],
        }
        connection.execute(
            """
            INSERT INTO round_actions(
                round_id, action_seq, action_type, entry_id,
                entry_version, details_json
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                round_id,
                action_seq,
                "CLOSE",
                updated["entry_id"],
                updated["entry_version"],
                canonical_text(action),
            ),
        )
        actions.append(action)

    def _apply_observations(
        self,
        connection: sqlite3.Connection,
        *,
        expected: dict[str, Any],
        service: dict[str, Any],
        policy: dict[str, Any],
        observations: dict[str, Any],
        round_id: str,
        clock_ms: int,
        actions: list[dict[str, Any]],
        crash_after_writes: int | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        ledger_context = self._load_ledger_context(connection, service["service"])
        computed = analyze_observations(
            expected,
            service,
            policy,
            observations,
            clock_ms,
            ledger_context,
        )
        action_seq = 0
        for instance_analysis in computed["instances"]:
            instance_id = instance_analysis["instance_id"]
            expected_instance = next(
                item for item in service["instances"] if item["instance_id"] == instance_id
            )
            observation = observations[instance_id]
            report = observation["report"]
            prior_map = ledger_context.get(instance_id, {})
            active_before = {
                prior_key: entry
                for prior_key, entry in prior_map.items()
                if entry["status"] != "CLOSED"
            }
            observed_keys = {
                drift_prior_key(drift["drift_type"], drift["field_path"])
                for drift in instance_analysis["drifts"]
            }
            for drift in instance_analysis["drifts"]:
                prior_key = drift_prior_key(drift["drift_type"], drift["field_path"])
                prior = prior_map.get(prior_key)
                if (
                    prior is not None
                    and prior["status"] != "CLOSED"
                    and clock_ms > prior["cooldown_until_clock_ms"]
                ):
                    action_seq += 1
                    self._close_entry(
                        connection,
                        entry=prior,
                        round_id=round_id,
                        clock_ms=clock_ms,
                        action_seq=action_seq,
                        actions=actions,
                        reason="superseded by a new ledger entry after dedup cooldown",
                    )
                    if (
                        crash_after_writes is not None
                        and len(actions) >= crash_after_writes
                    ):
                        from .crash import hard_kill

                        hard_kill("after_ledger_writes")
                action_seq += 1
                self._upsert_drift_entry(
                    connection,
                    service=service,
                    instance=expected_instance,
                    report=report,
                    drift=drift,
                    prior=prior,
                    policy=policy,
                    round_id=round_id,
                    clock_ms=clock_ms,
                    expected_snapshot_id=expected["snapshot_id"],
                    action_seq=action_seq,
                    actions=actions,
                )
                if crash_after_writes is not None and len(actions) >= crash_after_writes:
                    from .crash import hard_kill

                    hard_kill("after_ledger_writes")
            for prior_key in sorted(set(active_before) - observed_keys):
                action_seq += 1
                self._close_entry(
                    connection,
                    entry=active_before[prior_key],
                    round_id=round_id,
                    clock_ms=clock_ms,
                    action_seq=action_seq,
                    actions=actions,
                )
                if crash_after_writes is not None and len(actions) >= crash_after_writes:
                    from .crash import hard_kill

                    hard_kill("after_ledger_writes")
        return computed, ledger_context

    def _update_instance_views(
        self,
        connection: sqlite3.Connection,
        *,
        service: dict[str, Any],
        observations: dict[str, Any],
        round_id: str,
        clock_ms: int,
    ) -> None:
        now = _now_ms()
        for instance in service["instances"]:
            instance_id = instance["instance_id"]
            observation = observations.get(instance_id)
            if observation is None or observation.get("outcome") == "ABANDONED":
                continue
            report = observation.get("report") or {}
            status = "OK" if observation["outcome"] == "PULLED_OK" else "ERROR"
            error_code = observation.get("error_code")
            version = report.get("version")
            digest = report.get("digest")
            report_clock = report.get("logical_clock_ms")
            healthy = report.get("healthy")
            view_payload = {
                "service": service["service"],
                "instance_id": instance_id,
                "round_id": round_id,
                "logical_clock_ms": clock_ms,
                "status": status,
                "error_code": error_code,
                "diagnostic": observation.get("diagnostic", {}),
                "report": report or None,
            }
            connection.execute(
                """
                INSERT INTO instance_views(
                    service, instance_id, round_id, logical_clock_ms, status,
                    error_code, report_version, report_digest, report_clock_ms,
                    healthy, view_json, updated_wall_ms
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(service, instance_id) DO UPDATE SET
                    round_id=excluded.round_id,
                    logical_clock_ms=excluded.logical_clock_ms,
                    status=excluded.status,
                    error_code=excluded.error_code,
                    report_version=excluded.report_version,
                    report_digest=excluded.report_digest,
                    report_clock_ms=excluded.report_clock_ms,
                    healthy=excluded.healthy,
                    view_json=excluded.view_json,
                    updated_wall_ms=excluded.updated_wall_ms
                """,
                (
                    service["service"],
                    instance_id,
                    round_id,
                    clock_ms,
                    status,
                    error_code,
                    version,
                    digest,
                    report_clock,
                    None if healthy is None else int(healthy),
                    canonical_text(view_payload),
                    now,
                ),
            )
            anomaly_increment = 0 if status == "OK" else 1
            existing_state = connection.execute(
                """
                SELECT last_reported_version, last_report_clock_ms, anomaly_count
                FROM instance_state WHERE service=? AND instance_id=?
                """,
                (service["service"], instance_id),
            ).fetchone()
            last_version = None if existing_state is None else existing_state["last_reported_version"]
            last_clock = None if existing_state is None else existing_state["last_report_clock_ms"]
            anomaly_count = 0 if existing_state is None else existing_state["anomaly_count"]
            if status == "OK" and version is not None:
                last_version = version
            if status == "OK" and report_clock is not None:
                last_clock = max(last_clock or 0, report_clock)
            connection.execute(
                """
                INSERT INTO instance_state(
                    service, instance_id, last_reported_version,
                    last_report_clock_ms, anomaly_count, last_round_id, updated_wall_ms
                ) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(service, instance_id) DO UPDATE SET
                    last_reported_version=excluded.last_reported_version,
                    last_report_clock_ms=excluded.last_report_clock_ms,
                    anomaly_count=excluded.anomaly_count,
                    last_round_id=excluded.last_round_id,
                    updated_wall_ms=excluded.updated_wall_ms
                """,
                (
                    service["service"],
                    instance_id,
                    last_version,
                    last_clock,
                    anomaly_count + anomaly_increment,
                    round_id,
                    now,
                ),
            )

    @staticmethod
    def _view_digest(
        connection: sqlite3.Connection, service_name: str
    ) -> str:
        rows = connection.execute(
            """
            SELECT * FROM instance_views WHERE service=?
            ORDER BY instance_id
            """,
            (service_name,),
        ).fetchall()
        payload = [
            {
                "instance_id": row["instance_id"],
                "round_id": row["round_id"],
                "logical_clock_ms": row["logical_clock_ms"],
                "status": row["status"],
                "error_code": row["error_code"],
                "view": _json_load(row["view_json"]),
            }
            for row in rows
        ]
        return json_digest(payload)

    def commit_round(
        self,
        *,
        run: dict[str, Any],
        expected: dict[str, Any],
        service: dict[str, Any],
        policy: dict[str, Any],
        observations: dict[str, Any],
        abandoned: list[dict[str, Any]],
        crash_after_writes: int | None = None,
        service_lock_held: bool = False,
    ) -> dict[str, Any]:
        """Atomically commit ledger, views, service watermarks, and round summary."""

        round_id = run["round_id"]
        clock_ms = run["logical_clock_ms"]
        lock_context = (
            contextlib.nullcontext()
            if service_lock_held
            else self.service_lock(service["service"])
        )
        with lock_context:
            with self.write_connection() as connection:
                existing = connection.execute(
                    "SELECT * FROM rounds WHERE round_id=?", (round_id,)
                ).fetchone()
                if existing is not None:
                    return self.get_round(round_id) or {}

                state_row = connection.execute(
                    "SELECT * FROM service_state WHERE service=?",
                    (service["service"],),
                ).fetchone()
                if (
                    state_row is not None
                    and clock_ms < state_row["watermark_clock_ms"]
                ):
                    raise DriftError(
                        E_CLOCK_REGRESSION,
                        "round logical clock is older than the committed service watermark",
                        details={
                            "service": service["service"],
                            "round_clock_ms": clock_ms,
                            "watermark_clock_ms": state_row["watermark_clock_ms"],
                        },
                    )

                actions: list[dict[str, Any]] = []
                computed, ledger_context = self._apply_observations(
                    connection,
                    expected=expected,
                    service=service,
                    policy=policy,
                    observations=observations,
                    round_id=round_id,
                    clock_ms=clock_ms,
                    actions=actions,
                    crash_after_writes=crash_after_writes,
                )
                self._update_instance_views(
                    connection,
                    service=service,
                    observations=observations,
                    round_id=round_id,
                    clock_ms=clock_ms,
                )

                participating = [item["instance_id"] for item in service["instances"]]
                started = [
                    instance_id
                    for instance_id in participating
                    if observations.get(instance_id, {}).get("outcome")
                    in ("PULLED_OK", "PULLED_ERROR")
                ]
                outcomes = [
                    {
                        "instance_id": instance_id,
                        "outcome": observations.get(instance_id, {}).get(
                            "outcome", "ABANDONED"
                        ),
                        "error_code": observations.get(instance_id, {}).get("error_code"),
                    }
                    for instance_id in participating
                ]
                abandoned = sorted(
                    abandoned,
                    key=lambda item: (item["instance_id"], item["error_code"]),
                )
                decision_stats = {
                    VERDICT_TOLERATE: 0,
                    VERDICT_AUTO_CONVERGE: 0,
                    VERDICT_MANUAL_INTERVENTION: 0,
                    VERDICT_CONVERGED: 0,
                }
                for item in computed["instances"]:
                    decision_stats[item["aggregate_decision"]["verdict"]] += 1
                drift_type_stats = dict(
                    sorted(computed["summary"]["drift_type_counts"].items())
                )
                replay_inputs = {
                    "schema_version": 1,
                    "round_id": round_id,
                    "service": service,
                    "policy": policy,
                    "expected_snapshot_id": expected["snapshot_id"],
                    "expected_logical_clock_ms": expected["logical_clock_ms"],
                    "logical_clock_ms": clock_ms,
                    "observations": observations,
                    "abandoned": abandoned,
                    "ledger_context": ledger_context,
                }
                input_digest = json_digest(
                    {
                        "expected": {
                            "schema_version": 1,
                            "snapshot_id": expected["snapshot_id"],
                            "logical_clock_ms": expected["logical_clock_ms"],
                            "service": service,
                        },
                        "policy": policy,
                        "observations": observations,
                        "abandoned": abandoned,
                        "clock_ms": clock_ms,
                    }
                )
                view_digest = self._view_digest(connection, service["service"])
                prior_ledger_version = (
                    state_row["ledger_watermark_version"] if state_row is not None else 0
                )
                final_ledger_version = prior_ledger_version + len(actions)
                prior_round_count = state_row["round_count"] if state_row is not None else 0
                summary_payload = {
                    "round_id": round_id,
                    "service": service["service"],
                    "logical_clock_ms": clock_ms,
                    "expected_snapshot_id": expected["snapshot_id"],
                    "expected_snapshot_digest": json_digest(service),
                    "policy_id": policy["policy_id"],
                    "policy_digest": json_digest(policy),
                    "input_snapshot_digest": input_digest,
                    "participating_instances": participating,
                    "started_instances": started,
                    "abandoned": abandoned,
                    "outcomes": outcomes,
                    "decision_stats": decision_stats,
                    "drift_type_stats": drift_type_stats,
                    "instance_view_digest": view_digest,
                    "ledger_watermark_version": final_ledger_version,
                    "actions": actions,
                    "analysis": computed,
                    "summary_digest": json_digest(
                        {
                            "input_snapshot_digest": input_digest,
                            "decision_stats": decision_stats,
                            "drift_type_stats": drift_type_stats,
                            "actions": actions,
                            "analysis": computed,
                        }
                    ),
                }
                connection.execute(
                    """
                    INSERT INTO rounds(
                        round_id, service, logical_clock_ms, expected_snapshot_id,
                        expected_snapshot_digest, policy_id, policy_digest,
                        input_snapshot_digest, participating_json, started_json,
                        abandoned_json, outcomes_json, decision_stats_json,
                        drift_type_stats_json, replay_inputs_json, computed_json,
                        instance_view_digest, ledger_watermark_version,
                        committed_wall_ms
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        round_id,
                        service["service"],
                        clock_ms,
                        expected["snapshot_id"],
                        json_digest(service),
                        policy["policy_id"],
                        json_digest(policy),
                        input_digest,
                        canonical_text(participating),
                        canonical_text(started),
                        canonical_text(abandoned),
                        canonical_text(outcomes),
                        canonical_text(decision_stats),
                        canonical_text(drift_type_stats),
                        canonical_text(replay_inputs),
                        canonical_text(summary_payload),
                        view_digest,
                        final_ledger_version,
                        _now_ms(),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO service_state(
                        service, last_committed_round_id, watermark_clock_ms,
                        ledger_watermark_version, instance_view_digest,
                        round_count, updated_wall_ms
                    ) VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(service) DO UPDATE SET
                        last_committed_round_id=excluded.last_committed_round_id,
                        watermark_clock_ms=excluded.watermark_clock_ms,
                        ledger_watermark_version=excluded.ledger_watermark_version,
                        instance_view_digest=excluded.instance_view_digest,
                        round_count=excluded.round_count,
                        updated_wall_ms=excluded.updated_wall_ms
                    """,
                    (
                        service["service"],
                        round_id,
                        clock_ms,
                        final_ledger_version,
                        view_digest,
                        prior_round_count + 1,
                        _now_ms(),
                    ),
                )
                connection.execute(
                    "UPDATE round_runs SET state='COMMITTED', updated_wall_ms=? WHERE round_id=?",
                    (_now_ms(), round_id),
                )
                from .crash import maybe_crash

                maybe_crash("before_commit")
            return self.get_round(round_id) or {}

    def list_ledger_entries(
        self, service: str | None = None
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM ledger_entries"
        params: tuple[Any, ...] = ()
        if service is not None:
            query += " WHERE service=?"
            params = (service,)
        query += " ORDER BY service, instance_id, drift_type, entry_seq"
        with self.read_connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._entry_from_row(row) for row in rows]

    def ledger_events(self, entry_id: str) -> list[dict[str, Any]]:
        with self.read_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM ledger_events WHERE entry_id=? ORDER BY event_seq",
                (entry_id,),
            ).fetchall()
        return [
            {
                "event_seq": row["event_seq"],
                "entry_id": row["entry_id"],
                "round_id": row["round_id"],
                "clock_ms": row["clock_ms"],
                "entry_version": row["entry_version"],
                "event_type": row["event_type"],
                "details": _json_load(row["details_json"]),
                "canonical_digest": row["canonical_digest"],
            }
            for row in rows
        ]

    def ledger_revisions(self, entry_id: str) -> list[dict[str, Any]]:
        with self.read_connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM ledger_revisions
                WHERE entry_id=? ORDER BY entry_version
                """,
                (entry_id,),
            ).fetchall()
        return [
            {
                "entry_id": row["entry_id"],
                "entry_version": row["entry_version"],
                "round_id": row["round_id"],
                "clock_ms": row["clock_ms"],
                "payload": _json_load(row["payload_json"]),
                "canonical_digest": row["canonical_digest"],
            }
            for row in rows
        ]

    def ledger_evidence(self, entry_id: str) -> list[dict[str, Any]]:
        with self.read_connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM ledger_evidence
                WHERE entry_id=? ORDER BY evidence_seq
                """,
                (entry_id,),
            ).fetchall()
        return [
            {
                "evidence_seq": row["evidence_seq"],
                "entry_id": row["entry_id"],
                "round_id": row["round_id"],
                "clock_ms": row["clock_ms"],
                "entry_version": row["entry_version"],
                "evidence": _json_load(row["evidence_json"]),
                "canonical_digest": row["canonical_digest"],
            }
            for row in rows
        ]

    def instance_views(self, service: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM instance_views"
        params: tuple[Any, ...] = ()
        if service is not None:
            query += " WHERE service=?"
            params = (service,)
        query += " ORDER BY service, instance_id"
        with self.read_connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [
            {
                "service": row["service"],
                "instance_id": row["instance_id"],
                "round_id": row["round_id"],
                "logical_clock_ms": row["logical_clock_ms"],
                "status": row["status"],
                "error_code": row["error_code"],
                "report_version": row["report_version"],
                "report_digest": row["report_digest"],
                "report_clock_ms": row["report_clock_ms"],
                "healthy": None if row["healthy"] is None else bool(row["healthy"]),
                "view": _json_load(row["view_json"]),
            }
            for row in rows
        ]

    def instance_states(self, service: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM instance_state"
        params: tuple[Any, ...] = ()
        if service is not None:
            query += " WHERE service=?"
            params = (service,)
        query += " ORDER BY service, instance_id"
        with self.read_connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def list_rounds(self, service: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM rounds"
        params: tuple[Any, ...] = ()
        if service is not None:
            query += " WHERE service=?"
            params = (service,)
        query += " ORDER BY service, logical_clock_ms, round_id"
        with self.read_connection() as connection:
            rows = connection.execute(query, params).fetchall()
        results = []
        for row in rows:
            result = dict(row)
            for key in (
                "participating_json",
                "started_json",
                "abandoned_json",
                "outcomes_json",
                "decision_stats_json",
                "drift_type_stats_json",
                "replay_inputs_json",
                "computed_json",
            ):
                result[key.removesuffix("_json")] = _json_load(result.pop(key))
            results.append(result)
        return results

    def service_states(self) -> list[dict[str, Any]]:
        with self.read_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM service_state ORDER BY service"
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_round_state(self, round_id: str, state: str) -> None:
        with self.write_connection() as connection:
            connection.execute(
                "UPDATE round_runs SET state=?, updated_wall_ms=? WHERE round_id=?",
                (state, _now_ms(), round_id),
            )

    def project_ledger(self, service: str | None = None) -> list[dict[str, Any]]:
        """Stable projection used for crash/uninterrupted equality checks."""

        projection = []
        for entry in self.list_ledger_entries(service):
            events = self.ledger_events(entry["entry_id"])
            revisions = self.ledger_revisions(entry["entry_id"])
            evidence = self.ledger_evidence(entry["entry_id"])
            projection.append(
                {
                    "entry": entry,
                    "events": [
                        {
                            "entry_version": item["entry_version"],
                            "round_id": item["round_id"],
                            "clock_ms": item["clock_ms"],
                            "event_type": item["event_type"],
                            "details": item["details"],
                            "canonical_digest": item["canonical_digest"],
                        }
                        for item in events
                    ],
                    "revisions": [
                        {
                            "entry_version": item["entry_version"],
                            "round_id": item["round_id"],
                            "clock_ms": item["clock_ms"],
                            "payload": item["payload"],
                            "canonical_digest": item["canonical_digest"],
                        }
                        for item in revisions
                    ],
                    "evidence": [
                        {
                            "round_id": item["round_id"],
                            "clock_ms": item["clock_ms"],
                            "entry_version": item["entry_version"],
                            "evidence": item["evidence"],
                            "canonical_digest": item["canonical_digest"],
                        }
                        for item in evidence
                    ],
                }
            )
        return projection

    def database_digest(self) -> str:
        payload = {
            "ledger": self.project_ledger(),
            "views": self.instance_views(),
            "states": [
                {
                    key: value
                    for key, value in state.items()
                    if key not in ("updated_wall_ms",)
                }
                for state in self.instance_states()
            ],
            "rounds": [
                {
                    key: value
                    for key, value in round_record.items()
                    if key not in ("committed_wall_ms",)
                }
                for round_record in self.list_rounds()
            ],
            "service_states": [
                {
                    key: value
                    for key, value in state.items()
                    if key not in ("updated_wall_ms",)
                }
                for state in self.service_states()
            ],
        }
        return json_digest(payload)
