"""Deterministic lock-wait scheduler and accounting engine."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .clock import convert_duration, next_sample, sample_by_id
from .errors import DiagnosticError, E_STATE_CORRUPT
from .locks import (
    conflicting_holders,
    lock_snapshot,
    primary_blocker,
    queue_snapshot,
    queued_entries,
    request_from_operation,
    resource_from_queue_row,
    resources_conflict,
    validate_holder_set,
)
from .model import SampleBundle
from .store import StateStore
from .util import (
    canonical_json,
    decimal_to_micro,
    format_micro,
    format_units,
    tolerance_to_units,
)
from .worker import probe_all, quote_identifier

TERMINAL_STATUSES = frozenset(
    {
        "COMMITTED",
        "ABANDONED_TIMEOUT",
        "ROLLED_BACK_ACTIVE",
        "ROLLED_BACK_DEADLOCK",
        "NOT_STARTED_CONCURRENCY",
        "INTERRUPTED",
    }
)


def _float_from_units(units: int, scale: int) -> str:
    return format_units(units, scale)


def _next_ordinal(connection: sqlite3.Connection, tx_id: str) -> int:
    row = connection.execute(
        "SELECT wait_count FROM tx_accounts WHERE tx_id=?", (tx_id,)
    ).fetchone()
    return (int(row["wait_count"]) if row else 0) + 1


def _wait_source(wait: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    holders = json.loads(wait["holder_txs_json"])
    return {
        "link": "WAIT_TO_FIXED_POINT",
        "wait_id": wait["wait_id"],
        "resource_key": wait["resource_key"],
        "holders": holders,
        "enqueue_sample_id": wait["enqueue_sample"],
        "resolve_sample_id": wait["resolve_sample"],
        "enqueue_raw": wait["enqueue_raw"],
        "resolve_raw": wait["resolve_raw"],
        "raw_delta": wait["raw_delta"],
        "scale": wait["scale"],
        "raw_units_per_ms": wait["raw_units_per_ms"],
        "rounding_method": "HALF_UP",
        "rounding_direction": wait["rounding_direction"],
        "rounded_units": wait["rounded_units"],
        "exact_numerator": wait["exact_numerator"],
        "exact_denominator": wait["exact_denominator"],
        "actual_residual_micro": wait["actual_residual_micro"],
        "error_bound_micro": wait["error_bound_micro"],
        "resolution": wait["resolution"],
    }


def resolve_wait(
    connection: sqlite3.Connection,
    wait_id: str,
    resolve_sample_id: int,
    queue_status: str,
    resolution: str,
    clock: Any,
) -> dict[str, Any] | None:
    """Resolve a waiting record exactly once and update all ledgers atomically."""

    wait = connection.execute("SELECT * FROM waits WHERE wait_id=?", (wait_id,)).fetchone()
    if wait is None or wait["status"] != "WAITING":
        return None
    start = sample_by_id(connection, int(wait["enqueue_sample"]))
    end = sample_by_id(connection, resolve_sample_id)
    conversion = convert_duration(start, end, int(wait["scale"]))
    connection.execute(
        """
        UPDATE waits SET
            resolve_sample=?, resolve_sequence_index=?, resolve_raw=?,
            raw_delta=?, rounded_units=?, exact_numerator=?, exact_denominator=?,
            rounding_direction=?, residual_numerator=?,
            actual_residual_micro=?, error_bound_micro=?,
            status='RESOLVED', resolution=?
        WHERE wait_id=?
        """,
        (
            resolve_sample_id,
            end["sequence_index"],
            end["raw_value"],
            conversion["raw_delta"],
            conversion["rounded_units"],
            conversion["exact_numerator"],
            conversion["exact_denominator"],
            conversion["rounding_direction"],
            conversion["residual_numerator"],
            conversion["actual_residual_micro"],
            conversion["error_bound_micro"],
            resolution,
            wait_id,
        ),
    )
    connection.execute(
        """
        UPDATE wait_queue
        SET status=?, resolve_sample=?, resolution=?
        WHERE wait_id=? AND status='QUEUED'
        """,
        (queue_status, resolve_sample_id, resolution, wait_id),
    )
    row = connection.execute("SELECT * FROM waits WHERE wait_id=?", (wait_id,)).fetchone()
    assert row is not None
    ordinal = _next_ordinal(connection, row["tx_id"])
    connection.execute(
        """
        INSERT INTO tx_accounts(
            tx_id, wait_count, wait_total_units, wait_actual_error_micro,
            wait_error_micro, updated_sample
        ) VALUES (?, 1, ?, ?, ?, ?)
        ON CONFLICT(tx_id) DO UPDATE SET
            wait_count=tx_accounts.wait_count+1,
            wait_total_units=tx_accounts.wait_total_units+excluded.wait_total_units,
            wait_actual_error_micro=tx_accounts.wait_actual_error_micro
                +excluded.wait_actual_error_micro,
            wait_error_micro=tx_accounts.wait_error_micro+excluded.wait_error_micro,
            updated_sample=excluded.updated_sample
        """,
        (
            row["tx_id"],
            row["rounded_units"],
            row["actual_residual_micro"],
            row["error_bound_micro"],
            resolve_sample_id,
        ),
    )
    connection.execute(
        """
        UPDATE transactions SET
            wait_count=(SELECT wait_count FROM tx_accounts WHERE tx_id=?),
            wait_total_units=(SELECT wait_total_units FROM tx_accounts WHERE tx_id=?),
            wait_actual_error_micro=(SELECT wait_actual_error_micro FROM tx_accounts WHERE tx_id=?),
            wait_error_micro=(SELECT wait_error_micro FROM tx_accounts WHERE tx_id=?)
        WHERE tx_id=?
        """,
        (row["tx_id"], row["tx_id"], row["tx_id"], row["tx_id"], row["tx_id"]),
    )
    connection.execute(
        """
        INSERT INTO tx_error_chains(
            tx_id, ordinal, wait_id, resource_key, holders_json,
            enqueue_sample, resolve_sample, error_bound_micro, source_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["tx_id"],
            ordinal,
            row["wait_id"],
            row["resource_key"],
            row["holder_txs_json"],
            row["enqueue_sample"],
            resolve_sample_id,
            row["error_bound_micro"],
            canonical_json(_wait_source(row)),
        ),
    )
    holder_tx = row["holder_tx"] or "FIFO"
    connection.execute(
        """
        INSERT INTO attribution(
            holder_tx, waiter_tx, index_path, wait_count, total_units,
            actual_error_micro, error_bound_micro
        ) VALUES (?, ?, ?, 1, ?, ?, ?)
        ON CONFLICT(holder_tx, waiter_tx, index_path) DO UPDATE SET
            wait_count=attribution.wait_count+1,
            total_units=attribution.total_units+excluded.total_units,
            actual_error_micro=attribution.actual_error_micro
                +excluded.actual_error_micro,
            error_bound_micro=attribution.error_bound_micro+excluded.error_bound_micro
        """,
        (
            holder_tx,
            row["tx_id"],
            row["index_path"],
            row["rounded_units"],
            row["actual_residual_micro"],
            row["error_bound_micro"],
        ),
    )
    return conversion


