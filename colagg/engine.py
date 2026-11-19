"""Streaming execution, recovery, plan replacement, and verification."""

from __future__ import annotations

import copy
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .canonical import atomic_write_text, canonical_json, pretty_json, sha256_value
from .compute import (
    apply_aggregate_batch,
    apply_window_batch,
    empty_aggregate_state,
    empty_window_state,
    finalize_group_record,
    finalize_window_state,
)
from .errors import (
    ColAggError,
    E_BAD_BATCH,
    E_BATCH_DIGEST_MISMATCH,
    E_INCOMPLETE,
    E_NO_STATE,
    E_PLAN_CHANGED,
    E_PLAN_CONFLICT,
    E_PLAN_ROLLBACK,
    E_STATE_STALE,
    E_TYPE_MISMATCH,
    E_VERIFY_FAILED,
)
from .schema import (
    Batch,
    Column,
    OperatorSpec,
    PlanDefinition,
    Precision,
)
from .storage import (
    FileLock,
    database_lock_path,
    immediate_transaction,
    max_batch_clock,
    open_database,
    read_transaction,
)


def _row_to_batch(row: sqlite3.Row) -> Batch:
    payload = json.loads(row["payload_json"])
    columns = tuple(
        Column(
            name=item["name"],
            kind=item["kind"],
            values=tuple(item["values"]),
        )
        for item in payload["columns"]
    )
    return Batch(
        seq=int(payload["seq"]),
        row_count=int(payload["row_count"]),
        columns=columns,
        digest=row["digest"],
        provided_digest=row["provided_digest"],
    )


def _state_digest(state: dict[str, Any]) -> str:
    return sha256_value(state)


def _new_operator_state(
    operator: OperatorSpec,
    precision: Precision,
    plan_version: int,
    clock: int,
) -> dict[str, Any]:
    if operator.kind == "aggregate":
        return empty_aggregate_state(operator)
    return empty_window_state(operator)


def _operator_status_payload(
    operator: OperatorSpec,
    state: dict[str, Any] | None,
    precision: Precision,
    *,
    plan_version: int,
    committed_batches: int,
) -> dict[str, Any]:
    if state is None:
        return {
            "operator_id": operator.operator_id,
            "name": operator.name,
            "kind": operator.kind,
            "op": operator.op,
            "column": operator.column,
            "group_by": list(operator.group_by),
            "plan_version": plan_version,
            "watermark_seq": -1,
            "committed_batches": committed_batches,
            "rows_seen": 0,
            "accumulator": precision.accumulator,
            "primary_path": precision.accumulator,
            "status": "not_started",
            "finalized": False,
            "failure": None,
        }
    return {
        "operator_id": operator.operator_id,
        "name": operator.name,
        "kind": operator.kind,
        "op": operator.op,
        "column": operator.column,
        "group_by": list(operator.group_by),
        "plan_version": int(state["plan_version"]),
        "watermark_seq": int(state["watermark_seq"]),
        "committed_batches": committed_batches,
        "rows_seen": int(state["rows_seen"]),
        "accumulator": precision.accumulator,
        "primary_path": precision.accumulator,
        "status": state["status"],
        "finalized": bool(state["finalized"]),
        "state_digest": state["state_digest"],
        "failure": (
            {
                "code": state["failure_code"],
                "message": state["failure_message"],
                "batch_seq": state["failure_batch_seq"],
            }
            if state["failure_code"]
            else None
        ),
    }


