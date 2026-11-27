from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .evaluator import build_decision_record
from .graph import PropagationPlan, build_plan
from .model import Gate, Project


STATE_PENDING = "pending"
STATE_PROPAGATING = "propagating"
STATE_ACTIVE = "active"
STATE_FAILED = "failed"

STATE_LABELS = {
    STATE_PENDING: "待传播",
    STATE_PROPAGATING: "传播中",
    STATE_ACTIVE: "已生效",
    STATE_FAILED: "传播失败",
}

DB_SCHEMA_VERSION = "1"


class EngineError(RuntimeError):
    pass


class DatabaseConflict(EngineError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load_json(value: str) -> Any:
    return json.loads(value)


def project_fingerprint(project: Project) -> str:
    digest = hashlib.sha256()
    for relative_path, absolute_path in sorted(project.source_files.items()):
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        with Path(absolute_path).open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gate_state (
    gate_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    target_versions_json TEXT NOT NULL,
    active_versions_json TEXT NOT NULL,
    initial_versions_json TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error_code TEXT,
    last_error_retryable INTEGER,
    activated_batch INTEGER,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gate_transitions (
    transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
    gate_id TEXT NOT NULL,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    batch_no INTEGER NOT NULL,
    attempt_no INTEGER NOT NULL,
    reason TEXT NOT NULL,
    error_code TEXT,
    retryable INTEGER,
    active_versions_json TEXT NOT NULL,
    target_versions_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plan_batches (
    batch_no INTEGER PRIMARY KEY,
    gate_ids_json TEXT NOT NULL,
    cycle_gate_ids_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batch_runs (
    batch_no INTEGER PRIMARY KEY,
    base_gate_ids_json TEXT NOT NULL,
    retry_gate_ids_json TEXT NOT NULL,
    attempted_gate_ids_json TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS retry_queue (
    gate_id TEXT PRIMARY KEY,
    due_batch_no INTEGER NOT NULL,
    last_error_code TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gate_faults (
    gate_id TEXT NOT NULL,
    attempt_no INTEGER NOT NULL,
    error_code TEXT NOT NULL,
    retryable INTEGER NOT NULL,
    PRIMARY KEY (gate_id, attempt_no)
);

CREATE TABLE IF NOT EXISTS propagation_attempts (
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    gate_id TEXT NOT NULL,
    batch_no INTEGER NOT NULL,
    attempt_no INTEGER NOT NULL,
    delivered_versions_json TEXT NOT NULL,
    outcome TEXT NOT NULL,
    error_code TEXT,
    retryable INTEGER,
    occurred_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS version_snapshots (
    gate_id TEXT NOT NULL,
    batch_no INTEGER NOT NULL,
    versions_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (gate_id, batch_no)
);

CREATE TABLE IF NOT EXISTS verification_ledger (
    request_id TEXT PRIMARY KEY,
    gate_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    after_batch INTEGER,
    decision TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    rule_mode TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
"""


def connect_database(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    connection.executescript(SCHEMA_SQL)
    return connection


def reset_database(db_path: str | Path) -> None:
    path = Path(db_path).resolve()
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass


def _meta(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row["value"])


def _set_meta(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def prepare_database(
    project: Project,
    db_path: str | Path,
    *,
    reset: bool = False,
) -> PropagationPlan:
    if not project.valid:
        details = "; ".join(
            f"{issue.code}@{issue.path}" for issue in project.issues
        )
        raise EngineError(f"project validation failed: {details}")
    if reset:
        reset_database(db_path)

    plan = build_plan(project)
    fingerprint = project_fingerprint(project)
    connection = connect_database(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            stored_project_id = _meta(connection, "project_id")
            stored_graph_hash = _meta(connection, "graph_hash")
            stored_fingerprint = _meta(connection, "project_fingerprint")
            stored_plan_json = _meta(connection, "plan_json")
            if stored_project_id is not None:
                if (
                    stored_project_id != project.project_id
                    or stored_graph_hash != plan.graph_hash
                    or stored_fingerprint != fingerprint
                ):
                    raise DatabaseConflict(
                        "database belongs to a different project, rule snapshot, or plan; "
                        "pass --reset to create a new run"
                    )
            else:
                _set_meta(connection, "db_schema_version", DB_SCHEMA_VERSION)
                _set_meta(connection, "project_id", project.project_id)
                _set_meta(connection, "seed", str(project.seed))
                _set_meta(connection, "graph_hash", plan.graph_hash)
                _set_meta(connection, "project_fingerprint", fingerprint)
                _set_meta(connection, "plan_json", _json(plan.to_dict()))

            for batch in plan.batches:
                existing = connection.execute(
                    "SELECT * FROM plan_batches WHERE batch_no = ?",
                    (batch.batch_no,),
                ).fetchone()
                expected_gate_ids = _json(list(batch.gate_ids))
                expected_cycle_ids = _json(list(batch.cycle_gate_ids))
                if existing is None:
                    connection.execute(
                        "INSERT INTO plan_batches(batch_no, gate_ids_json, cycle_gate_ids_json) "
                        "VALUES(?, ?, ?)",
                        (batch.batch_no, expected_gate_ids, expected_cycle_ids),
                    )
                elif (
                    existing["gate_ids_json"] != expected_gate_ids
                    or existing["cycle_gate_ids_json"] != expected_cycle_ids
                ):
                    raise DatabaseConflict(
                        f"stored batch {batch.batch_no} differs from the current plan"
                    )

            initial_rows = list(connection.execute("SELECT gate_id FROM gate_state"))
            stored_gate_ids = {row["gate_id"] for row in initial_rows}
            if stored_project_id is None:
                stored_gate_ids = set()
            elif stored_gate_ids != plan.affected_set:
                raise DatabaseConflict(
                    "stored gate-state set does not match the affected gate set"
                )

            now = _utc_now()
            for gate_id in plan.affected_gate_ids:
                if gate_id in stored_gate_ids:
                    continue
                gate = project.gates[gate_id]
                target = project.target_versions_for_gate(gate)
                initial = project.baseline_versions_for_gate(gate)
                connection.execute(
                    "INSERT INTO gate_state("
                    "gate_id, state, target_versions_json, active_versions_json, "
                    "initial_versions_json, attempt_count, updated_at"
                    ") VALUES(?, ?, ?, ?, ?, 0, ?)",
                    (gate_id, STATE_PENDING, _json(target), _json(initial), _json(initial), now),
                )
                connection.execute(
                    "INSERT INTO gate_transitions("
                    "gate_id, from_state, to_state, batch_no, attempt_no, reason, "
                    "error_code, retryable, active_versions_json, target_versions_json, occurred_at"
                    ") VALUES(?, 'new', ?, -1, 0, 'prepared', NULL, NULL, ?, ?, ?)",
                    (gate_id, STATE_PENDING, _json(initial), _json(target), now),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO version_snapshots("
                    "gate_id, batch_no, versions_json, recorded_at"
                    ") VALUES(?, -1, ?, ?)",
                    (gate_id, _json(initial), now),
                )

                faults = project.faults.get(gate_id, ())
                for fault in faults:
                    connection.execute(
                        "INSERT OR IGNORE INTO gate_faults("
                        "gate_id, attempt_no, error_code, retryable"
                        ") VALUES(?, ?, ?, ?)",
                        (
                            gate_id,
                            fault.attempt,
                            fault.error_code,
                            1 if fault.retryable else 0,
                        ),
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    finally:
        connection.close()
    return plan


def _active_versions_for_event(
    project: Project, gate: Gate, event_id: str, versions: dict[str, str]
) -> str:
    if event_id not in gate.subscriptions:
        raise EngineError(f"gate {gate.gate_id} does not subscribe event {event_id}")
    version = versions.get(event_id)
    if version is None or version not in project.events[event_id].versions:
        raise EngineError(
            f"gate {gate.gate_id} has no valid {event_id} rule version in {versions}"
        )
    return version


def _fault_for_attempt(
    connection: sqlite3.Connection, gate_id: str, attempt_no: int
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT error_code, retryable FROM gate_faults "
        "WHERE gate_id = ? AND attempt_no = ?",
        (gate_id, attempt_no),
    ).fetchone()


def _record_transition(
    connection: sqlite3.Connection,
    *,
    gate_id: str,
    from_state: str,
    to_state: str,
    batch_no: int,
    attempt_no: int,
    reason: str,
    error_code: str | None,
    retryable: bool | None,
    active_versions: dict[str, str],
    target_versions: dict[str, str],
    occurred_at: str,
) -> None:
    connection.execute(
        "INSERT INTO gate_transitions("
        "gate_id, from_state, to_state, batch_no, attempt_no, reason, "
        "error_code, retryable, active_versions_json, target_versions_json, occurred_at"
        ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            gate_id,
            from_state,
            to_state,
            batch_no,
            attempt_no,
            reason,
            error_code,
            None if retryable is None else (1 if retryable else 0),
            _json(active_versions),
            _json(target_versions),
            occurred_at,
        ),
    )


def _run_batch(
    connection: sqlite3.Connection,
    batch_no: int,
    base_gate_ids: list[str],
) -> set[str]:
    retry_rows = list(
        connection.execute(
            "SELECT gate_id FROM retry_queue WHERE due_batch_no <= ? ORDER BY gate_id",
            (batch_no,),
        )
    )
    retry_gate_ids = [row["gate_id"] for row in retry_rows]
    gate_ids = sorted(set(base_gate_ids).union(retry_gate_ids))
    retry_set = set(retry_gate_ids)
    attempted: list[str] = []
    started_at = _utc_now()

    connection.execute("BEGIN IMMEDIATE")
    try:
        for gate_id in gate_ids:
            gate_row = connection.execute(
                "SELECT * FROM gate_state WHERE gate_id = ?", (gate_id,)
            ).fetchone()
            if gate_row is None:
                continue
            state = gate_row["state"]
            if state not in {STATE_PENDING, STATE_FAILED}:
                continue
            if state == STATE_FAILED:
                retry_row = connection.execute(
                    "SELECT * FROM retry_queue WHERE gate_id = ?", (gate_id,)
                ).fetchone()
                if retry_row is None:
                    continue

            target_versions = _load_json(gate_row["target_versions_json"])
            active_versions = _load_json(gate_row["active_versions_json"])
            attempt_no = int(gate_row["attempt_count"]) + 1
            occurred_at = _utc_now()
            attempted.append(gate_id)

            _record_transition(
                connection,
                gate_id=gate_id,
                from_state=state,
                to_state=STATE_PROPAGATING,
                batch_no=batch_no,
                attempt_no=attempt_no,
                reason="retry_started" if state == STATE_FAILED else "delivery_started",
                error_code=None,
                retryable=None,
                active_versions=active_versions,
                target_versions=target_versions,
                occurred_at=occurred_at,
            )
            connection.execute(
                "UPDATE gate_state SET state = ?, attempt_count = ?, updated_at = ? "
                "WHERE gate_id = ?",
                (STATE_PROPAGATING, attempt_no, occurred_at, gate_id),
            )

            fault = _fault_for_attempt(connection, gate_id, attempt_no)
            if fault is None:
                connection.execute(
                    "INSERT INTO propagation_attempts("
                    "gate_id, batch_no, attempt_no, delivered_versions_json, "
                    "outcome, error_code, retryable, occurred_at"
                    ") VALUES(?, ?, ?, ?, 'success', NULL, NULL, ?)",
                    (gate_id, batch_no, attempt_no, _json(target_versions), occurred_at),
                )
                connection.execute(
                    "UPDATE gate_state SET state = ?, active_versions_json = ?, "
                    "last_error_code = NULL, last_error_retryable = NULL, "
                    "activated_batch = ?, updated_at = ? WHERE gate_id = ?",
                    (
                        STATE_ACTIVE,
                        _json(target_versions),
                        batch_no,
                        occurred_at,
                        gate_id,
                    ),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO version_snapshots("
                    "gate_id, batch_no, versions_json, recorded_at"
                    ") VALUES(?, ?, ?, ?)",
                    (gate_id, batch_no, _json(target_versions), occurred_at),
                )
                _record_transition(
                    connection,
                    gate_id=gate_id,
                    from_state=STATE_PROPAGATING,
                    to_state=STATE_ACTIVE,
                    batch_no=batch_no,
                    attempt_no=attempt_no,
                    reason="target_version_activated",
                    error_code=None,
                    retryable=None,
                    active_versions=target_versions,
                    target_versions=target_versions,
                    occurred_at=occurred_at,
                )
                connection.execute("DELETE FROM retry_queue WHERE gate_id = ?", (gate_id,))
            else:
                error_code = str(fault["error_code"])
                retryable = bool(fault["retryable"])
                connection.execute(
                    "INSERT INTO propagation_attempts("
                    "gate_id, batch_no, attempt_no, delivered_versions_json, "
                    "outcome, error_code, retryable, occurred_at"
                    ") VALUES(?, ?, ?, ?, 'failed', ?, ?, ?)",
                    (
                        gate_id,
                        batch_no,
                        attempt_no,
                        _json(target_versions),
                        error_code,
                        1 if retryable else 0,
                        occurred_at,
                    ),
                )
                connection.execute(
                    "UPDATE gate_state SET state = ?, last_error_code = ?, "
                    "last_error_retryable = ?, updated_at = ? WHERE gate_id = ?",
                    (
                        STATE_FAILED,
                        error_code,
                        1 if retryable else 0,
                        occurred_at,
                        gate_id,
                    ),
                )
                if retryable:
                    connection.execute(
                        "INSERT INTO retry_queue(gate_id, due_batch_no, last_error_code) "
                        "VALUES(?, ?, ?) "
                        "ON CONFLICT(gate_id) DO UPDATE SET "
                        "due_batch_no = excluded.due_batch_no, "
                        "last_error_code = excluded.last_error_code",
                        (gate_id, batch_no + 1, error_code),
                    )
                else:
                    connection.execute("DELETE FROM retry_queue WHERE gate_id = ?", (gate_id,))
                _record_transition(
                    connection,
                    gate_id=gate_id,
                    from_state=STATE_PROPAGATING,
                    to_state=STATE_FAILED,
                    batch_no=batch_no,
                    attempt_no=attempt_no,
                    reason="retryable_delivery_failure" if retryable else "terminal_delivery_failure",
                    error_code=error_code,
                    retryable=retryable,
                    active_versions=active_versions,
                    target_versions=target_versions,
                    occurred_at=occurred_at,
                )

        completed_at = _utc_now()
        connection.execute(
            "INSERT INTO batch_runs("
            "batch_no, base_gate_ids_json, retry_gate_ids_json, "
            "attempted_gate_ids_json, status, started_at, completed_at"
            ") VALUES(?, ?, ?, ?, 'completed', ?, ?)",
            (
                batch_no,
                _json(sorted(base_gate_ids)),
                _json(sorted(retry_set)),
                _json(attempted),
                started_at,
                completed_at,
            ),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return retry_set


def propagate(
    project: Project,
    db_path: str | Path,
    *,
    crash_after_batch: int | None = None,
) -> dict[str, Any]:
    plan = prepare_database(project, db_path)
    connection = connect_database(db_path)
    processed_batches: list[int] = []
    try:
        base_batches = list(
            connection.execute("SELECT * FROM plan_batches ORDER BY batch_no")
        )
        for row in base_batches:
            batch_no = int(row["batch_no"])
            completed = connection.execute(
                "SELECT 1 FROM batch_runs WHERE batch_no = ?", (batch_no,)
            ).fetchone()
            if completed is not None:
                continue
            base_gate_ids = _load_json(row["gate_ids_json"])
            _run_batch(connection, batch_no, base_gate_ids)
            processed_batches.append(batch_no)
            if crash_after_batch == batch_no:
                os._exit(75)

        max_batch_row = connection.execute(
            "SELECT MAX(batch_no) AS max_batch FROM batch_runs"
        ).fetchone()
        next_batch_no = int(max_batch_row["max_batch"] or -1) + 1
        while connection.execute("SELECT 1 FROM retry_queue LIMIT 1").fetchone() is not None:
            _run_batch(connection, next_batch_no, [])
            processed_batches.append(next_batch_no)
            if crash_after_batch == next_batch_no:
                os._exit(75)
            next_batch_no += 1
    finally:
        connection.close()
    return {
        "project_id": project.project_id,
        "processed_batches": processed_batches,
        "complete": is_propagation_complete(project, db_path),
    }


def is_propagation_complete(project: Project, db_path: str | Path) -> bool:
    connection = connect_database(db_path)
    try:
        base_numbers = [
            int(row["batch_no"])
            for row in connection.execute("SELECT batch_no FROM plan_batches ORDER BY batch_no")
        ]
        completed_numbers = {
            int(row["batch_no"])
            for row in connection.execute(
                "SELECT batch_no FROM batch_runs WHERE status = 'completed'"
            )
        }
        retry_pending = connection.execute(
            "SELECT 1 FROM retry_queue LIMIT 1"
        ).fetchone()
        return all(number in completed_numbers for number in base_numbers) and retry_pending is None
    finally:
        connection.close()


def _state_at_snapshot(
    connection: sqlite3.Connection,
    gate_id: str,
    after_batch: int | None,
) -> tuple[str, dict[str, str], int | None]:
    if after_batch is None:
        row = connection.execute(
            "SELECT * FROM gate_state WHERE gate_id = ?", (gate_id,)
        ).fetchone()
        if row is None:
            raise EngineError(f"gate {gate_id} is not part of this propagation run")
        return (
            str(row["state"]),
            _load_json(row["active_versions_json"]),
            row["activated_batch"],
        )

    snapshot = connection.execute(
        "SELECT versions_json FROM version_snapshots "
        "WHERE gate_id = ? AND batch_no <= ? ORDER BY batch_no DESC LIMIT 1",
        (gate_id, after_batch),
    ).fetchone()
    if snapshot is None:
        raise EngineError(f"no version snapshot for gate {gate_id} at batch {after_batch}")
    versions = _load_json(snapshot["versions_json"])
    transition = connection.execute(
        "SELECT to_state, batch_no FROM gate_transitions "
        "WHERE gate_id = ? AND batch_no <= ? ORDER BY transition_id DESC LIMIT 1",
        (gate_id, after_batch),
    ).fetchone()
    state = str(transition["to_state"]) if transition is not None else STATE_PENDING
    activated = connection.execute(
        "SELECT MAX(batch_no) AS activated_batch FROM version_snapshots "
        "WHERE gate_id = ? AND batch_no >= 0 AND batch_no <= ?",
        (gate_id, after_batch),
    ).fetchone()
    activated_batch = (
        int(activated["activated_batch"])
        if activated is not None and activated["activated_batch"] is not None
        else None
    )
    return state, versions, activated_batch


def verify_requests(
    project: Project,
    db_path: str | Path,
    *,
    allow_incomplete: bool = False,
) -> list[dict[str, Any]]:
    if not allow_incomplete and not is_propagation_complete(project, db_path):
        raise EngineError(
            "propagation is incomplete; finish or resume it before writing the final ledger"
        )
    connection = connect_database(db_path)
    decisions: list[dict[str, Any]] = []
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            for request in project.requests:
                gate = project.gates[request.gate_id]
                gate_row = connection.execute(
                    "SELECT * FROM gate_state WHERE gate_id = ?",
                    (request.gate_id,),
                ).fetchone()
                if gate_row is None:
                    raise EngineError(
                        f"verification request {request.request_id} targets gate "
                        f"{request.gate_id}, which is not affected"
                    )
                state, effective_versions, _ = _state_at_snapshot(
                    connection,
                    request.gate_id,
                    request.after_batch,
                )
                target_versions = _load_json(gate_row["target_versions_json"])
                version = _active_versions_for_event(
                    project, gate, request.event_id, effective_versions
                )
                rule = project.rule(request.event_id, version)
                decision = build_decision_record(
                    request,
                    rule,
                    state_at_snapshot=state,
                    effective_versions=effective_versions,
                    target_versions=target_versions,
                    snapshot_after_batch=request.after_batch,
                )
                decisions.append(decision)
                connection.execute(
                    "INSERT INTO verification_ledger("
                    "request_id, gate_id, event_id, after_batch, decision, "
                    "reason_code, rule_mode, rule_version, decision_json, recorded_at"
                    ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(request_id) DO UPDATE SET "
                    "gate_id = excluded.gate_id, event_id = excluded.event_id, "
                    "after_batch = excluded.after_batch, decision = excluded.decision, "
                    "reason_code = excluded.reason_code, rule_mode = excluded.rule_mode, "
                    "rule_version = excluded.rule_version, "
                    "decision_json = excluded.decision_json, "
                    "recorded_at = excluded.recorded_at",
                    (
                        request.request_id,
                        request.gate_id,
                        request.event_id,
                        request.after_batch,
                        decision["decision"],
                        decision["reason_code"],
                        decision["rule_mode"],
                        decision["rule_version"],
                        _json(decision),
                        _utc_now(),
                    ),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    finally:
        connection.close()
    return decisions


def decision_stats(decisions: Iterable[dict[str, Any]]) -> dict[str, Any]:
    decision_rows = list(decisions)
    by_result: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    by_rule_mode: dict[str, int] = {}
    for decision in decision_rows:
        result = str(decision["decision"])
        reason = str(decision["reason_code"])
        mode = str(decision["rule_mode"])
        by_result[result] = by_result.get(result, 0) + 1
        by_reason[reason] = by_reason.get(reason, 0) + 1
        by_rule_mode[mode] = by_rule_mode.get(mode, 0) + 1
    return {
        "total": len(decision_rows),
        "by_result": dict(sorted(by_result.items())),
        "by_reason": dict(sorted(by_reason.items())),
        "by_rule_mode": dict(sorted(by_rule_mode.items())),
        "degraded_decisions": by_rule_mode.get("degraded", 0),
    }


def _read_decisions(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        _load_json(row["decision_json"])
        for row in connection.execute(
            "SELECT decision_json FROM verification_ledger ORDER BY request_id"
        )
    ]


def _load_gate_states(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = list(connection.execute("SELECT * FROM gate_state ORDER BY gate_id"))
    result: list[dict[str, Any]] = []
    for row in rows:
        target = _load_json(row["target_versions_json"])
        active = _load_json(row["active_versions_json"])
        last_error = None
        if row["last_error_code"] is not None:
            last_error = {
                "error_code": row["last_error_code"],
                "retryable": bool(row["last_error_retryable"]),
            }
        result.append(
            {
                "gate_id": row["gate_id"],
                "state": row["state"],
                "state_label": STATE_LABELS.get(row["state"], row["state"]),
                "target_versions": dict(sorted(target.items())),
                "active_versions": dict(sorted(active.items())),
                "activated_batch": row["activated_batch"],
                "attempt_count": row["attempt_count"],
                "last_error": last_error,
                "target_acknowledged": row["state"] == STATE_ACTIVE and active == target,
                "degraded": active != target,
            }
        )
    return result


def _degradation_entry(
    project: Project, gate_state: dict[str, Any]
) -> dict[str, Any] | None:
    active = gate_state["active_versions"]
    target = gate_state["target_versions"]
    if active == target:
        return None
    event_ids = sorted(set(active).union(target))
    event_differences: list[dict[str, Any]] = []
    for event_id in event_ids:
        active_version = active.get(event_id)
        target_version = target.get(event_id)
        if active_version == target_version:
            continue
        fields: list[dict[str, Any]] = []
        if active_version is not None and target_version is not None:
            active_rule = project.rule(event_id, active_version)
            target_rule = project.rule(event_id, target_version)
            active_fields = active_rule.comparable_fields()
            target_fields = target_rule.comparable_fields()
            for field_name in sorted(active_fields):
                if active_fields[field_name] == target_fields[field_name]:
                    continue
                fields.append(
                    {
                        "field": field_name,
                        "active_value": active_fields[field_name],
                        "target_value": target_fields[field_name],
                        "active_source": active_rule.sources[field_name],
                        "target_source": target_rule.sources[field_name],
                    }
                )
        event_differences.append(
            {
                "event_id": event_id,
                "active_version": active_version,
                "target_version": target_version,
                "fields": fields,
            }
        )

    last_error = gate_state["last_error"]
    if gate_state["state"] == STATE_FAILED and last_error and not last_error["retryable"]:
        reason = "nonretryable_failure"
    elif gate_state["state"] == STATE_FAILED:
        reason = "retry_pending"
    else:
        reason = "not_activated"
    return {
        "gate_id": gate_state["gate_id"],
        "state": gate_state["state"],
        "state_label": gate_state["state_label"],
        "reason": reason,
        "frozen_versions": dict(sorted(active.items())),
        "latest_versions": dict(sorted(target.items())),
        "event_differences": event_differences,
    }


def _batch_reports(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    runs = list(connection.execute("SELECT * FROM batch_runs ORDER BY batch_no"))
    attempts_by_batch: dict[int, list[dict[str, Any]]] = {}
    for row in connection.execute(
        "SELECT * FROM propagation_attempts ORDER BY batch_no, gate_id, attempt_no"
    ):
        attempts_by_batch.setdefault(int(row["batch_no"]), []).append(
            {
                "gate_id": row["gate_id"],
                "attempt_no": row["attempt_no"],
                "delivered_versions": _load_json(row["delivered_versions_json"]),
                "outcome": row["outcome"],
                "error_code": row["error_code"],
                "retryable": (
                    None if row["retryable"] is None else bool(row["retryable"])
                ),
                "occurred_at": row["occurred_at"],
            }
        )
    result = []
    for row in runs:
        batch_no = int(row["batch_no"])
        result.append(
            {
                "batch_no": batch_no,
                "base_gate_ids": _load_json(row["base_gate_ids_json"]),
                "retry_gate_ids": _load_json(row["retry_gate_ids_json"]),
                "attempted_gate_ids": _load_json(row["attempted_gate_ids_json"]),
                "status": row["status"],
                "started_at": row["started_at"],
                "completed_at": row["completed_at"],
                "attempts": attempts_by_batch.get(batch_no, []),
            }
        )
    return result


def build_report(project: Project, db_path: str | Path) -> dict[str, Any]:
    if not is_propagation_complete(project, db_path):
        raise EngineError("cannot build final report before propagation completes")
    plan = build_plan(project)
    connection = connect_database(db_path)
    try:
        decisions = _read_decisions(connection)
        stats = decision_stats(decisions)
        gate_states = _load_gate_states(connection)
        degradation_entries = [
            entry
            for entry in (
                _degradation_entry(project, gate_state)
                for gate_state in gate_states
            )
            if entry is not None
        ]
        transitions = [
            {
                "transition_id": row["transition_id"],
                "gate_id": row["gate_id"],
                "from_state": row["from_state"],
                "to_state": row["to_state"],
                "batch_no": row["batch_no"],
                "attempt_no": row["attempt_no"],
                "reason": row["reason"],
                "error_code": row["error_code"],
                "retryable": (
                    None if row["retryable"] is None else bool(row["retryable"])
                ),
                "active_versions": _load_json(row["active_versions_json"]),
                "target_versions": _load_json(row["target_versions_json"]),
                "occurred_at": row["occurred_at"],
            }
            for row in connection.execute(
                "SELECT * FROM gate_transitions ORDER BY transition_id"
            )
        ]
        batch_reports = _batch_reports(connection)
        stored_plan = _load_json(_meta(connection, "plan_json") or "{}")
        accounted_gate_ids = [gate_state["gate_id"] for gate_state in gate_states]
        transition_gate_ids = sorted({row["gate_id"] for row in transitions})
        decision_gate_ids = sorted({decision["gate_id"] for decision in decisions})

        expected_plan_batch_numbers = [batch.batch_no for batch in plan.batches]
        run_batch_numbers = [batch["batch_no"] for batch in batch_reports]
        invariants = {
            "accounted_gate_set_equals_affected": (
                accounted_gate_ids == list(plan.affected_gate_ids)
            ),
            "transition_gate_set_equals_affected": (
                transition_gate_ids == list(plan.affected_gate_ids)
            ),
            "decision_gate_set_is_subset_of_affected": set(decision_gate_ids).issubset(
                plan.affected_set
            ),
            "all_base_batches_have_completed_runs": all(
                number in run_batch_numbers for number in expected_plan_batch_numbers
            ),
            "retry_queue_is_empty": connection.execute(
                "SELECT 1 FROM retry_queue LIMIT 1"
            ).fetchone()
            is None,
            "all_active_version_keys_match_targets": all(
                set(gate_state["active_versions"]) == set(gate_state["target_versions"])
                for gate_state in gate_states
            ),
            "every_decision_has_rule_source": all(
                decision.get("trace", {}).get("decisive") is not None
                or decision.get("decision") == "ALLOW"
                for decision in decisions
            ),
        }

        summary = {
            "project_id": project.project_id,
            "seed": project.seed,
            "graph_hash": plan.graph_hash,
            "affected_gate_ids": list(plan.affected_gate_ids),
            "accounted_gate_ids": accounted_gate_ids,
            "propagation_plan": {
                **stored_plan,
                "batches": [
                    {
                        **batch.to_dict(),
                        "completed": batch.batch_no
                        in {
                            report["batch_no"]
                            for report in batch_reports
                            if report["status"] == "completed"
                        },
                    }
                    for batch in plan.batches
                ],
            },
            "execution_batches": batch_reports,
            "gate_versions": gate_states,
            "degradation": {
                "gates": degradation_entries,
                "gate_ids": [entry["gate_id"] for entry in degradation_entries],
            },
            "verification": {
                "decisions": decisions,
                "stats": stats,
            },
            "audit": {
                "transitions": transitions,
                "attempts": [
                    attempt
                    for batch in batch_reports
                    for attempt in batch["attempts"]
                ],
            },
            "invariants": invariants,
        }
        return summary
    finally:
        connection.close()


def write_report_files(report: dict[str, Any], output_dir: str | Path) -> None:
    root = Path(output_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    plan_document = {
        "project_id": report["project_id"],
        "affected_gate_ids": report["affected_gate_ids"],
        **report["propagation_plan"],
        "execution_batches": report["execution_batches"],
    }
    gate_document = {
        "project_id": report["project_id"],
        "affected_gate_ids": report["affected_gate_ids"],
        "gates": report["gate_versions"],
    }
    degradation_document = {
        "project_id": report["project_id"],
        "affected_gate_ids": report["affected_gate_ids"],
        **report["degradation"],
    }
    audit_document = {
        "project_id": report["project_id"],
        "affected_gate_ids": report["affected_gate_ids"],
        **report["audit"],
    }
    documents = {
        "summary.json": report,
        "plan.json": plan_document,
        "gate_versions.json": gate_document,
        "degradation.json": degradation_document,
        "decision_stats.json": report["verification"]["stats"],
        "audit.json": audit_document,
    }
    for filename, document in documents.items():
        path = root / filename
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    with (root / "decisions.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for decision in report["verification"]["decisions"]:
            handle.write(_json(decision))
            handle.write("\n")