def rebuild_ledgers(connection: sqlite3.Connection, watermark_sample: int | None) -> None:
    """Recompute accounts, chains and attribution from committed waits."""

    connection.execute("DELETE FROM tx_error_chains")
    connection.execute("DELETE FROM attribution")
    connection.execute("DELETE FROM tx_accounts")
    transactions = connection.execute("SELECT tx_id FROM transactions ORDER BY tx_id").fetchall()
    for row in transactions:
        connection.execute("INSERT INTO tx_accounts(tx_id) VALUES (?)", (row["tx_id"],))
    waits = connection.execute(
        """
        SELECT * FROM waits
        WHERE status='RESOLVED' AND resolve_sample IS NOT NULL
        ORDER BY tx_id, op_seq, wait_id
        """
    ).fetchall()
    counts: dict[str, int] = {row["tx_id"]: 0 for row in transactions}
    for wait in waits:
        tx_id = wait["tx_id"]
        counts[tx_id] = counts.get(tx_id, 0) + 1
        ordinal = counts[tx_id]
        connection.execute(
            """
            INSERT INTO tx_error_chains(
                tx_id, ordinal, wait_id, resource_key, holders_json,
                enqueue_sample, resolve_sample, error_bound_micro, source_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tx_id,
                ordinal,
                wait["wait_id"],
                wait["resource_key"],
                wait["holder_txs_json"],
                wait["enqueue_sample"],
                wait["resolve_sample"],
                wait["error_bound_micro"],
                canonical_json(_wait_source(wait)),
            ),
        )
        holder_tx = wait["holder_tx"] or "FIFO"
        connection.execute(
            """
            INSERT INTO attribution(
                holder_tx, waiter_tx, index_path, wait_count, total_units,
                actual_error_micro, error_bound_micro
            ) VALUES (?, ?, ?, 1, ?, ?, ?)
            ON CONFLICT(holder_tx, waiter_tx, index_path) DO UPDATE SET
                wait_count=attribution.wait_count+1,
                total_units=attribution.total_units+excluded.total_units,
                actual_error_micro=attribution.actual_error_micro
                    +excluded.actual_error_micro,
                error_bound_micro=attribution.error_bound_micro
                    +excluded.error_bound_micro
            """,
            (
                holder_tx,
                tx_id,
                wait["index_path"],
                wait["rounded_units"],
                wait["actual_residual_micro"],
                wait["error_bound_micro"],
            ),
        )
    for tx_id, count in counts.items():
        account = connection.execute(
            """
            SELECT COUNT(*) AS wait_count,
                   COALESCE(SUM(rounded_units), 0) AS total_units,
                   COALESCE(SUM(actual_residual_micro), 0) AS actual_error,
                   COALESCE(SUM(error_bound_micro), 0) AS error_bound
            FROM waits
            WHERE tx_id=? AND status='RESOLVED'
            """,
            (tx_id,),
        ).fetchone()
        connection.execute(
            """
            UPDATE tx_accounts SET
                wait_count=?, wait_total_units=?, wait_actual_error_micro=?,
                wait_error_micro=?, updated_sample=?
            WHERE tx_id=?
            """,
            (
                account["wait_count"],
                account["total_units"],
                account["actual_error"],
                account["error_bound"],
                watermark_sample,
                tx_id,
            ),
        )
        connection.execute(
            """
            UPDATE transactions SET
                wait_count=?, wait_total_units=?, wait_actual_error_micro=?,
                wait_error_micro=?
            WHERE tx_id=?
            """,
            (
                account["wait_count"],
                account["total_units"],
                account["actual_error"],
                account["error_bound"],
                tx_id,
            ),
        )


def _graph_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    cycles: dict[tuple[str, ...], list[str]] = {}
    nodes = sorted(graph)
    for start in nodes:
        stack: list[str] = []
        on_stack: set[str] = set()

        def walk(node: str) -> None:
            if node in on_stack:
                index = stack.index(node)
                cycle = stack[index:] + [node]
                body = cycle[:-1]
                rotations = [
                    tuple(body[index:] + body[:index]) for index in range(len(body))
                ]
                canonical = min(rotations)
                cycles[canonical] = list(canonical) + [canonical[0]]
                return
            if node in stack:
                return
            stack.append(node)
            on_stack.add(node)
            for neighbor in sorted(graph.get(node, set())):
                if neighbor in graph:
                    walk(neighbor)
            on_stack.remove(node)
            stack.pop()

        walk(start)
    return [cycles[key] for key in sorted(cycles)]


def detect_cycles(connection: sqlite3.Connection) -> list[list[str]]:
    graph: dict[str, set[str]] = {}
    active_ids = {
        row["tx_id"]
        for row in connection.execute(
            "SELECT tx_id FROM transactions WHERE status IN ('RUNNING','WAITING')"
        )
    }
    for queued in queued_entries(connection):
        tx_id = queued["tx_id"]
        if tx_id not in active_ids:
            continue
        neighbors = {
            value
            for value in json.loads(queued["holder_txs_json"])
            if value in active_ids and value != tx_id
        }
        request = resource_from_queue_row(queued)
        for holder in conflicting_holders(connection, request, exclude_tx=tx_id):
            if holder["tx_id"] in active_ids:
                neighbors.add(holder["tx_id"])
        graph[tx_id] = neighbors
    return _graph_cycles(graph)


class DiagnosisEngine:
    def __init__(
        self,
        store: StateStore,
        bundle: SampleBundle,
        run_db_path: str | Path,
        crash_at: str | None = None,
    ) -> None:
        self.store = store
        self.bundle = bundle
        self.connection = store.open()
        self.run_db_path = Path(run_db_path)
        self.crash_at = crash_at
        self.clock = bundle.clock
        self.scale = bundle.config.fixed_scale
        self.tolerance_units = max(1, tolerance_to_units(bundle.config.wait_tolerance_ms, self.scale))
        self.tolerance_micro = decimal_to_micro(bundle.config.wait_tolerance_ms)
        self._failpoint_armed = False

    # ------------------------------------------------------------------
    # Scheduling primitives
    # ------------------------------------------------------------------
    def _arm_failpoint(self, stage: str) -> None:
        if self.crash_at == stage:
            self._failpoint_armed = True

    def _stage(self, tx_id: str, op_seq: int, stage: str, payload: dict[str, Any]) -> int:
        return self.store.stage_intent(tx_id, op_seq, stage, payload)

    def _active_count(self) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) AS n FROM transactions WHERE status IN ('RUNNING','WAITING')"
        ).fetchone()
        return int(row["n"])

    def _dependencies_ready(self, depends: Iterable[str]) -> bool:
        values = list(depends)
        if not values:
            return True
        placeholders = ",".join("?" for _ in values)
        rows = self.connection.execute(
            f"SELECT status FROM transactions WHERE tx_id IN ({placeholders})", values
        ).fetchall()
        return len(rows) == len(values) and all(row["status"] in TERMINAL_STATUSES for row in rows)

    def _start_ready(self, step: int) -> bool:
        cap = self.bundle.config.max_concurrent_transactions
        active = self._active_count()
        if active >= cap:
            return False
        candidates: list[sqlite3.Row] = []
        for row in self.connection.execute(
            "SELECT * FROM transactions WHERE status='NEW' ORDER BY start_order, tx_id"
        ):
            depends = json.loads(row["depends_json"])
            if self._dependencies_ready(depends):
                candidates.append(row)
                if active + len(candidates) >= cap:
                    break
        if not candidates:
            return False
        bundle_id = f"start:{step}"
        intent = self._stage("SCHEDULER", 0, "start", {"count": len(candidates)})
        with self.store.bundle(bundle_id, "SCHEDULER", "START", intent, {"candidates": len(candidates)}) as connection:
            active = connection.execute(
                "SELECT COUNT(*) AS n FROM transactions WHERE status IN ('RUNNING','WAITING')"
            ).fetchone()["n"]
            started = []
            for candidate in candidates:
                if active >= cap:
                    break
                current = connection.execute(
                    "SELECT status FROM transactions WHERE tx_id=?", (candidate["tx_id"],)
                ).fetchone()
                if current is None or current["status"] != "NEW":
                    continue
                sample = next_sample(
                    connection,
                    self.clock,
                    bundle_id,
                    "TX_START",
                    candidate["tx_id"],
                    0,
                )
                connection.execute(
                    """
                    UPDATE transactions
                    SET status='RUNNING', started_sample=?, end_reason=NULL
                    WHERE tx_id=?
                    """,
                    (sample["sample_id"], candidate["tx_id"]),
                )
                connection.execute(
                    """
                    INSERT INTO events(step, event_type, tx_id, op_seq, sample_id, bundle_id, details_json)
                    VALUES (?, 'TX_START', ?, 0, ?, ?, ?)
                    """,
                    (
                        step,
                        candidate["tx_id"],
                        sample["sample_id"],
                        bundle_id,
                        canonical_json({"start_order": candidate["start_order"]}),
                    ),
                )
                started.append(candidate["tx_id"])
                active += 1
        return bool(started)

    def _next_runnable(self) -> sqlite3.Row | None:
        schedule = self.connection.execute("SELECT * FROM schedule_state WHERE id=1").fetchone()
        cursor = int(schedule["rr_start_order"])
        row = self.connection.execute(
            """
            SELECT * FROM transactions
            WHERE status='RUNNING' AND start_order > ?
            ORDER BY start_order, tx_id LIMIT 1
            """,
            (cursor,),
        ).fetchone()
        if row is None:
            row = self.connection.execute(
                """
                SELECT * FROM transactions
                WHERE status='RUNNING'
                ORDER BY start_order, tx_id LIMIT 1
                """
            ).fetchone()
        return row

    def _next_operation(self, tx: sqlite3.Row) -> sqlite3.Row | None:
        return self.connection.execute(
            """
            SELECT * FROM operations
            WHERE tx_id=? AND op_seq=? AND op_version=?
            """,
            (tx["tx_id"], tx["current_op_seq"], tx["version"]),
        ).fetchone()

    def _advance_tx_cursor(self, connection: sqlite3.Connection, tx: sqlite3.Row) -> None:
        connection.execute(
            "UPDATE schedule_state SET rr_start_order=? WHERE id=1",
            (tx["start_order"],),
        )

    # ------------------------------------------------------------------
    # Lock operations
    # ------------------------------------------------------------------
    def _existing_holder(
        self, connection: sqlite3.Connection, tx_id: str, resource_key: str
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM lock_holders WHERE resource_key=? AND tx_id=?",
            (resource_key, tx_id),
        ).fetchone()

    def _request_would_queue(self, tx_id: str, operation: sqlite3.Row) -> bool:
        request = request_from_operation(dict(operation))
        existing = self._existing_holder(self.connection, tx_id, operation["resource_key"])
        if existing is not None:
            if existing["mode"] == "exclusive" or operation["requested_mode"] == "shared":
                if not any(
                    queued["tx_id"] != tx_id
                    and resources_conflict(resource_from_queue_row(queued), request)
                    for queued in queued_entries(self.connection)
                ):
                    return False
            if not conflicting_holders(self.connection, request, exclude_tx=tx_id):
                if not any(
                    queued["tx_id"] != tx_id
                    and resources_conflict(resource_from_queue_row(queued), request)
                    for queued in queued_entries(self.connection)
                ):
                    return False
        for queued in queued_entries(self.connection):
            if queued["tx_id"] != tx_id and resources_conflict(
                resource_from_queue_row(queued), request
            ):
                return True
        return bool(conflicting_holders(self.connection, request, exclude_tx=tx_id))

    def _add_or_upgrade_holder(
        self,
        connection: sqlite3.Connection,
        tx_id: str,
        operation: sqlite3.Row,
        sample_id: int,
        reason: str,
    ) -> None:
        existing = self._existing_holder(connection, tx_id, operation["resource_key"])
        if existing is not None:
            mode = "exclusive" if "exclusive" in (existing["mode"], operation["requested_mode"]) else "shared"
            connection.execute(
                """
                UPDATE lock_holders
                SET mode=?, reentrant_count=reentrant_count+1
                WHERE holder_id=?
                """,
                (mode, existing["holder_id"]),
            )
            connection.execute(
                """
                INSERT INTO lock_reentrancy(tx_id, op_seq, resource_key, reason, sample_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                (tx_id, operation["op_seq"], operation["resource_key"], reason, sample_id),
            )
            return
        connection.execute(
            """
            INSERT INTO lock_holders(
                resource_key, resource_type, table_name, row_id, range_start,
                range_end, mode, tx_id, op_seq, op_version, acquired_sample
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                operation["resource_key"],
                operation["resource_type"],
                operation["table_name"],
                operation["row_id"],
                operation["range_start"],
                operation["range_end"],
                operation["requested_mode"],
                tx_id,
                operation["op_seq"],
                operation["op_version"],
                sample_id,
            ),
        )

    def _grant_ready_waits(
        self,
        connection: sqlite3.Connection,
        bundle_id: str,
        tx_id_for_sample: str,
        step: int,
    ) -> list[str]:
        granted: list[str] = []
        while True:
            head = connection.execute(
                "SELECT * FROM wait_queue WHERE status='QUEUED' ORDER BY queue_no LIMIT 1"
            ).fetchone()
            if head is None:
                break
            request = resource_from_queue_row(head)
            conflicts = conflicting_holders(connection, request, exclude_tx=head["tx_id"])
            if conflicts:
                break
            tx_row = connection.execute(
                "SELECT * FROM transactions WHERE tx_id=?", (head["tx_id"],)
            ).fetchone()
            if tx_row is None or tx_row["status"] not in {"RUNNING", "WAITING"}:
                connection.execute(
                    """
                    UPDATE wait_queue SET status='ORPHAN', resolution='ORPHAN'
                    WHERE wait_id=? AND status='QUEUED'
                    """,
                    (head["wait_id"],),
                )
                continue
            operation = connection.execute(
                """
                SELECT * FROM operations
                WHERE tx_id=? AND op_seq=? AND op_version=?
                """,
                (head["tx_id"], head["op_seq"], head["op_version"]),
            ).fetchone()
            if operation is None:
                connection.execute(
                    """
                    UPDATE wait_queue SET status='ORPHAN', resolution='ORPHAN'
                    WHERE wait_id=? AND status='QUEUED'
                    """,
                    (head["wait_id"],),
                )
                continue
            sample = next_sample(
                connection,
                self.clock,
                bundle_id,
                "WAIT_GRANT",
                head["tx_id"],
                head["op_seq"],
            )
            self._add_or_upgrade_holder(
                connection,
                head["tx_id"],
                operation,
                sample["sample_id"],
                "WAIT_GRANT_UPGRADE"
                if operation["requested_mode"] == "exclusive"
                else "WAIT_GRANT",
            )
            resolve_wait(
                connection,
                head["wait_id"],
                sample["sample_id"],
                "GRANTED",
                "GRANTED",
                self.clock,
            )
            connection.execute(
                """
                UPDATE transactions SET status='RUNNING'
                WHERE tx_id=? AND status='WAITING'
                """,
                (head["tx_id"],),
            )
            connection.execute(
                """
                UPDATE operations SET status='ACQUIRED', acquired_sample=?
                WHERE tx_id=? AND op_seq=? AND op_version=? AND status='QUEUED'
                """,
                (
                    sample["sample_id"],
                    head["tx_id"],
                    head["op_seq"],
                    head["op_version"],
                ),
            )
            connection.execute(
                """
                INSERT INTO events(step, event_type, tx_id, op_seq, sample_id, bundle_id, details_json)
                VALUES (?, 'WAIT_GRANT', ?, ?, ?, ?, ?)
                """,
                (
                    step,
                    head["tx_id"],
                    head["op_seq"],
                    sample["sample_id"],
                    bundle_id,
                    canonical_json({"wait_id": head["wait_id"]}),
                ),
            )
            granted.append(head["tx_id"])
            self._arm_failpoint("grant")
        return granted

    def _head_grantable(self, connection: sqlite3.Connection | None = None) -> bool:
        connection = connection or self.connection
        head = connection.execute(
            "SELECT * FROM wait_queue WHERE status='QUEUED' ORDER BY queue_no LIMIT 1"
        ).fetchone()
        if head is None:
            return False
        request = resource_from_queue_row(head)
        return not conflicting_holders(connection, request, exclude_tx=head["tx_id"])

    def _queue_request(
        self, tx: sqlite3.Row, operation: sqlite3.Row, step: int
    ) -> None:
        bundle_id = f"enqueue:{step}:{tx['tx_id']}:{operation['op_seq']}"
        payload = {
            "wait_id": f"w:{tx['tx_id']}:{operation['op_seq']}:{tx['version']}",
            "resource_key": operation["resource_key"],
        }
        intent = self._stage(tx["tx_id"], operation["op_seq"], "enqueue", payload)
        with self.store.bundle(
            bundle_id, tx["tx_id"], "ENQUEUE", intent, payload
        ) as connection:
            request = request_from_operation(dict(operation))
            existing_holder = self._existing_holder(
                connection, tx["tx_id"], operation["resource_key"]
            )
            queue_reason = (
                "UPGRADE_WAIT"
                if existing_holder is not None
                and existing_holder["mode"] == "shared"
                and operation["requested_mode"] == "exclusive"
                else "LOCK_CONFLICT"
            )
            primary, holders = primary_blocker(connection, request, tx["tx_id"])
            if not holders:
                sample = next_sample(
                    connection,
                    self.clock,
                    bundle_id,
                    "LOCK_ACQUIRE",
                    tx["tx_id"],
                    operation["op_seq"],
                )
                self._add_or_upgrade_holder(
                    connection,
                    tx["tx_id"],
                    operation,
                    sample["sample_id"],
                    "RACE_FREE_GRANT",
                )
                connection.execute(
                    """
                    UPDATE operations SET status='ACQUIRED', acquired_sample=?
                    WHERE tx_id=? AND op_seq=? AND op_version=?
                    """,
                    (
                        sample["sample_id"],
                        tx["tx_id"],
                        operation["op_seq"],
                        operation["op_version"],
                    ),
                )
                self._advance_tx_cursor(connection, tx)
                return
            sample = next_sample(
                connection,
                self.clock,
                bundle_id,
                "WAIT_ENQUEUE",
                tx["tx_id"],
                operation["op_seq"],
            )
            schedule = connection.execute("SELECT * FROM schedule_state WHERE id=1").fetchone()
            queue_no = int(schedule["next_queue_no"])
            connection.execute(
                "UPDATE schedule_state SET next_queue_no=next_queue_no+1 WHERE id=1"
            )
            wait_id = payload["wait_id"]
            holder_tx = primary or (holders[0] if holders else "FIFO")
            holder_json = canonical_json(holders)
            connection.execute(
                """
                INSERT INTO wait_queue(
                    wait_id, queue_no, tx_id, op_seq, op_version, resource_key,
                    requested_mode, holder_tx, holder_txs_json, index_path,
                    enqueue_sample, status, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'QUEUED', ?)
                """,
                (
                    wait_id,
                    queue_no,
                    tx["tx_id"],
                    operation["op_seq"],
                    operation["op_version"],
                    operation["resource_key"],
                    operation["requested_mode"],
                    holder_tx,
                    holder_json,
                    operation["index_path"] or "UNKNOWN",
                    sample["sample_id"],
                    queue_reason,
                ),
            )
            sample_row = sample_by_id(connection, sample["sample_id"])
            connection.execute(
                """
                INSERT INTO waits(
                    wait_id, tx_id, op_seq, op_version, resource_key,
                    holder_tx, holder_txs_json, index_path, enqueue_sample,
                    enqueue_sequence_index, enqueue_raw, scale,
                    raw_units_per_ms, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'WAITING')
                """,
                (
                    wait_id,
                    tx["tx_id"],
                    operation["op_seq"],
                    operation["op_version"],
                    operation["resource_key"],
                    holder_tx,
                    holder_json,
                    operation["index_path"] or "UNKNOWN",
                    sample["sample_id"],
                    sample_row["sequence_index"],
                    sample_row["raw_value"],
                    self.scale,
                    self.clock.raw_units_per_ms,
                ),
            )
            connection.execute(
                "UPDATE transactions SET status='WAITING' WHERE tx_id=?",
                (tx["tx_id"],),
            )
            connection.execute(
                "UPDATE operations SET status='QUEUED' WHERE tx_id=? AND op_seq=? AND op_version=?",
                (tx["tx_id"], operation["op_seq"], operation["op_version"]),
            )
            connection.execute(
                """
                INSERT INTO events(step, event_type, tx_id, op_seq, sample_id, bundle_id, details_json)
                VALUES (?, 'WAIT_ENQUEUE', ?, ?, ?, ?, ?)
                """,
                (
                    step,
                    tx["tx_id"],
                    operation["op_seq"],
                    sample["sample_id"],
                    bundle_id,
                    canonical_json(
                        {
                            "wait_id": wait_id,
                            "queue_no": queue_no,
                            "holders": holders,
                        }
                    ),
                ),
            )
            self._advance_tx_cursor(connection, tx)
        self._arm_failpoint("enqueue")

    def _acquire_immediate(
        self, tx: sqlite3.Row, operation: sqlite3.Row, step: int
    ) -> None:
        bundle_id = f"acquire:{step}:{tx['tx_id']}:{operation['op_seq']}"
        intent = self._stage(
            tx["tx_id"],
            operation["op_seq"],
            "acquire",
            {"resource_key": operation["resource_key"]},
        )
        with self.store.bundle(
            bundle_id,
            tx["tx_id"],
            "LOCK_ACQUIRE",
            intent,
            {"resource_key": operation["resource_key"]},
        ) as connection:
            sample = next_sample(
                connection,
                self.clock,
                bundle_id,
                "LOCK_ACQUIRE",
                tx["tx_id"],
                operation["op_seq"],
            )
            existing = self._existing_holder(
                connection, tx["tx_id"], operation["resource_key"]
            )
            reason = "REENTRANT_REUSE"
            if existing is not None and existing["mode"] == "shared" and operation["requested_mode"] == "exclusive":
                reason = "UPGRADE_IN_PLACE"
            self._add_or_upgrade_holder(
                connection,
                tx["tx_id"],
                operation,
                sample["sample_id"],
                reason,
            )
            connection.execute(
                """
                UPDATE operations
                SET status='ACQUIRED', acquired_sample=?, reentrant_reason=?
                WHERE tx_id=? AND op_seq=? AND op_version=?
                """,
                (
                    sample["sample_id"],
                    reason if existing is not None else None,
                    tx["tx_id"],
                    operation["op_seq"],
                    operation["op_version"],
                ),
            )
            connection.execute(
                """
                INSERT INTO events(step, event_type, tx_id, op_seq, sample_id, bundle_id, details_json)
                VALUES (?, 'LOCK_ACQUIRE', ?, ?, ?, ?, ?)
                """,
                (
                    step,
                    tx["tx_id"],
                    operation["op_seq"],
                    sample["sample_id"],
                    bundle_id,
                    canonical_json({"reason": reason, "reentrant": existing is not None}),
                ),
            )
            self._advance_tx_cursor(connection, tx)

    def _complete_operation(
        self, tx: sqlite3.Row, operation: sqlite3.Row, step: int
    ) -> None:
        bundle_id = f"opdone:{step}:{tx['tx_id']}:{operation['op_seq']}"
        intent = self._stage(
            tx["tx_id"], operation["op_seq"], "operation", {"kind": operation["kind"]}
        )
        with self.store.bundle(
            bundle_id,
            tx["tx_id"],
            "OP_COMPLETE",
            intent,
            {"op_id": operation["op_id"]},
        ) as connection:
            sample = next_sample(
                connection,
                self.clock,
                bundle_id,
                "OP_COMPLETE",
                tx["tx_id"],
                operation["op_seq"],
            )
            connection.execute(
                """
                UPDATE operations SET status='DONE', completed_sample=?
                WHERE tx_id=? AND op_seq=? AND op_version=?
                """,
                (
                    sample["sample_id"],
                    tx["tx_id"],
                    operation["op_seq"],
                    operation["op_version"],
                ),
            )
            connection.execute(
                "UPDATE transactions SET current_op_seq=current_op_seq+1 WHERE tx_id=?",
                (tx["tx_id"],),
            )
            connection.execute(
                """
                INSERT INTO events(step, event_type, tx_id, op_seq, sample_id, bundle_id, details_json)
                VALUES (?, 'OP_COMPLETE', ?, ?, ?, ?, ?)
                """,
                (
                    step,
                    tx["tx_id"],
                    operation["op_seq"],
                    sample["sample_id"],
                    bundle_id,
                    canonical_json({"op_id": operation["op_id"]}),
                ),
            )
            self._advance_tx_cursor(connection, tx)

    def _release_holders(self, connection: sqlite3.Connection, tx_id: str) -> None:
        connection.execute("DELETE FROM lock_holders WHERE tx_id=?", (tx_id,))

    def _apply_writes(self, tx_id: str, version: int) -> None:
        writes = self.connection.execute(
            """
            SELECT * FROM operations
            WHERE tx_id=? AND op_version=? AND kind='write' AND status='DONE'
            ORDER BY op_seq
            """,
            (tx_id, version),
        ).fetchall()
        if not writes:
            return
        connection = sqlite3.connect(str(self.run_db_path), timeout=30)
        try:
            connection.execute("BEGIN IMMEDIATE")
            for operation in writes:
                payload = json.loads(operation["payload_json"])
                table = quote_identifier(payload["table"])
                column = quote_identifier(payload["column"])
                connection.execute(
                    f"UPDATE {table} SET {column}=? WHERE rowid=?",
                    (payload.get("value"), payload["row_id"]),
                )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def _commit_transaction(self, tx: sqlite3.Row, step: int) -> None:
        self._apply_writes(tx["tx_id"], tx["version"])
        bundle_id = f"commit:{step}:{tx['tx_id']}"
        intent = None
        if self._head_grantable():
            intent = self._stage(
                tx["tx_id"], int(tx["current_op_seq"]), "grant", {"after_tx": tx["tx_id"]}
            )
        with self.store.bundle(
            bundle_id,
            tx["tx_id"],
            "TX_COMMIT",
            intent,
            {"tx_id": tx["tx_id"]},
        ) as connection:
            sample = next_sample(
                connection, self.clock, bundle_id, "TX_COMMIT", tx["tx_id"], 0
            )
            connection.execute(
                """
                UPDATE transactions
                SET status='COMMITTED', finished_sample=?, end_reason='COMMITTED'
                WHERE tx_id=?
                """,
                (sample["sample_id"], tx["tx_id"]),
            )
            self._release_holders(connection, tx["tx_id"])
            granted = self._grant_ready_waits(connection, bundle_id, tx["tx_id"], step)
            connection.execute(
                """
                INSERT INTO events(step, event_type, tx_id, op_seq, sample_id, bundle_id, details_json)
                VALUES (?, 'TX_COMMIT', ?, 0, ?, ?, ?)
                """,
                (
                    step,
                    tx["tx_id"],
                    sample["sample_id"],
                    bundle_id,
                    canonical_json({"granted": granted}),
                ),
            )
            self._advance_tx_cursor(connection, tx)

    def _active_rollback(self, tx: sqlite3.Row, operation: sqlite3.Row, step: int) -> None:
        bundle_id = f"rollback:{step}:{tx['tx_id']}"
        intent = self._stage(
            tx["tx_id"], operation["op_seq"], "rollback", {"reason": "active"}
        )
        with self.store.bundle(
            bundle_id,
            tx["tx_id"],
            "ACTIVE_ROLLBACK",
            intent,
            {"reason": "active"},
        ) as connection:
            sample = next_sample(
                connection, self.clock, bundle_id, "ACTIVE_ROLLBACK", tx["tx_id"], operation["op_seq"]
            )
            connection.execute(
                """
                UPDATE operations SET status='ROLLED_BACK', completed_sample=?
                WHERE tx_id=? AND op_seq=? AND op_version=?
                """,
                (
                    sample["sample_id"],
                    tx["tx_id"],
                    operation["op_seq"],
                    operation["op_version"],
                ),
            )
            connection.execute(
                """
                UPDATE transactions
                SET status='ROLLED_BACK_ACTIVE', finished_sample=?,
                    end_reason='ACTIVE_ROLLBACK'
                WHERE tx_id=?
                """,
                (sample["sample_id"], tx["tx_id"]),
            )
            self._release_holders(connection, tx["tx_id"])
            granted = self._grant_ready_waits(connection, bundle_id, tx["tx_id"], step)
            connection.execute(
                """
                INSERT INTO events(step, event_type, tx_id, op_seq, sample_id, bundle_id, details_json)
                VALUES (?, 'ACTIVE_ROLLBACK', ?, ?, ?, ?, ?)
                """,
                (
                    step,
                    tx["tx_id"],
                    operation["op_seq"],
                    sample["sample_id"],
                    bundle_id,
                    canonical_json({"granted": granted}),
                ),
            )
            self._advance_tx_cursor(connection, tx)

    # ------------------------------------------------------------------
    # Timeout and deadlock decisions
    # ------------------------------------------------------------------
    def _timeout_candidate(self) -> sqlite3.Row | None:
        clock_state = self.connection.execute(
            "SELECT next_raw FROM clock_state WHERE id=1"
        ).fetchone()
        prospective_raw = int(clock_state["next_raw"])
        waits = self.connection.execute(
            """
            SELECT w.*, t.budget_ms, t.start_order
            FROM waits w
            JOIN transactions t ON t.tx_id=w.tx_id
            WHERE w.status='WAITING' AND t.status='WAITING'
            ORDER BY t.start_order, w.tx_id
            """
        ).fetchall()
        for wait in waits:
            budget_ticks = int(wait["budget_ms"]) * int(wait["raw_units_per_ms"])
            prior = self.connection.execute(
                """
                SELECT COALESCE(SUM(raw_delta), 0) AS raw_total
                FROM waits
                WHERE tx_id=? AND status='RESOLVED'
                """,
                (wait["tx_id"],),
            ).fetchone()["raw_total"]
            cumulative_raw = int(prior) + (prospective_raw - int(wait["enqueue_raw"]))
            if cumulative_raw >= budget_ticks:
                return wait
        return None

    def _expire_wait(self, wait: sqlite3.Row, step: int) -> None:
        bundle_id = f"timeout:{step}:{wait['tx_id']}:{wait['op_seq']}"
        intent = self._stage(
            wait["tx_id"],
            wait["op_seq"],
            "abandon",
            {"wait_id": wait["wait_id"], "reason": "WAIT_BUDGET_EXCEEDED"},
        )
        with self.store.bundle(
            bundle_id,
            wait["tx_id"],
            "WAIT_TIMEOUT",
            intent,
            {"wait_id": wait["wait_id"]},
        ) as connection:
            sample = next_sample(
                connection,
                self.clock,
                bundle_id,
                "WAIT_TIMEOUT",
                wait["tx_id"],
                wait["op_seq"],
            )
            resolve_wait(
                connection,
                wait["wait_id"],
                sample["sample_id"],
                "ABANDONED",
                "TIMEOUT",
                self.clock,
            )
            connection.execute(
                """
                UPDATE operations SET status='ROLLED_BACK', completed_sample=?
                WHERE tx_id=? AND op_seq=? AND op_version=? AND status='QUEUED'
                """,
                (
                    sample["sample_id"],
                    wait["tx_id"],
                    wait["op_seq"],
                    wait["op_version"],
                ),
            )
            connection.execute(
                """
                UPDATE transactions
                SET status='ABANDONED_TIMEOUT', finished_sample=?,
                    end_reason='WAIT_BUDGET_EXCEEDED'
                WHERE tx_id=?
                """,
                (sample["sample_id"], wait["tx_id"]),
            )
            self._release_holders(connection, wait["tx_id"])
            granted = self._grant_ready_waits(connection, bundle_id, wait["tx_id"], step)
            connection.execute(
                """
                INSERT INTO events(step, event_type, tx_id, op_seq, sample_id, bundle_id, details_json)
                VALUES (?, 'WAIT_TIMEOUT', ?, ?, ?, ?, ?)
                """,
                (
                    step,
                    wait["tx_id"],
                    wait["op_seq"],
                    sample["sample_id"],
                    bundle_id,
                    canonical_json(
                        {
                            "wait_id": wait["wait_id"],
                            "budget_ms": wait["budget_ms"],
                            "granted": granted,
                        }
                    ),
                ),
            )
        self._arm_failpoint("abandon")

    def _tick_waiting(self, step: int) -> None:
        waiting = self.connection.execute(
            "SELECT COUNT(*) AS n FROM transactions WHERE status='WAITING'"
        ).fetchone()["n"]
        if not waiting:
            return
        bundle_id = f"tick:{step}"
        intent = self._stage("SCHEDULER", 0, "tick", {"waiting": waiting})
        with self.store.bundle(
            bundle_id, "SCHEDULER", "HOUSEKEEPING", intent, {"waiting": waiting}
        ) as connection:
            next_sample(
                connection, self.clock, bundle_id, "HOUSEKEEPING", None, None
            )

    def _resolve_deadlock(self, cycle: list[str], step: int) -> bool:
        victims = []
        for tx_id in cycle[:-1]:
            row = self.connection.execute(
                "SELECT * FROM transactions WHERE tx_id=?", (tx_id,)
            ).fetchone()
            if row is not None:
                victims.append(row)
        if not victims:
            return False
        victim = max(victims, key=lambda row: (int(row["start_order"]), row["tx_id"]))
        wait = self.connection.execute(
            "SELECT * FROM waits WHERE tx_id=? AND status='WAITING' ORDER BY op_seq LIMIT 1",
            (victim["tx_id"],),
        ).fetchone()
        bundle_id = f"deadlock:{step}:{victim['tx_id']}"
        intent = self._stage(
            victim["tx_id"],
            0,
            "deadlock",
            {"cycle": cycle, "victim": victim["tx_id"]},
        )
        with self.store.bundle(
            bundle_id,
            victim["tx_id"],
            "DEADLOCK_VICTIM",
            intent,
            {"cycle": cycle},
        ) as connection:
            sample = next_sample(
                connection, self.clock, bundle_id, "DEADLOCK", victim["tx_id"], 0
            )
            if wait is not None:
                resolve_wait(
                    connection,
                    wait["wait_id"],
                    sample["sample_id"],
                    "DEADLOCK_VICTIM",
                    "DEADLOCK_VICTIM",
                    self.clock,
                )
                connection.execute(
                    """
                    UPDATE operations SET status='ROLLED_BACK', completed_sample=?
                    WHERE tx_id=? AND op_seq=? AND op_version=? AND status='QUEUED'
                    """,
                    (
                        sample["sample_id"],
                        wait["tx_id"],
                        wait["op_seq"],
                        wait["op_version"],
                    ),
                )
            connection.execute(
                """
                UPDATE transactions
                SET status='ROLLED_BACK_DEADLOCK', finished_sample=?,
                    end_reason='DEADLOCK_VICTIM'
                WHERE tx_id=?
                """,
                (sample["sample_id"], victim["tx_id"]),
            )
            self._release_holders(connection, victim["tx_id"])
            granted = self._grant_ready_waits(connection, bundle_id, victim["tx_id"], step)
            queue = queue_snapshot(connection)
            locks = lock_snapshot(connection)
            conflicts = validate_holder_set(locks)
            continued = sorted(
                {
                    row["tx_id"]
                    for row in connection.execute(
                        "SELECT tx_id FROM transactions WHERE status IN ('RUNNING','WAITING')"
                    )
                }
            )
            basis = (
                f"highest start_order in cycle: {victim['tx_id']} "
                f"({victim['start_order']})"
            )
            connection.execute(
                """
                INSERT INTO deadlocks(
                    detected_sample, cycle_json, victim_tx, victim_start_order,
                    basis, released_wait_id, continued_json, queue_json,
                    locks_json, revalidated, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sample["sample_id"],
                    canonical_json(cycle),
                    victim["tx_id"],
                    victim["start_order"],
                    basis,
                    wait["wait_id"] if wait is not None else None,
                    canonical_json(continued),
                    canonical_json(queue),
                    canonical_json(locks),
                    0 if conflicts else 1,
                    canonical_json(
                        {
                            "granted_after_resolution": granted,
                            "holder_conflicts_after_resolution": conflicts,
                        }
                    ),
                ),
            )
            connection.execute(
                """
                INSERT INTO events(step, event_type, tx_id, op_seq, sample_id, bundle_id, details_json)
                VALUES (?, 'DEADLOCK_VICTIM', ?, 0, ?, ?, ?)
                """,
                (
                    step,
                    victim["tx_id"],
                    sample["sample_id"],
                    bundle_id,
                    canonical_json(
                        {
                            "cycle": cycle,
                            "granted": granted,
                            "revalidated": not conflicts,
                        }
                    ),
                ),
            )
        return True

    # ------------------------------------------------------------------
    # Run loop and final analysis
    # ------------------------------------------------------------------
    def _process_action(self, tx: sqlite3.Row, step: int) -> None:
        operation = self._next_operation(tx)
        if operation is None:
            self._commit_transaction(tx, step)
            return
        if operation["kind"] == "rollback":
            self._active_rollback(tx, operation, step)
            return
        if operation["status"] == "PENDING":
            if self._request_would_queue(tx["tx_id"], operation):
                self._queue_request(tx, operation, step)
            else:
                self._acquire_immediate(tx, operation, step)
            return
        if operation["status"] == "ACQUIRED":
            self._complete_operation(tx, operation, step)
            return

    def _check_deadlock(self, step: int) -> bool:
        cycles = detect_cycles(self.connection)
        if not cycles:
            return False
        return self._resolve_deadlock(cycles[0], step)

    def _finalize_precision(self, connection: sqlite3.Connection) -> None:
        connection.execute("DELETE FROM precision_flags")
        transactions = connection.execute(
            """
            SELECT * FROM transactions
            WHERE wait_count > 0
            ORDER BY start_order, tx_id
            """
        ).fetchall()
        factor = 10**self.scale
        for left_index, left in enumerate(transactions):
            for right in transactions[left_index + 1 :]:
                difference = abs(int(left["wait_total_units"]) - int(right["wait_total_units"]))
                if difference >= self.tolerance_units:
                    continue
                left_waits = connection.execute(
                    """
                    SELECT * FROM waits
                    WHERE tx_id=? AND status='RESOLVED'
                    ORDER BY enqueue_sample, wait_id
                    """,
                    (left["tx_id"],),
                ).fetchall()
                right_waits = connection.execute(
                    """
                    SELECT * FROM waits
                    WHERE tx_id=? AND status='RESOLVED'
                    ORDER BY enqueue_sample, wait_id
                    """,
                    (right["tx_id"],),
                ).fetchall()
                reasons = {
                    wait["resolution"] for wait in list(left_waits) + list(right_waits)
                }
                reason = "BUDGET_DECISION" if "TIMEOUT" in reasons else "WAIT_ORDER"

                def intermediate(tx_row: sqlite3.Row, waits: list[sqlite3.Row]) -> dict[str, Any]:
                    rounded_units = int(tx_row["wait_total_units"])
                    rounded_micro = rounded_units * 1_000_000 // factor
                    exact_micro = 0
                    actual_micro = 0
                    bound_micro = 0
                    for wait in waits:
                        exact_micro += (
                            int(wait["exact_numerator"]) * 1_000_000
                            // (int(wait["exact_denominator"]) * int(wait["scale"]))
                        )
                        actual_micro += int(wait["actual_residual_micro"])
                        bound_micro += int(wait["error_bound_micro"])
                    return {
                        "status": tx_row["status"],
                        "rounded_units": rounded_units,
                        "rounded_ms": format_units(rounded_units, self.scale),
                        "exact_micro_ms": exact_micro,
                        "exact_ms": format_micro(exact_micro),
                        "rounding_error_micro_ms": actual_micro,
                        "error_bound_micro_ms": bound_micro,
                        "lower_bound_micro_ms": rounded_micro - bound_micro,
                        "upper_bound_micro_ms": rounded_micro + bound_micro,
                        "wait_ids": [wait["wait_id"] for wait in waits],
                        "sample_ids": sorted(
                            {
                                int(wait["enqueue_sample"])
                                for wait in waits
                            }
                            | {
                                int(wait["resolve_sample"])
                                for wait in waits
                                if wait["resolve_sample"] is not None
                            }
                        ),
                    }

                left_value = intermediate(left, list(left_waits))
                right_value = intermediate(right, list(right_waits))
                rounded_samples = sorted(
                    {
                        int(wait["resolve_sample"])
                        for wait in list(left_waits) + list(right_waits)
                        if wait["resolve_sample"] is not None
                        and int(wait["residual_numerator"]) > 0
                    }
                )
                if not rounded_samples:
                    rounded_samples = sorted(
                        {
                            int(wait["resolve_sample"])
                            for wait in list(left_waits) + list(right_waits)
                            if wait["resolve_sample"] is not None
                        }
                    )
                first_sample = rounded_samples[0] if rounded_samples else None
                details = {
                    "difference_ms": format_units(difference, self.scale),
                    "tolerance_ms": format_units(self.tolerance_units, self.scale),
                    "tx_a": left_value,
                    "tx_b": right_value,
                    "explanation": (
                        "rounded wait totals are inside the configured tolerance band"
                    ),
                }
                connection.execute(
                    """
                    INSERT INTO precision_flags(
                        reason, tx_a, tx_b, difference_units, tolerance_units,
                        first_divergent_sample, details_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        reason,
                        left["tx_id"],
                        right["tx_id"],
                        difference,
                        self.tolerance_units,
                        first_sample,
                        canonical_json(details),
                    ),
                )

    def finalize(self) -> None:
        with self.store.bundle(
            "finalize",
            "SYSTEM",
            "FINALIZE",
            None,
            {"engine": "lockdiag"},
        ) as connection:
            watermark_row = connection.execute(
                "SELECT last_sample_id FROM clock_state WHERE id=1"
            ).fetchone()
            watermark = watermark_row["last_sample_id"] if watermark_row else None
            rebuild_ledgers(connection, watermark)
            self._finalize_precision(connection)
            connection.execute("UPDATE schedule_state SET status='COMPLETE' WHERE id=1")
            connection.execute("UPDATE run_info SET status='COMPLETE' WHERE id=1")
            unresolved = connection.execute(
                "SELECT COUNT(*) AS n FROM waits WHERE status='WAITING'"
            ).fetchone()["n"]
            if unresolved:
                raise DiagnosticError(
                    E_STATE_CORRUPT,
                    f"cannot finalize with {unresolved} unresolved wait records",
                )

    def _mark_not_started(self, step: int) -> list[str]:
        rows = self.connection.execute(
            "SELECT tx_id FROM transactions WHERE status='NEW' ORDER BY start_order, tx_id"
        ).fetchall()
        if not rows:
            return []
        tx_ids = [row["tx_id"] for row in rows]
        bundle_id = f"not-started:{step}"
        with self.store.bundle(
            bundle_id,
            "SCHEDULER",
            "NOT_STARTED_CONCURRENCY",
            None,
            {"transactions": tx_ids},
        ) as connection:
            for tx_id in tx_ids:
                connection.execute(
                    """
                    UPDATE transactions
                    SET status='NOT_STARTED_CONCURRENCY',
                        end_reason='SCHEDULER_STEP_BUDGET_BEFORE_START'
                    WHERE tx_id=? AND status='NEW'
                    """,
                    (tx_id,),
                )
            connection.execute(
                """
                INSERT INTO events(step, event_type, tx_id, op_seq, sample_id, bundle_id, details_json)
                VALUES (?, 'NOT_STARTED_CONCURRENCY', NULL, 0, NULL, ?, ?)
                """,
                (step, bundle_id, canonical_json({"transactions": tx_ids})),
            )
        return tx_ids

    def run(self) -> dict[str, Any]:
        current_status = self.connection.execute(
            "SELECT status FROM run_info WHERE id=1"
        ).fetchone()["status"]
        remaining_at_start = self.connection.execute(
            """
            SELECT COUNT(*) AS n FROM transactions
            WHERE status NOT IN (
                'COMMITTED','ABANDONED_TIMEOUT','ROLLED_BACK_ACTIVE',
                'ROLLED_BACK_DEADLOCK','NOT_STARTED_CONCURRENCY','INTERRUPTED'
            )
            """
        ).fetchone()["n"]
        if remaining_at_start == 0:
            if current_status != "COMPLETE":
                self.finalize()
            return self.store.status_payload()
        self.connection.execute("UPDATE run_info SET status='RUNNING' WHERE id=1")
        max_steps = self.bundle.config.max_scheduler_steps
        with self.store.transaction() as connection:
            schedule = connection.execute("SELECT * FROM schedule_state WHERE id=1").fetchone()
            start_step = int(schedule["steps"])
            connection.execute("UPDATE run_info SET status='RUNNING' WHERE id=1")
        paused = False
        for offset in range(max_steps):
            step = start_step + offset
            self._start_ready(step)
            tx = self._next_runnable()
            if tx is not None:
                self._process_action(tx, step)
            if step % self.bundle.config.deadlock_check_period == 0:
                self._check_deadlock(step)
            candidate = self._timeout_candidate()
            if candidate is not None:
                self._expire_wait(candidate, step)
            else:
                self._tick_waiting(step)
            with self.store.transaction() as connection:
                connection.execute(
                    "UPDATE schedule_state SET steps=? WHERE id=1", (step + 1,)
                )
            if self._failpoint_armed:
                os._exit(97)
            remaining = self.connection.execute(
                """
                SELECT COUNT(*) AS n FROM transactions
                WHERE status NOT IN (
                    'COMMITTED','ABANDONED_TIMEOUT','ROLLED_BACK_ACTIVE',
                    'ROLLED_BACK_DEADLOCK','NOT_STARTED_CONCURRENCY','INTERRUPTED'
                )
                """
            ).fetchone()["n"]
            if remaining == 0:
                break
        else:
            paused = True
        if paused:
            self._mark_not_started(start_step + max_steps)
            self.connection.execute("UPDATE run_info SET status='PAUSED' WHERE id=1")
            self.connection.execute("UPDATE schedule_state SET status='PAUSED' WHERE id=1")
        else:
            self.finalize()
        return self.store.status_payload()


def recover_state(store: StateStore, bundle: SampleBundle, run_db_path: str | Path) -> dict[str, Any]:
    """Recover committed state and discard only staged/half-written registrations."""

    connection = store.open()
    scale = bundle.config.fixed_scale
    with store.transaction() as connection:
        staged_rows = connection.execute(
            "SELECT * FROM recovery_intents WHERE status='STAGED' ORDER BY intent_id"
        ).fetchall()
        staged_removed = len(staged_rows)
        connection.execute("DELETE FROM recovery_intents WHERE status='STAGED'")
        terminal = tuple(
            (
                "COMMITTED",
                "ABANDONED_TIMEOUT",
                "ROLLED_BACK_ACTIVE",
                "ROLLED_BACK_DEADLOCK",
                "NOT_STARTED_CONCURRENCY",
            )
        )
        holders = connection.execute("SELECT * FROM lock_holders").fetchall()
        orphan_locks = 0
        for holder in holders:
            tx = connection.execute(
                "SELECT status FROM transactions WHERE tx_id=?", (holder["tx_id"],)
            ).fetchone()
            operation = connection.execute(
                """
                SELECT status FROM operations
                WHERE tx_id=? AND op_seq=? AND op_version=?
                """,
                (holder["tx_id"], holder["op_seq"], holder["op_version"]),
            ).fetchone()
            if (
                tx is None
                or tx["status"] in terminal
                or operation is None
                or operation["status"] not in {"ACQUIRED", "DONE"}
            ):
                connection.execute(
                    "DELETE FROM lock_holders WHERE holder_id=?", (holder["holder_id"],)
                )
                orphan_locks += 1
        orphan_waits = 0
        queue_rows = connection.execute(
            "SELECT * FROM wait_queue WHERE status='QUEUED' ORDER BY queue_no"
        ).fetchall()
        for queued in queue_rows:
            tx = connection.execute(
                "SELECT status FROM transactions WHERE tx_id=?", (queued["tx_id"],)
            ).fetchone()
            operation = connection.execute(
                """
                SELECT status FROM operations
                WHERE tx_id=? AND op_seq=? AND op_version=?
                """,
                (queued["tx_id"], queued["op_seq"], queued["op_version"]),
            ).fetchone()
            if (
                tx is None
                or tx["status"] in terminal
                or operation is None
                or operation["status"] != "QUEUED"
            ):
                connection.execute(
                    "UPDATE wait_queue SET status='ORPHAN', resolution='RECOVERY_CLEANUP' WHERE wait_id=?",
                    (queued["wait_id"],),
                )
                connection.execute(
                    "DELETE FROM waits WHERE wait_id=? AND status='WAITING'",
                    (queued["wait_id"],),
                )
                orphan_waits += 1
        watermark_row = connection.execute(
            "SELECT last_sample_id FROM clock_state WHERE id=1"
        ).fetchone()
        watermark = watermark_row["last_sample_id"] if watermark_row else None
        rebuild_ledgers(connection, watermark)
        snapshot = queue_snapshot(connection)
        details = {
            "staged_intents": [dict(row) for row in staged_rows],
            "terminal_statuses": list(terminal),
        }
        connection.execute(
            """
            INSERT INTO recovery_runs(
                staged_removed, orphan_locks_removed, orphan_waits_removed,
                queue_json, account_rebuilt, details_json
            ) VALUES (?, ?, ?, ?, 1, ?)
            """,
            (
                staged_removed,
                orphan_locks,
                orphan_waits,
                canonical_json(snapshot),
                canonical_json(details),
            ),
        )
    return {
        "staged_removed": staged_removed,
        "orphan_locks_removed": orphan_locks,
        "orphan_waits_removed": orphan_waits,
        "queue": snapshot,
    }