class Engine:
    def __init__(self, database_path: Path, output_dir: Path) -> None:
        self.database_path = database_path.resolve()
        self.output_dir = output_dir.resolve()
        self.lock_path = database_lock_path(self.database_path)

    def _connect(self, *, create: bool = False) -> sqlite3.Connection:
        return open_database(self.database_path, create=create)

    def _fetch_plan(self, connection: sqlite3.Connection) -> PlanDefinition:
        row = connection.execute(
            """
            SELECT p.normalized_json
            FROM current_plan c
            JOIN plans p ON p.version = c.version
            WHERE c.singleton = 1
            """
        ).fetchone()
        if row is None:
            raise ColAggError(E_NO_STATE, "no current execution plan")
        return PlanDefinition.from_normalized_dict(json.loads(row["normalized_json"]))

    def _fetch_batches(
        self,
        connection: sqlite3.Connection,
        *,
        statuses: Iterable[str] | None = None,
    ) -> list[Batch]:
        if statuses is None:
            rows = connection.execute("SELECT * FROM batches ORDER BY seq").fetchall()
        else:
            status_list = tuple(statuses)
            placeholders = ",".join("?" for _ in status_list)
            rows = connection.execute(
                f"SELECT * FROM batches WHERE status IN ({placeholders}) ORDER BY seq",
                status_list,
            ).fetchall()
        return [_row_to_batch(row) for row in rows]

    def _fetch_batch(self, connection: sqlite3.Connection, seq: int) -> Batch | None:
        row = connection.execute("SELECT * FROM batches WHERE seq = ?", (seq,)).fetchone()
        return None if row is None else _row_to_batch(row)

    def _next_clock(self, connection: sqlite3.Connection, precision: Precision) -> int:
        maximum = max_batch_clock(connection)
        row = connection.execute("SELECT COUNT(*) AS count FROM batches WHERE clock IS NOT NULL").fetchone()
        if int(row["count"]) == 0:
            return precision.clock_start
        return maximum + precision.clock_step

    def _specs_for_version(
        self,
        connection: sqlite3.Connection,
        version: int,
    ) -> dict[str, OperatorSpec]:
        rows = connection.execute(
            "SELECT spec_json FROM plan_ops WHERE version = ? ORDER BY operator_id",
            (version,),
        ).fetchall()
        result: dict[str, OperatorSpec] = {}
        for row in rows:
            spec = json.loads(row["spec_json"])
            operator = OperatorSpec(
                kind=spec["kind"],
                name=spec["name"],
                op=spec["op"],
                column=spec.get("column"),
                group_by=tuple(spec["group_by"]),
                precision_signature=spec["precision"],
                window=spec.get("window"),
                operator_id=spec["operator_id"],
            )
            result[operator.operator_id] = operator
        return result

    def _insert_state_row(
        self,
        connection: sqlite3.Connection,
        operator: OperatorSpec,
        precision: Precision,
        plan_version: int,
        clock: int,
    ) -> None:
        state = (
            empty_aggregate_state(operator)
            if operator.kind == "aggregate"
            else empty_window_state(operator)
        )
        self._write_state(
            connection,
            operator,
            precision,
            plan_version,
            state,
            watermark_seq=-1,
            clock=clock,
            status="ready",
            failure=None,
            finalized=False,
        )

    def _write_state(
        self,
        connection: sqlite3.Connection,
        operator: OperatorSpec,
        precision: Precision,
        plan_version: int,
        state: dict[str, Any],
        *,
        watermark_seq: int,
        clock: int,
        status: str,
        failure: ColAggError | None,
        failure_batch_seq: int | None = None,
        finalized: bool = False,
    ) -> None:
        digest = _state_digest(state)
        connection.execute(
            """
            INSERT INTO operator_state (
                operator_id, name, kind, plan_version, watermark_seq, rows_seen,
                primary_path, status, failure_code, failure_message,
                failure_batch_seq, state_json, state_digest, finalized, updated_clock
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(operator_id) DO UPDATE SET
                name = excluded.name,
                kind = excluded.kind,
                plan_version = excluded.plan_version,
                watermark_seq = excluded.watermark_seq,
                rows_seen = excluded.rows_seen,
                primary_path = excluded.primary_path,
                status = excluded.status,
                failure_code = excluded.failure_code,
                failure_message = excluded.failure_message,
                failure_batch_seq = excluded.failure_batch_seq,
                state_json = excluded.state_json,
                state_digest = excluded.state_digest,
                finalized = excluded.finalized,
                updated_clock = excluded.updated_clock
            """,
            (
                operator.operator_id,
                operator.name,
                operator.kind,
                plan_version,
                watermark_seq,
                int(state.get("rows_seen", 0)),
                precision.accumulator,
                status,
                failure.code if failure else None,
                failure.message if failure else None,
                failure_batch_seq,
                canonical_json(state),
                digest,
                int(finalized),
                clock,
            ),
        )

    def _record_failure(
        self,
        connection: sqlite3.Connection,
        *,
        scope: str,
        batch_seq: int | None,
        operator_id: str,
        error: ColAggError,
        clock: int,
    ) -> None:
        connection.execute(
            """
            INSERT INTO failures(scope, batch_seq, operator_id, code, message, details_json, clock)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(operator_id, batch_seq, code) DO UPDATE SET
                message = excluded.message,
                details_json = excluded.details_json,
                clock = excluded.clock
            """,
            (
                scope,
                batch_seq,
                operator_id,
                error.code,
                error.message,
                canonical_json(error.details),
                clock,
            ),
        )

    def _reset_operator(self, connection: sqlite3.Connection, operator_id: str) -> None:
        for table in (
            "operator_state",
            "applied",
            "batch_outputs",
            "group_results",
            "window_results",
            "conversion_traces",
            "failures",
        ):
            if table == "operator_state":
                connection.execute(
                    f"DELETE FROM {table} WHERE operator_id = ?", (operator_id,)
                )
            elif table == "failures":
                connection.execute(
                    f"DELETE FROM {table} WHERE operator_id = ?", (operator_id,)
                )
            else:
                connection.execute(
                    f"DELETE FROM {table} WHERE operator_id = ?", (operator_id,)
                )

    def load(
        self,
        batches: list[Batch],
        plan: PlanDefinition,
        *,
        clock: int | None = None,
    ) -> dict[str, Any]:
        created = not self.database_path.exists()
        try:
            with FileLock(self.lock_path):
                connection = self._connect(create=True)
                try:
                    with immediate_transaction(connection):
                        result = self._load_transaction(connection, batches, plan, clock)
                finally:
                    connection.close()
            return result
        except BaseException:
            if created:
                for suffix in ("", "-wal", "-shm"):
                    candidate = Path(str(self.database_path) + suffix)
                    try:
                        candidate.unlink()
                    except FileNotFoundError:
                        pass
                try:
                    self.lock_path.unlink()
                except FileNotFoundError:
                    pass
            raise

    def _load_transaction(
        self,
        connection: sqlite3.Connection,
        batches: list[Batch],
        plan: PlanDefinition,
        clock: int | None,
    ) -> dict[str, Any]:
        current = connection.execute(
            "SELECT version, digest FROM current_plan WHERE singleton = 1"
        ).fetchone()
        now = max(
            max_batch_clock(connection),
            plan.precision.clock_start,
            clock if clock is not None else plan.precision.clock_start,
        )
        reused: list[str] = []
        rebuilt: list[str] = []
        inserted_batches: list[int] = []
        old_version: int | None = None
        old_digest: str | None = None
        changed_plan = False

        if current is None:
            classification = "initial"
            old_ops: dict[str, OperatorSpec] = {}
            changed_plan = True
        else:
            old_version = int(current["version"])
            old_digest = str(current["digest"])
            old_ops = self._specs_for_version(connection, old_version)
            new_ids = {item.operator_id for item in plan.operators}
            old_ids = set(old_ops)
            reused = sorted(old_ids & new_ids)
            rebuilt = sorted(new_ids - old_ids)
            if plan.version < old_version:
                raise ColAggError(
                    E_PLAN_ROLLBACK,
                    f"plan rollback is forbidden: {plan.version} < {old_version}",
                    details={"current_version": old_version, "incoming_version": plan.version},
                )
            if plan.version == old_version:
                if plan.digest != old_digest:
                    raise ColAggError(
                        E_PLAN_CONFLICT,
                        "the same plan version has a different content digest",
                        details={
                            "version": plan.version,
                            "current_digest": old_digest,
                            "incoming_digest": plan.digest,
                        },
                    )
                classification = "unchanged"
            else:
                changed_plan = True
                if old_ids.issubset(new_ids):
                    classification = "incremental_append"
                else:
                    classification = "full_replace"

        existing_rows = {
            int(row["seq"]): row
            for row in connection.execute("SELECT * FROM batches").fetchall()
        }
        existing_max_seq = max(existing_rows) if existing_rows else -1
        for batch in batches:
            existing = existing_rows.get(batch.seq)
            if existing is not None:
                if existing["digest"] != batch.digest:
                    raise ColAggError(
                        E_BATCH_DIGEST_MISMATCH,
                        f"batch {batch.seq} differs from the persisted batch",
                        details={
                            "seq": batch.seq,
                            "persisted_digest": existing["digest"],
                            "incoming_digest": batch.digest,
                        },
                    )
                continue
            if batch.seq <= existing_max_seq:
                raise ColAggError(
                    E_BAD_BATCH,
                    "incremental batches must have seq greater than every persisted batch",
                    details={
                        "seq": batch.seq,
                        "persisted_max_seq": existing_max_seq,
                    },
                )
            inserted_batches.append(batch.seq)
            connection.execute(
                """
                INSERT INTO batches(
                    seq, digest, provided_digest, row_count, columns_json,
                    payload_json, status, clock, loaded_clock
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL, ?)
                """,
                (
                    batch.seq,
                    batch.digest,
                    batch.provided_digest,
                    batch.row_count,
                    canonical_json([column.as_dict() for column in batch.columns]),
                    batch.payload_json(),
                    now,
                ),
            )

        if changed_plan:
            normalized = plan.normalized_dict()
            connection.execute(
                """
                INSERT OR REPLACE INTO plans(
                    version, digest, query_json, precision_json, normalized_json,
                    classification, created_clock
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan.version,
                    plan.digest,
                    canonical_json(normalized),
                    canonical_json(plan.precision.as_dict()),
                    canonical_json(normalized),
                    classification,
                    now,
                ),
            )
            for operator in plan.operators:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO plan_ops(
                        version, operator_id, kind, name, spec_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        plan.version,
                        operator.operator_id,
                        operator.kind,
                        operator.name,
                        canonical_json(operator.as_dict()),
                    ),
                )
            connection.execute(
                """
                INSERT INTO current_plan(singleton, version, digest)
                VALUES (1, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    version = excluded.version,
                    digest = excluded.digest
                """,
                (plan.version, plan.digest),
            )
            for operator_id in reused:
                connection.execute(
                    """
                    UPDATE operator_state
                    SET plan_version = ?, name = ?, kind = ?, primary_path = ?
                    WHERE operator_id = ?
                    """,
                    (
                        plan.version,
                        plan.operators[
                            next(
                                i
                                for i, item in enumerate(plan.operators)
                                if item.operator_id == operator_id
                            )
                        ].name,
                        next(
                            item.kind
                            for item in plan.operators
                            if item.operator_id == operator_id
                        ),
                        plan.precision.accumulator,
                        operator_id,
                    ),
                )
            for operator in plan.operators:
                existed = connection.execute(
                    "SELECT 1 FROM operator_state WHERE operator_id = ?",
                    (operator.operator_id,),
                ).fetchone()
                if operator.operator_id not in reused or existed is None:
                    if existed is None:
                        self._insert_state_row(
                            connection,
                            operator,
                            plan.precision,
                            plan.version,
                            now,
                        )
            connection.execute(
                """
                INSERT INTO plan_audit(
                    old_version, new_version, old_digest, new_digest,
                    classification, reused_json, rebuilt_json, clock
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    old_version,
                    plan.version,
                    old_digest,
                    plan.digest,
                    classification,
                    canonical_json(reused),
                    canonical_json(rebuilt),
                    now,
                ),
            )
        elif inserted_batches:
            classification = "incremental_append"
            connection.execute(
                """
                INSERT INTO plan_audit(
                    old_version, new_version, old_digest, new_digest,
                    classification, reused_json, rebuilt_json, clock
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    old_version,
                    plan.version,
                    old_digest,
                    plan.digest,
                    classification,
                    canonical_json(sorted(item.operator_id for item in plan.operators)),
                    canonical_json([]),
                    now,
                ),
            )
        else:
            classification = "unchanged"

        if inserted_batches:
            window_ids = [
                item.operator_id for item in plan.operators if item.kind == "window"
            ]
            reset_ids = []
            for operator_id in window_ids:
                row = connection.execute(
                    "SELECT finalized FROM operator_state WHERE operator_id = ?",
                    (operator_id,),
                ).fetchone()
                if row is not None and int(row["finalized"]):
                    reset_ids.append(operator_id)
            for operator_id in reset_ids:
                self._reset_operator(connection, operator_id)
                operator = next(
                    item for item in plan.operators if item.operator_id == operator_id
                )
                self._insert_state_row(
                    connection,
                    operator,
                    plan.precision,
                    plan.version,
                    now,
                )

        return {
            "ok": True,
            "command": "load",
            "plan": {
                "version": plan.version,
                "digest": plan.digest,
                "classification": classification,
            },
            "operators": [
                {
                    "operator_id": item.operator_id,
                    "name": item.name,
                    "kind": item.kind,
                }
                for item in plan.operators
            ],
            "inserted_batches": sorted(inserted_batches),
            "reused_operators": reused,
            "rebuilt_operators": rebuilt,
            "logical_clock": now,
        }

    def status(self) -> dict[str, Any]:
        connection = self._connect()
        try:
            with read_transaction(connection):
                plan = self._fetch_plan(connection)
                rows = {
                    row["operator_id"]: row
                    for row in connection.execute(
                        "SELECT * FROM operator_state"
                    ).fetchall()
                }
                applied_counts = {
                    row["operator_id"]: int(row["count"])
                    for row in connection.execute(
                        "SELECT operator_id, COUNT(*) AS count FROM applied GROUP BY operator_id"
                    ).fetchall()
                }
                operators = []
                for operator in plan.operators:
                    operators.append(
                        _operator_status_payload(
                            operator,
                            rows.get(operator.operator_id),
                            plan.precision,
                            plan_version=plan.version,
                            committed_batches=applied_counts.get(operator.operator_id, 0),
                        )
                    )
                operators.sort(key=lambda item: item["operator_id"])
                batch_rows = connection.execute(
                    """
                    SELECT
                        COUNT(*) AS total,
                        SUM(CASE WHEN status = 'committed' THEN 1 ELSE 0 END) AS committed,
                        SUM(CASE WHEN status = 'partial' THEN 1 ELSE 0 END) AS partial,
                        SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                        SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending,
                        MAX(clock) AS max_clock
                    FROM batches
                    """
                ).fetchone()
                return {
                    "ok": True,
                    "command": "status",
                    "plan": {
                        "version": plan.version,
                        "digest": plan.digest,
                        "accumulator": plan.precision.accumulator,
                        "scale": plan.precision.scale,
                        "relative_tolerance": plan.precision.relative_tolerance,
                        "clock_step": plan.precision.clock_step,
                    },
                    "batches": {
                        "total": int(batch_rows["total"] or 0),
                        "committed": int(batch_rows["committed"] or 0),
                        "partial": int(batch_rows["partial"] or 0),
                        "failed": int(batch_rows["failed"] or 0),
                        "pending": int(batch_rows["pending"] or 0),
                        "max_logical_clock": (
                            int(batch_rows["max_clock"])
                            if batch_rows["max_clock"] is not None
                            else None
                        ),
                    },
                    "operators": operators,
                }
        finally:
            connection.close()

    def plan_history(self) -> dict[str, Any]:
        connection = self._connect()
        try:
            with read_transaction(connection):
                plan = self._fetch_plan(connection)
                snapshots = [
                    {
                        "version": int(row["version"]),
                        "digest": row["digest"],
                        "classification": row["classification"],
                        "created_clock": int(row["created_clock"]),
                    }
                    for row in connection.execute(
                        "SELECT * FROM plans ORDER BY version"
                    ).fetchall()
                ]
                audit = [
                    {
                        "old_version": row["old_version"],
                        "new_version": int(row["new_version"]),
                        "old_digest": row["old_digest"],
                        "new_digest": row["new_digest"],
                        "classification": row["classification"],
                        "reused_operators": json.loads(row["reused_json"]),
                        "rebuilt_operators": json.loads(row["rebuilt_json"]),
                        "logical_clock": int(row["clock"]),
                    }
                    for row in connection.execute(
                        "SELECT * FROM plan_audit ORDER BY audit_id"
                    ).fetchall()
                ]
                return {
                    "ok": True,
                    "command": "plan show",
                    "current_plan": {
                        "version": plan.version,
                        "digest": plan.digest,
                        "operators": [item.as_dict() for item in plan.operators],
                    },
                    "snapshots": snapshots,
                    "audit": audit,
                }
        finally:
            connection.close()

    @staticmethod
    def _near_threshold(left: float, right: float, tolerance: float) -> float:
        return tolerance * max(abs(left), abs(right))

    def _sensitivity_for_records(
        self,
        records: list[dict[str, Any]],
        tolerance: float,
    ) -> dict[str, list[dict[str, Any]]]:
        usable = [
            record
            for record in records
            if record.get("value") is not None
            and isinstance(record.get("value"), (int, float))
        ]
        usable.sort(
            key=lambda item: (
                float(item["value"]),
                item.get("group_key", ""),
                int(item.get("window_index", -1)),
            )
        )
        flags: dict[str, list[dict[str, Any]]] = {}
        for left_index, left in enumerate(usable):
            for right in usable[left_index + 1 :]:
                left_value = float(left["value"])
                right_value = float(right["value"])
                difference = abs(left_value - right_value)
                threshold = self._near_threshold(left_value, right_value, tolerance)
                if difference > threshold:
                    break
                left_id = (
                    f"{left['group_key']}|{int(left.get('window_index', -1))}"
                    if left["kind"] == "window"
                    else left["group_key"]
                )
                right_id = (
                    f"{right['group_key']}|{int(right.get('window_index', -1))}"
                    if right["kind"] == "window"
                    else right["group_key"]
                )
                left_entry = {
                    "other_group_key": right["group_key"],
                    "other_group": right.get("group"),
                    "other_window_index": right.get("window_index"),
                    "other_value": right["value"],
                    "abs_diff": difference,
                    "threshold": threshold,
                    "reason": "near_tie_rank",
                }
                right_entry = {
                    "other_group_key": left["group_key"],
                    "other_group": left.get("group"),
                    "other_window_index": left.get("window_index"),
                    "other_value": left["value"],
                    "abs_diff": difference,
                    "threshold": threshold,
                    "reason": "near_tie_rank",
                }
                flags.setdefault(left_id, []).append(left_entry)
                flags.setdefault(right_id, []).append(right_entry)
        for values in flags.values():
            values.sort(
                key=lambda item: (
                    float(item["abs_diff"]),
                    str(item["other_group_key"]),
                    int(item.get("other_window_index") or -1),
                )
            )
            del values[64:]
        return flags

    def _refresh_sensitivity(self, connection: sqlite3.Connection) -> None:
        plan = self._fetch_plan(connection)
        for operator in plan.operators:
            if operator.kind == "aggregate":
                rows = connection.execute(
                    """
                    SELECT group_key, record_json
                    FROM group_results WHERE operator_id = ?
                    """,
                    (operator.operator_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT group_key, window_index, record_json
                    FROM window_results WHERE operator_id = ?
                    """,
                    (operator.operator_id,),
                ).fetchall()
            records = [json.loads(row["record_json"]) for row in rows]
            flags = self._sensitivity_for_records(
                records,
                plan.precision.relative_tolerance,
            )
            for record in records:
                if operator.kind == "aggregate":
                    record_id = record["group_key"]
                else:
                    record_id = f"{record['group_key']}|{int(record['window_index'])}"
                sensitivity = flags.get(record_id, [])
                record["precision_sensitive"] = bool(sensitivity)
                record["sensitivity"] = sensitivity
                record.pop("result_digest", None)
                record["result_digest"] = sha256_value(record)
                if operator.kind == "aggregate":
                    connection.execute(
                        """
                        UPDATE group_results
                        SET precision_sensitive = ?, sensitivity_json = ?,
                            record_json = ?, result_digest = ?
                        WHERE operator_id = ? AND group_key = ?
                        """,
                        (
                            int(record["precision_sensitive"]),
                            canonical_json(sensitivity),
                            canonical_json(record),
                            record["result_digest"],
                            operator.operator_id,
                            record["group_key"],
                        ),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE window_results
                        SET precision_sensitive = ?, sensitivity_json = ?,
                            record_json = ?, result_digest = ?
                        WHERE operator_id = ? AND group_key = ? AND window_index = ?
                        """,
                        (
                            int(record["precision_sensitive"]),
                            canonical_json(sensitivity),
                            canonical_json(record),
                            record["result_digest"],
                            operator.operator_id,
                            record["group_key"],
                            int(record["window_index"]),
                        ),
                    )

    def _build_results_payload(
        self,
        connection: sqlite3.Connection,
    ) -> dict[str, Any]:
        plan = self._fetch_plan(connection)
        operator_ids = [item.operator_id for item in plan.operators]
        placeholders = ",".join("?" for _ in operator_ids)
        failure_filter = f"(operator_id = '' OR operator_id IN ({placeholders}))"
        aggregates = [
            json.loads(row["record_json"])
            for row in connection.execute(
                "SELECT record_json FROM group_results ORDER BY operator_id, group_key"
            ).fetchall()
        ]
        windows = [
            json.loads(row["record_json"])
            for row in connection.execute(
                """
                SELECT record_json FROM window_results
                ORDER BY operator_id, group_key, window_index
                """
            ).fetchall()
        ]
        failures = [
            {
                "operator_id": row["operator_id"],
                "batch_seq": row["batch_seq"],
                "code": row["code"],
                "message": row["message"],
            }
            for row in connection.execute(
                f"""
                SELECT operator_id, batch_seq, code, message
                FROM failures
                WHERE {failure_filter}
                ORDER BY batch_seq, operator_id, code
                """,
                tuple(operator_ids),
            ).fetchall()
        ]
        result_digest = sha256_value({"aggregates": aggregates, "windows": windows})
        return {
            "schema_version": 1,
            "plan": {"version": plan.version, "digest": plan.digest},
            "precision": plan.precision.as_dict(),
            "aggregates": aggregates,
            "windows": windows,
            "failures": failures,
            "summary": {
                "aggregate_count": len(aggregates),
                "window_count": len(windows),
                "sensitive_aggregate_count": sum(
                    1 for item in aggregates if item["precision_sensitive"]
                ),
                "sensitive_window_count": sum(
                    1 for item in windows if item["precision_sensitive"]
                ),
                "result_digest": result_digest,
            },
        }

    def export_results(self) -> dict[str, Any]:
        with FileLock(self.lock_path):
            connection = self._connect()
            try:
                with immediate_transaction(connection):
                    self._refresh_sensitivity(connection)
                    payload = self._build_results_payload(connection)
            finally:
                connection.close()
            path = self.output_dir / "results.json"
            atomic_write_text(path, pretty_json(payload) + "\n")
        return {
            "ok": True,
            "path": str(path),
            "result_digest": payload["summary"]["result_digest"],
            "aggregate_count": len(payload["aggregates"]),
            "window_count": len(payload["windows"]),
        }

    def _upsert_group_record(
        self,
        connection: sqlite3.Connection,
        record: dict[str, Any],
    ) -> None:
        value = record.get("value")
        connection.execute(
            """
            INSERT INTO group_results(
                operator_id, group_key, group_json, count, watermark_seq,
                primary_path, value_number, float_value, fixed_value,
                error_bound, path_delta, precision_sensitive, sensitivity_json,
                error_chain_json, record_json, result_digest
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(operator_id, group_key) DO UPDATE SET
                group_json = excluded.group_json,
                count = excluded.count,
                watermark_seq = excluded.watermark_seq,
                primary_path = excluded.primary_path,
                value_number = excluded.value_number,
                float_value = excluded.float_value,
                fixed_value = excluded.fixed_value,
                error_bound = excluded.error_bound,
                path_delta = excluded.path_delta,
                precision_sensitive = excluded.precision_sensitive,
                sensitivity_json = excluded.sensitivity_json,
                error_chain_json = excluded.error_chain_json,
                record_json = excluded.record_json,
                result_digest = excluded.result_digest
            """,
            (
                record["operator_id"],
                record["group_key"],
                canonical_json(record["group"]),
                int(record["count"]),
                int(record["watermark_seq"]),
                record["primary_path"],
                float(value) if isinstance(value, (int, float)) else None,
                float(record["float_value"]),
                float(record["fixed_value"]),
                float(record["error_bound"]),
                float(record["path_delta"]),
                int(bool(record["precision_sensitive"])),
                canonical_json(record["sensitivity"]),
                canonical_json(record["error_chain"]),
                canonical_json(record),
                record["result_digest"],
            ),
        )

    def _upsert_window_record(
        self,
        connection: sqlite3.Connection,
        record: dict[str, Any],
    ) -> None:
        value = record.get("value")
        connection.execute(
            """
            INSERT INTO window_results(
                operator_id, group_key, window_index, start_row, end_row,
                length, complete, status, primary_path, value_number,
                float_value, fixed_value, error_bound, path_delta,
                precision_sensitive, sensitivity_json, error_chain_json,
                record_json, result_digest
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(operator_id, group_key, window_index) DO UPDATE SET
                start_row = excluded.start_row,
                end_row = excluded.end_row,
                length = excluded.length,
                complete = excluded.complete,
                status = excluded.status,
                primary_path = excluded.primary_path,
                value_number = excluded.value_number,
                float_value = excluded.float_value,
                fixed_value = excluded.fixed_value,
                error_bound = excluded.error_bound,
                path_delta = excluded.path_delta,
                precision_sensitive = excluded.precision_sensitive,
                sensitivity_json = excluded.sensitivity_json,
                error_chain_json = excluded.error_chain_json,
                record_json = excluded.record_json,
                result_digest = excluded.result_digest
            """,
            (
                record["operator_id"],
                record["group_key"],
                int(record["window_index"]),
                int(record["start_row"]),
                int(record["end_row"]),
                int(record["length"]),
                int(bool(record["complete"])),
                record["status"],
                record["primary_path"],
                float(value) if isinstance(value, (int, float)) else None,
                float(record["float_value"]),
                float(record["fixed_value"]),
                float(record["error_bound"]),
                float(record["path_delta"]),
                int(bool(record["precision_sensitive"])),
                canonical_json(record["sensitivity"]),
                canonical_json(record["error_chain"]),
                canonical_json(record),
                record["result_digest"],
            ),
        )

    def _write_traces(
        self,
        connection: sqlite3.Connection,
        traces: list[dict[str, Any]],
    ) -> None:
        for trace in traces:
            if trace.get("column") is None:
                continue
            connection.execute(
                """
                INSERT OR REPLACE INTO conversion_traces(
                    operator_id, batch_seq, column_name, row_index, path,
                    raw_value, scale, scaled_value, direction, group_key,
                    details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trace["operator_id"],
                    int(trace["batch_seq"]),
                    trace["column"],
                    int(trace["row_index"]),
                    trace["path"],
                    str(trace["raw_value"]),
                    int(trace["scale"]),
                    str(trace["scaled_value"]),
                    trace["direction"],
                    trace["group_key"],
                    canonical_json(trace.get("details", {})),
                ),
            )

    def _write_application(
        self,
        connection: sqlite3.Connection,
        operator: OperatorSpec,
        precision: Precision,
        plan_version: int,
        state: dict[str, Any],
        *,
        batch: Batch,
        clock: int,
        output_records: list[dict[str, Any]],
        result_records: list[dict[str, Any]],
        traces: list[dict[str, Any]],
    ) -> None:
        self._write_state(
            connection,
            operator,
            precision,
            plan_version,
            state,
            watermark_seq=batch.seq,
            clock=clock,
            status="ready",
            failure=None,
            finalized=bool(state.get("finalized", False)),
        )
        output_json = canonical_json(output_records)
        output_digest = sha256_value(output_records)
        connection.execute(
            """
            INSERT OR REPLACE INTO batch_outputs(
                operator_id, batch_seq, plan_version, output_json, output_digest
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                operator.operator_id,
                batch.seq,
                plan_version,
                output_json,
                output_digest,
            ),
        )
        connection.execute(
            """
            INSERT OR REPLACE INTO applied(
                operator_id, batch_seq, result_digest, state_digest, clock
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                operator.operator_id,
                batch.seq,
                output_digest,
                _state_digest(state),
                clock,
            ),
        )
        self._write_traces(connection, traces)
        for record in result_records:
            if record["kind"] == "aggregate":
                self._upsert_group_record(connection, record)
            else:
                self._upsert_window_record(connection, record)

    def _mark_operator_failure(
        self,
        connection: sqlite3.Connection,
        operator: OperatorSpec,
        precision: Precision,
        plan_version: int,
        state: dict[str, Any],
        *,
        batch_seq: int | None,
        error: ColAggError,
        clock: int,
    ) -> None:
        stored = connection.execute(
            """
            SELECT watermark_seq, rows_seen, finalized
            FROM operator_state WHERE operator_id = ?
            """,
            (operator.operator_id,),
        ).fetchone()
        watermark = int(stored["watermark_seq"]) if stored is not None else -1
        rows_seen = int(stored["rows_seen"]) if stored is not None else 0
        finalized = bool(stored["finalized"]) if stored is not None else False
        self._write_state(
            connection,
            operator,
            precision,
            plan_version,
            state,
            watermark_seq=watermark,
            clock=clock,
            status="failed",
            failure=error,
            failure_batch_seq=batch_seq,
            finalized=finalized,
        )
        self._record_failure(
            connection,
            scope="operator",
            batch_seq=batch_seq,
            operator_id=operator.operator_id,
            error=error,
            clock=clock,
        )

    def _state_from_row(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return json.loads(row["state_json"])

    def _actual_batch_digest(self, batch: Batch) -> str:
        return sha256_value(
            {
                "seq": batch.seq,
                "row_count": batch.row_count,
                "columns": [column.as_dict() for column in batch.columns],
            }
        )

    def _check_source_integrity(
        self,
        connection: sqlite3.Connection,
        provided_batches: list[Batch] | None,
    ) -> None:
        rows = connection.execute("SELECT * FROM batches ORDER BY seq").fetchall()
        if not rows:
            raise ColAggError(E_NO_STATE, "no batches have been loaded")
        by_seq = {int(row["seq"]): row for row in rows}
        if provided_batches is not None:
            incoming = {batch.seq: batch for batch in provided_batches}
            missing = sorted(set(by_seq) - set(incoming))
            extra = sorted(set(incoming) - set(by_seq))
            if missing or extra:
                raise ColAggError(
                    E_BATCH_DIGEST_MISMATCH,
                    "batch file does not match the loaded batch catalog",
                    details={"missing": missing, "not_loaded": extra},
                )
            for seq, batch in incoming.items():
                if by_seq[seq]["digest"] != batch.digest:
                    raise ColAggError(
                        E_BATCH_DIGEST_MISMATCH,
                        f"batch {seq} digest changed after load",
                        details={
                            "seq": seq,
                            "persisted_digest": by_seq[seq]["digest"],
                            "incoming_digest": batch.digest,
                        },
                    )
        for row in rows:
            batch = _row_to_batch(row)
            actual = self._actual_batch_digest(batch)
            if actual != row["digest"]:
                raise ColAggError(
                    E_BATCH_DIGEST_MISMATCH,
                    f"persisted batch {batch.seq} failed its content digest check",
                    details={
                        "seq": batch.seq,
                        "stored_digest": row["digest"],
                        "computed_digest": actual,
                    },
                )

    def _persist_global_digest_failure(
        self,
        batch_seq: int,
        error: ColAggError,
    ) -> None:
        with FileLock(self.lock_path):
            connection = self._connect()
            try:
                with immediate_transaction(connection):
                    plan = self._fetch_plan(connection)
                    clock = self._next_clock(connection, plan.precision)
                    for operator in plan.operators:
                        applied = connection.execute(
                            "SELECT 1 FROM applied WHERE operator_id = ? AND batch_seq = ?",
                            (operator.operator_id, batch_seq),
                        ).fetchone()
                        if applied is not None:
                            continue
                        row = connection.execute(
                            "SELECT * FROM operator_state WHERE operator_id = ?",
                            (operator.operator_id,),
                        ).fetchone()
                        state = self._state_from_row(row)
                        if state is None:
                            state = (
                                empty_aggregate_state(operator)
                                if operator.kind == "aggregate"
                                else empty_window_state(operator)
                            )
                        self._mark_operator_failure(
                            connection,
                            operator,
                            plan.precision,
                            plan.version,
                            state,
                            batch_seq=batch_seq,
                            error=error,
                            clock=clock,
                        )
                    self._record_failure(
                        connection,
                        scope="batch",
                        batch_seq=batch_seq,
                        operator_id="",
                        error=error,
                        clock=clock,
                    )
                    applied_count = int(
                        connection.execute(
                            "SELECT COUNT(*) AS count FROM applied WHERE batch_seq = ?",
                            (batch_seq,),
                        ).fetchone()["count"]
                    )
                    total_ops = len(plan.operators)
                    if total_ops and applied_count >= total_ops:
                        batch_status = "committed"
                    elif applied_count:
                        batch_status = "partial"
                    else:
                        batch_status = "failed"
                    connection.execute(
                        "UPDATE batches SET status = ? WHERE seq = ?",
                        (batch_status, batch_seq),
                    )
                    self._refresh_sensitivity(connection)
            finally:
                connection.close()

    def _clock_for_batch(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        precision: Precision,
        requested_clock: int | None,
    ) -> int:
        if row["clock"] is not None:
            return int(row["clock"])
        assigned = connection.execute(
            "SELECT COUNT(*) AS count FROM batches WHERE clock IS NOT NULL"
        ).fetchone()
        if int(assigned["count"]) == 0 and requested_clock is not None:
            return requested_clock
        return self._next_clock(connection, precision)

    def _process_one_batch(
        self,
        seq: int,
        *,
        requested_clock: int | None,
    ) -> dict[str, Any]:
        with FileLock(self.lock_path):
            connection = self._connect()
            try:
                with immediate_transaction(connection):
                    plan = self._fetch_plan(connection)
                    row = connection.execute(
                        "SELECT * FROM batches WHERE seq = ?", (seq,)
                    ).fetchone()
                    if row is None:
                        raise ColAggError(E_BAD_BATCH, f"batch {seq} is not loaded")
                    batch = _row_to_batch(row)
                    actual_digest = self._actual_batch_digest(batch)
                    if actual_digest != row["digest"]:
                        raise ColAggError(
                            E_BATCH_DIGEST_MISMATCH,
                            f"batch {seq} content digest mismatch",
                            details={
                                "seq": seq,
                                "stored_digest": row["digest"],
                                "computed_digest": actual_digest,
                            },
                        )
                    if row["status"] == "failed":
                        return {
                            "seq": seq,
                            "skipped": True,
                            "reason": "failed",
                            "operators": [],
                        }
                    clock = self._clock_for_batch(
                        connection, row, plan.precision, requested_clock
                    )
                    connection.execute(
                        "UPDATE batches SET clock = ? WHERE seq = ?", (clock, seq)
                    )
                    applied_now: list[str] = []
                    skipped: list[str] = []
                    failures: list[dict[str, Any]] = []
                    for operator in plan.operators:
                        applied = connection.execute(
                            "SELECT 1 FROM applied WHERE operator_id = ? AND batch_seq = ?",
                            (operator.operator_id, seq),
                        ).fetchone()
                        if applied is not None:
                            skipped.append(operator.operator_id)
                            continue
                        state_row = connection.execute(
                            "SELECT * FROM operator_state WHERE operator_id = ?",
                            (operator.operator_id,),
                        ).fetchone()
                        if state_row is not None and state_row["status"] == "failed":
                            skipped.append(operator.operator_id)
                            continue
                        if (
                            state_row is not None
                            and int(state_row["plan_version"]) != plan.version
                        ):
                            error = ColAggError(
                                E_STATE_STALE,
                                "operator state belongs to a different plan version",
                                details={
                                    "operator_id": operator.operator_id,
                                    "state_plan_version": int(state_row["plan_version"]),
                                    "current_plan_version": plan.version,
                                },
                            )
                            state = self._state_from_row(state_row) or (
                                empty_aggregate_state(operator)
                                if operator.kind == "aggregate"
                                else empty_window_state(operator)
                            )
                            self._mark_operator_failure(
                                connection,
                                operator,
                                plan.precision,
                                plan.version,
                                state,
                                batch_seq=seq,
                                error=error,
                                clock=clock,
                            )
                            failures.append(
                                {
                                    "operator_id": operator.operator_id,
                                    "code": error.code,
                                    "message": error.message,
                                }
                            )
                            continue
                        state = self._state_from_row(state_row)
                        if state is None:
                            state = (
                                empty_aggregate_state(operator)
                                if operator.kind == "aggregate"
                                else empty_window_state(operator)
                            )
                        watermark = int(
                            state_row["watermark_seq"] if state_row is not None else -1
                        )
                        if watermark >= seq:
                            error = ColAggError(
                                E_STATE_STALE,
                                "operator watermark is not behind the incoming batch",
                                details={
                                    "operator_id": operator.operator_id,
                                    "watermark_seq": watermark,
                                    "batch_seq": seq,
                                },
                            )
                            self._mark_operator_failure(
                                connection,
                                operator,
                                plan.precision,
                                plan.version,
                                state,
                                batch_seq=seq,
                                error=error,
                                clock=clock,
                            )
                            failures.append(
                                {
                                    "operator_id": operator.operator_id,
                                    "code": error.code,
                                    "message": error.message,
                                }
                            )
                            continue
                        try:
                            if operator.kind == "aggregate":
                                (
                                    next_state,
                                    output_records,
                                    result_records,
                                    traces,
                                ) = apply_aggregate_batch(
                                    operator, plan.precision, state, batch
                                )
                            else:
                                (
                                    next_state,
                                    output_records,
                                    result_records,
                                    traces,
                                ) = apply_window_batch(
                                    operator, plan.precision, state, batch
                                )
                        except ColAggError as error:
                            self._mark_operator_failure(
                                connection,
                                operator,
                                plan.precision,
                                plan.version,
                                state,
                                batch_seq=seq,
                                error=error,
                                clock=clock,
                            )
                            failures.append(
                                {
                                    "operator_id": operator.operator_id,
                                    "code": error.code,
                                    "message": error.message,
                                }
                            )
                            continue
                        self._write_application(
                            connection,
                            operator,
                            plan.precision,
                            plan.version,
                            next_state,
                            batch=batch,
                            clock=clock,
                            output_records=output_records,
                            result_records=result_records,
                            traces=traces,
                        )
                        applied_now.append(operator.operator_id)

                    applied_count = int(
                        connection.execute(
                            "SELECT COUNT(*) AS count FROM applied WHERE batch_seq = ?",
                            (seq,),
                        ).fetchone()["count"]
                    )
                    total_ops = len(plan.operators)
                    if total_ops and applied_count >= total_ops:
                        status = "committed"
                    elif applied_count:
                        status = "partial"
                    else:
                        status = "failed"
                    connection.execute(
                        "UPDATE batches SET status = ? WHERE seq = ?", (status, seq)
                    )
                    self._refresh_sensitivity(connection)
                    return {
                        "seq": seq,
                        "skipped": False,
                        "clock": clock,
                        "status": status,
                        "applied_operators": sorted(applied_now),
                        "already_applied": sorted(skipped),
                        "failures": failures,
                    }
            finally:
                connection.close()

    def _replay_operator(
        self,
        connection: sqlite3.Connection,
        operator: OperatorSpec,
        precision: Precision,
        plan_version: int,
        batches: list[Batch],
        clock_by_seq: dict[int, int],
    ) -> bool:
        state = (
            empty_aggregate_state(operator)
            if operator.kind == "aggregate"
            else empty_window_state(operator)
        )
        for batch in batches:
            clock = clock_by_seq.get(batch.seq, precision.clock_start)
            try:
                if operator.kind == "aggregate":
                    next_state, output_records, result_records, traces = (
                        apply_aggregate_batch(operator, precision, state, batch)
                    )
                else:
                    next_state, output_records, result_records, traces = (
                        apply_window_batch(operator, precision, state, batch)
                    )
            except ColAggError as error:
                self._mark_operator_failure(
                    connection,
                    operator,
                    precision,
                    plan_version,
                    state,
                    batch_seq=batch.seq,
                    error=error,
                    clock=clock,
                )
                return False
            self._write_application(
                connection,
                operator,
                precision,
                plan_version,
                next_state,
                batch=batch,
                clock=clock,
                output_records=output_records,
                result_records=result_records,
                traces=traces,
            )
            state = next_state
        self._write_state(
            connection,
            operator,
            precision,
            plan_version,
            state,
            watermark_seq=(batches[-1].seq if batches else -1),
            clock=(clock_by_seq.get(batches[-1].seq, precision.clock_start) if batches else precision.clock_start),
            status="ready",
            failure=None,
            finalized=bool(state.get("finalized", False)),
        )
        return True

    def _recover_operator(
        self,
        connection: sqlite3.Connection,
        operator: OperatorSpec,
        plan: PlanDefinition,
    ) -> str:
        row = connection.execute(
            "SELECT * FROM operator_state WHERE operator_id = ?",
            (operator.operator_id,),
        ).fetchone()
        if row is not None and row["status"] == "failed":
            return "failed"
        eligible = [
            batch
            for batch in self._fetch_batches(
                connection, statuses=("committed", "partial")
            )
        ]
        clock_by_seq = {
            int(batch_row["seq"]): int(batch_row["clock"])
            for batch_row in connection.execute(
                "SELECT seq, clock FROM batches WHERE clock IS NOT NULL"
            ).fetchall()
        }
        if row is not None and int(row["plan_version"]) != plan.version:
            state = self._state_from_row(row) or (
                empty_aggregate_state(operator)
                if operator.kind == "aggregate"
                else empty_window_state(operator)
            )
            error = ColAggError(
                E_STATE_STALE,
                "operator state version is stale",
                details={
                    "operator_id": operator.operator_id,
                    "state_plan_version": int(row["plan_version"]),
                    "current_plan_version": plan.version,
                },
            )
            self._mark_operator_failure(
                connection,
                operator,
                plan.precision,
                plan.version,
                state,
                batch_seq=None,
                error=error,
                clock=self._next_clock(connection, plan.precision),
            )
            return "stale"

        applied_rows = connection.execute(
            "SELECT batch_seq FROM applied WHERE operator_id = ? ORDER BY batch_seq",
            (operator.operator_id,),
        ).fetchall()
        applied_seqs = [int(item["batch_seq"]) for item in applied_rows]
        eligible_row_counts = {
            int(batch_row["seq"]): int(batch_row["row_count"])
            for batch_row in connection.execute(
                "SELECT seq, row_count FROM batches WHERE status IN ('committed', 'partial')"
            ).fetchall()
        }
        rebuild_reason: str | None = None
        pending_exists = connection.execute(
            "SELECT 1 FROM batches WHERE status = 'pending' LIMIT 1"
        ).fetchone() is not None
        if row is None:
            rebuild_reason = "missing_state" if (applied_seqs or eligible) else None
        else:
            state = self._state_from_row(row)
            if state is None or sha256_value(state) != row["state_digest"]:
                rebuild_reason = "state_digest_mismatch"
            elif applied_seqs and int(row["watermark_seq"]) != max(applied_seqs):
                rebuild_reason = "watermark_mismatch"
            elif (
                int(row["rows_seen"])
                != sum(eligible_row_counts.get(seq, 0) for seq in applied_seqs)
            ):
                rebuild_reason = "rows_seen_mismatch"
            elif not applied_seqs and eligible:
                rebuild_reason = "unapplied_committed_batches"
            elif applied_seqs and any(
                seq not in eligible_row_counts for seq in applied_seqs
            ):
                rebuild_reason = "applied_batch_not_committed"
            elif (
                operator.kind == "window"
                and pending_exists
                and bool(row["finalized"])
            ):
                rebuild_reason = "finalized_with_pending_batches"

        if rebuild_reason is None:
            if row is None:
                self._insert_state_row(
                    connection,
                    operator,
                    plan.precision,
                    plan.version,
                    self._next_clock(connection, plan.precision),
                )
                return "initialized"
            return "ok"

        self._reset_operator(connection, operator.operator_id)
        success = self._replay_operator(
            connection,
            operator,
            plan.precision,
            plan.version,
            eligible,
            clock_by_seq,
        )
        if not success:
            return "replay_failed"
        return f"rebuilt:{rebuild_reason}"

    def recover(self) -> dict[str, Any]:
        with FileLock(self.lock_path):
            connection = self._connect()
            try:
                with immediate_transaction(connection):
                    plan = self._fetch_plan(connection)
                    outcomes: dict[str, str] = {}
                    for operator in plan.operators:
                        outcomes[operator.operator_id] = self._recover_operator(
                            connection, operator, plan
                        )
                    self._refresh_batch_statuses(connection)
                    self._refresh_sensitivity(connection)
                    return {
                        "ok": True,
                        "operators": outcomes,
                        "rebuilt": [
                            operator_id
                            for operator_id, outcome in outcomes.items()
                            if outcome.startswith("rebuilt:")
                        ],
                        "failed": [
                            operator_id
                            for operator_id, outcome in outcomes.items()
                            if outcome in ("failed", "replay_failed", "stale")
                        ],
                    }
            finally:
                connection.close()

    def _refresh_batch_statuses(self, connection: sqlite3.Connection) -> None:
        plan = self._fetch_plan(connection)
        total_ops = len(plan.operators)
        for row in connection.execute(
            "SELECT seq, status FROM batches ORDER BY seq"
        ).fetchall():
            applied_count = int(
                connection.execute(
                    "SELECT COUNT(*) AS count FROM applied WHERE batch_seq = ?",
                    (int(row["seq"]),),
                ).fetchone()["count"]
            )
            if applied_count == 0:
                if row["status"] in ("committed", "partial"):
                    new_status = "failed"
                else:
                    continue
            elif total_ops and applied_count >= total_ops:
                new_status = "committed"
            else:
                new_status = "partial"
            if new_status != row["status"]:
                connection.execute(
                    "UPDATE batches SET status = ? WHERE seq = ?",
                    (new_status, int(row["seq"])),
                )

    def _finalize_windows(self) -> list[str]:
        with FileLock(self.lock_path):
            connection = self._connect()
            try:
                with immediate_transaction(connection):
                    plan = self._fetch_plan(connection)
                    finalized: list[str] = []
                    for operator in plan.operators:
                        if operator.kind != "window":
                            continue
                        row = connection.execute(
                            "SELECT * FROM operator_state WHERE operator_id = ?",
                            (operator.operator_id,),
                        ).fetchone()
                        if row is None or row["status"] == "failed" or int(row["finalized"]):
                            continue
                        state = self._state_from_row(row)
                        if state is None:
                            continue
                        next_state, records = finalize_window_state(
                            operator, plan.precision, state
                        )
                        clock = self._next_clock(connection, plan.precision)
                        self._write_state(
                            connection,
                            operator,
                            plan.precision,
                            plan.version,
                            next_state,
                            watermark_seq=int(row["watermark_seq"]),
                            clock=clock,
                            status="ready",
                            failure=None,
                            finalized=True,
                        )
                        for record in records:
                            self._upsert_window_record(connection, record)
                        finalized.append(operator.operator_id)
                    self._refresh_sensitivity(connection)
                    return finalized
            finally:
                connection.close()

    def _next_processable_batch(self, connection: sqlite3.Connection) -> int | None:
        plan = self._fetch_plan(connection)
        failed_ops = {
            row["operator_id"]
            for row in connection.execute(
                "SELECT operator_id FROM operator_state WHERE status = 'failed'"
            ).fetchall()
        }
        applied = {
            (row["operator_id"], int(row["batch_seq"]))
            for row in connection.execute(
                "SELECT operator_id, batch_seq FROM applied"
            ).fetchall()
        }
        for batch_row in connection.execute(
            "SELECT seq, status FROM batches ORDER BY seq"
        ).fetchall():
            seq = int(batch_row["seq"])
            if batch_row["status"] == "failed":
                continue
            for operator in plan.operators:
                if operator.operator_id in failed_ops:
                    continue
                if (operator.operator_id, seq) not in applied:
                    return seq
        return None

    def execute(
        self,
        provided_batches: list[Batch] | None = None,
        *,
        clock: int | None = None,
        max_batches: int | None = None,
        crash_after: int | None = None,
        finalize: bool = True,
        command: str = "execute",
    ) -> dict[str, Any]:
        source_connection = self._connect()
        try:
            try:
                self._check_source_integrity(source_connection, provided_batches)
            except ColAggError as error:
                if error.code == E_BATCH_DIGEST_MISMATCH and "seq" in error.details:
                    self._persist_global_digest_failure(
                        int(error.details["seq"]), error
                    )
                raise
        finally:
            source_connection.close()

        recovery = self.recover()
        recovery_connection = self._connect()
        try:
            recovery_errors = [
                {
                    "operator_id": row["operator_id"],
                    "batch_seq": row["failure_batch_seq"],
                    "code": row["failure_code"],
                    "message": row["failure_message"],
                }
                for row in recovery_connection.execute(
                    """
                    SELECT operator_id, failure_batch_seq, failure_code, failure_message
                    FROM operator_state
                    WHERE status = 'failed' AND failure_code IS NOT NULL
                    ORDER BY operator_id
                    """
                ).fetchall()
            ]
        finally:
            recovery_connection.close()
        processed: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        while len(processed) < (max_batches or 10**18):
            connection = self._connect()
            try:
                next_seq = self._next_processable_batch(connection)
            finally:
                connection.close()
            if next_seq is None:
                break
            result = self._process_one_batch(next_seq, requested_clock=clock)
            if result.get("skipped"):
                continue
            if not result.get("applied_operators") and not result.get("failures"):
                continue
            processed.append(result)
            failures.extend(result.get("failures", []))
            if crash_after is not None and len(processed) >= crash_after:
                os._exit(137)

        pending_connection = self._connect()
        try:
            has_more_work = self._next_processable_batch(pending_connection) is not None
        finally:
            pending_connection.close()
        finalized: list[str] = []
        if finalize and not has_more_work:
            finalized = self._finalize_windows()
        export = self.export_results()
        status = self.status()
        return {
            "ok": not failures and not recovery_errors,
            "command": command,
            "recovery": recovery,
            "recovery_errors": recovery_errors,
            "processed_batches": processed,
            "processed_count": len(processed),
            "failures": failures,
            "deferred_finalize": bool(finalize and has_more_work),
            "finalized_operators": finalized,
            "output": export,
            "status": status,
        }

    @staticmethod
    def _record_core(record: dict[str, Any]) -> dict[str, Any]:
        core = copy.deepcopy(record)
        core.pop("precision_sensitive", None)
        core.pop("sensitivity", None)
        core.pop("result_digest", None)
        return core

    def _recompute_operator(
        self,
        operator: OperatorSpec,
        precision: Precision,
        batches: list[Batch],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        state = (
            empty_aggregate_state(operator)
            if operator.kind == "aggregate"
            else empty_window_state(operator)
        )
        window_records: list[dict[str, Any]] = []
        for batch in batches:
            if operator.kind == "aggregate":
                state, _, _, _ = apply_aggregate_batch(
                    operator, precision, state, batch
                )
            else:
                state, records, _, _ = apply_window_batch(
                    operator, precision, state, batch
                )
                window_records.extend(records)
        if operator.kind == "aggregate":
            records = [
                finalize_group_record(
                    state["groups"][group_key],
                    operator,
                    precision,
                    watermark_seq=int(state["groups"][group_key]["batches"][-1]["batch_seq"]),
                )
                for group_key in sorted(state["groups"])
            ]
            return state, records
        state, partial = finalize_window_state(operator, precision, state)
        window_records.extend(partial)
        window_records.sort(key=lambda item: (item["group_key"], item["window_index"]))
        return state, window_records

    def verify(self, provided_batches: list[Batch] | None = None) -> dict[str, Any]:
        errors: list[dict[str, Any]] = []
        mismatches: list[dict[str, Any]] = []
        with FileLock(self.lock_path):
            connection = self._connect()
            try:
                with immediate_transaction(connection):
                    try:
                        self._check_source_integrity(connection, provided_batches)
                    except ColAggError as error:
                        errors.append(
                            {
                                "code": error.code,
                                "message": error.message,
                                "details": error.details,
                            }
                        )
                    plan = self._fetch_plan(connection)
                    self._refresh_sensitivity(connection)
                    batch_rows = connection.execute(
                        "SELECT seq, status FROM batches ORDER BY seq"
                    ).fetchall()
                    status_counts: dict[str, int] = {}
                    for row in batch_rows:
                        status = str(row["status"])
                        status_counts[status] = status_counts.get(status, 0) + 1
                    if status_counts.get("pending", 0):
                        errors.append(
                            {
                                "code": E_INCOMPLETE,
                                "message": "there are pending batches",
                                "details": {
                                    "pending_batches": [
                                        int(row["seq"])
                                        for row in batch_rows
                                        if row["status"] == "pending"
                                    ]
                                },
                            }
                        )
                    if status_counts.get("partial", 0) or status_counts.get("failed", 0):
                        errors.append(
                            {
                                "code": E_VERIFY_FAILED,
                                "message": "some batches have partial or failed operator chains",
                                "details": status_counts,
                            }
                        )
                    current_operator_ids = [
                        item.operator_id for item in plan.operators
                    ]
                    failure_placeholders = ",".join(
                        "?" for _ in current_operator_ids
                    )
                    failures = [
                        {
                            "operator_id": row["operator_id"],
                            "batch_seq": row["batch_seq"],
                            "code": row["code"],
                            "message": row["message"],
                        }
                        for row in connection.execute(
                            f"""
                            SELECT operator_id, batch_seq, code, message
                            FROM failures
                            WHERE operator_id = ''
                               OR operator_id IN ({failure_placeholders})
                            """,
                            tuple(current_operator_ids),
                        ).fetchall()
                    ]
                    if failures:
                        errors.append(
                            {
                                "code": E_VERIFY_FAILED,
                                "message": "persisted failure records are present",
                                "details": {"failure_count": len(failures)},
                            }
                        )
                    truth_batches = [
                        _row_to_batch(row)
                        for row in connection.execute(
                            "SELECT * FROM batches WHERE status != 'failed' ORDER BY seq"
                        ).fetchall()
                    ]
                    aggregate_comparisons = 0
                    window_comparisons = 0
                    state_comparisons = 0
                    for operator in plan.operators:
                        expected_state, expected_records = self._recompute_operator(
                            operator, plan.precision, truth_batches
                        )
                        state_row = connection.execute(
                            "SELECT * FROM operator_state WHERE operator_id = ?",
                            (operator.operator_id,),
                        ).fetchone()
                        state_comparisons += 1
                        if state_row is None:
                            mismatches.append(
                                {
                                    "kind": "state",
                                    "operator_id": operator.operator_id,
                                    "reason": "missing operator state",
                                }
                            )
                        else:
                            persisted_state = self._state_from_row(state_row)
                            if canonical_json(persisted_state) != canonical_json(expected_state):
                                mismatches.append(
                                    {
                                        "kind": "state",
                                        "operator_id": operator.operator_id,
                                        "reason": "state differs from full replay",
                                        "persisted_digest": state_row["state_digest"],
                                        "expected_digest": sha256_value(expected_state),
                                    }
                                )
                        if operator.kind == "aggregate":
                            aggregate_comparisons += len(expected_records)
                            persisted = {
                                row["group_key"]: json.loads(row["record_json"])
                                for row in connection.execute(
                                    """
                                    SELECT group_key, record_json FROM group_results
                                    WHERE operator_id = ?
                                    """,
                                    (operator.operator_id,),
                                ).fetchall()
                            }
                            expected = {
                                record["group_key"]: record for record in expected_records
                            }
                            for key in sorted(set(persisted) | set(expected)):
                                if key not in persisted:
                                    mismatches.append(
                                        {
                                            "kind": "aggregate",
                                            "operator_id": operator.operator_id,
                                            "group_key": key,
                                            "reason": "missing persisted result",
                                        }
                                    )
                                elif key not in expected:
                                    mismatches.append(
                                        {
                                            "kind": "aggregate",
                                            "operator_id": operator.operator_id,
                                            "group_key": key,
                                            "reason": "unexpected persisted result",
                                        }
                                    )
                                elif canonical_json(
                                    self._record_core(persisted[key])
                                ) != canonical_json(
                                    self._record_core(expected[key])
                                ):
                                    mismatches.append(
                                        {
                                            "kind": "aggregate",
                                            "operator_id": operator.operator_id,
                                            "group_key": key,
                                            "reason": "full replay value mismatch",
                                        }
                                    )
                        else:
                            window_comparisons += len(expected_records)
                            persisted = {
                                (row["group_key"], int(row["window_index"])): json.loads(
                                    row["record_json"]
                                )
                                for row in connection.execute(
                                    """
                                    SELECT group_key, window_index, record_json
                                    FROM window_results WHERE operator_id = ?
                                    """,
                                    (operator.operator_id,),
                                ).fetchall()
                            }
                            expected = {
                                (record["group_key"], int(record["window_index"])): record
                                for record in expected_records
                            }
                            for key in sorted(set(persisted) | set(expected)):
                                if key not in persisted:
                                    mismatches.append(
                                        {
                                            "kind": "window",
                                            "operator_id": operator.operator_id,
                                            "group_key": key[0],
                                            "window_index": key[1],
                                            "reason": "missing persisted result",
                                        }
                                    )
                                elif key not in expected:
                                    mismatches.append(
                                        {
                                            "kind": "window",
                                            "operator_id": operator.operator_id,
                                            "group_key": key[0],
                                            "window_index": key[1],
                                            "reason": "unexpected persisted result",
                                        }
                                    )
                                elif canonical_json(
                                    self._record_core(persisted[key])
                                ) != canonical_json(
                                    self._record_core(expected[key])
                                ):
                                    mismatches.append(
                                        {
                                            "kind": "window",
                                            "operator_id": operator.operator_id,
                                            "group_key": key[0],
                                            "window_index": key[1],
                                            "reason": "full replay value mismatch",
                                        }
                                    )

                    for row in connection.execute(
                        "SELECT * FROM batch_outputs ORDER BY operator_id, batch_seq"
                    ).fetchall():
                        try:
                            output_value = json.loads(row["output_json"])
                            actual = sha256_value(output_value)
                        except (json.JSONDecodeError, ValueError):
                            actual = ""
                        if actual != row["output_digest"]:
                            mismatches.append(
                                {
                                    "kind": "batch_output",
                                    "operator_id": row["operator_id"],
                                    "batch_seq": int(row["batch_seq"]),
                                    "reason": "batch output digest mismatch",
                                }
                            )

                    output_path = self.output_dir / "results.json"
                    expected_payload = self._build_results_payload(connection)
                    output_match = False
                    if not output_path.exists():
                        errors.append(
                            {
                                "code": E_INCOMPLETE,
                                "message": "results.json is missing",
                                "details": {"path": str(output_path)},
                            }
                        )
                    else:
                        try:
                            actual_payload = json.loads(
                                output_path.read_text(encoding="utf-8")
                            )
                            output_match = canonical_json(
                                actual_payload
                            ) == canonical_json(expected_payload)
                        except (OSError, json.JSONDecodeError):
                            actual_payload = None
                        if not output_match:
                            mismatches.append(
                                {
                                    "kind": "output_file",
                                    "reason": "results.json differs from persisted results",
                                    "path": str(output_path),
                                }
                            )

                    integrity = connection.execute("PRAGMA quick_check").fetchone()
                    integrity_ok = bool(integrity and integrity[0] == "ok")
                    if not integrity_ok:
                        errors.append(
                            {
                                "code": E_VERIFY_FAILED,
                                "message": "SQLite quick_check failed",
                            }
                        )
                    report = {
                        "ok": (
                            not errors
                            and not mismatches
                            and output_match
                            and integrity_ok
                        ),
                        "command": "verify",
                        "plan": {"version": plan.version, "digest": plan.digest},
                        "checks": {
                            "sqlite_quick_check": integrity_ok,
                            "output_file_matches": output_match,
                            "batch_status_counts": dict(sorted(status_counts.items())),
                            "aggregate_comparisons": aggregate_comparisons,
                            "window_comparisons": window_comparisons,
                            "state_comparisons": state_comparisons,
                            "batch_output_digests": int(
                                connection.execute(
                                    "SELECT COUNT(*) AS count FROM batch_outputs"
                                ).fetchone()["count"]
                            ),
                        },
                        "errors": errors,
                        "mismatches": mismatches[:100],
                        "mismatch_count": len(mismatches),
                        "result_digest": expected_payload["summary"]["result_digest"],
                    }
            finally:
                connection.close()
        report_path = self.output_dir / "verify.json"
        atomic_write_text(report_path, pretty_json(report) + "\n")
        report["report_path"] = str(report_path)
        return report


