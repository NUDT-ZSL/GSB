"""Deterministic logical-time lock scheduler and diagnostics."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Iterable

from .errors import fail
from .fixed import Conversion, convert_wait, fraction_text
from .model import Operation, Scenario, Transaction
from .storage import Store
from .util import canonical_json

ACTIVE_STATUSES = frozenset({"RUNNING", "WAITING"})
@dataclass(frozen=True, slots=True)
class Target:
    table_name: str
    kind: str
    row_value: Any = None
    range_lo: Any = None
    range_hi: Any = None
    resource_key: str = ""


@dataclass(frozen=True, slots=True)
class CrashPlan:
    phase: str
    tick: int
    occurrence: int = 1


def _json_value(value: Any) -> str:
    return canonical_json(value)


def _decode(value: str | None) -> Any:
    return None if value is None else json.loads(value)


def operation_target(operation: Operation) -> Target:
    if operation.row_value is not None:
        row_json = _json_value(operation.row_value)
        return Target(
            table_name=operation.table,
            kind="row",
            row_value=operation.row_value,
            resource_key=f"row:{operation.table}:{row_json}",
        )
    lo_json = _json_value(operation.range_lo)
    hi_json = _json_value(operation.range_hi)
    return Target(
        table_name=operation.table,
        kind="range",
        range_lo=operation.range_lo,
        range_hi=operation.range_hi,
        resource_key=f"range:{operation.table}:{lo_json}:{hi_json}",
    )


def _queue_target(row: sqlite3.Row | dict[str, Any]) -> Target:
    row_value = _decode(row["row_value"])
    range_lo = _decode(row["range_lo"])
    range_hi = _decode(row["range_hi"])
    return Target(
        table_name=row["table_name"],
        kind=row["resource_kind"],
        row_value=row_value,
        range_lo=range_lo,
        range_hi=range_hi,
        resource_key=row["resource_key"],
    )


def _holder_target(row: sqlite3.Row | dict[str, Any]) -> Target:
    return _queue_target(row)


def _compare(left: Any, right: Any) -> int | None:
    try:
        if left < right:
            return -1
        if left > right:
            return 1
        return 0
    except TypeError:
        return None


def _row_in_range(row_value: Any, lo: Any, hi: Any) -> bool:
    low = _compare(row_value, lo)
    high = _compare(row_value, hi)
    if low is None or high is None:
        return False
    return low >= 0 and high <= 0


def _ranges_overlap(a_lo: Any, a_hi: Any, b_lo: Any, b_hi: Any) -> bool:
    if _compare(a_hi, b_lo) is None:
        return False
    if _compare(a_hi, b_lo) < 0:
        return False
    if _compare(b_hi, a_lo) < 0:
        return False
    return True


def targets_overlap(left: Target, right: Target) -> bool:
    if left.table_name != right.table_name:
        return False
    if left.kind == "row" and right.kind == "row":
        comparison = _compare(left.row_value, right.row_value)
        return comparison == 0
    if left.kind == "range" and right.kind == "range":
        return _ranges_overlap(
            left.range_lo,
            left.range_hi,
            right.range_lo,
            right.range_hi,
        )
    if left.kind == "row" and right.kind == "range":
        return _row_in_range(left.row_value, right.range_lo, right.range_hi)
    return _row_in_range(right.row_value, left.range_lo, left.range_hi)


def target_covers(covering: Target, requested: Target) -> bool:
    if covering.table_name != requested.table_name:
        return False
    if covering.kind == "row":
        return (
            requested.kind == "row"
            and _compare(covering.row_value, requested.row_value) == 0
        )
    if requested.kind == "row":
        return _row_in_range(requested.row_value, covering.range_lo, covering.range_hi)
    return (
        _compare(covering.range_lo, requested.range_lo) <= 0
        and _compare(covering.range_hi, requested.range_hi) >= 0
    )


def modes_conflict(left: str, right: str) -> bool:
    return left == "X" or right == "X"


class Engine:
    """Single deterministic coordinator; every tick is one atomic SQLite commit."""

    def __init__(
        self,
        scenario: Scenario,
        store: Store,
        crash_plan: CrashPlan | None = None,
    ):
        self.scenario = scenario
        self.store = store
        self.crash_plan = crash_plan
        self._crash_counts: dict[str, int] = {}
        self._tx_by_key = {tx.tx_key: tx for tx in scenario.transactions}

    def initialize(self, run_id: str) -> None:
        self.store.initialize(self.scenario, run_id)

    def run(self, max_ticks: int | None = None) -> dict[str, Any]:
        ticks = 0
        while max_ticks is None or ticks < max_ticks:
            if not self.run_tick():
                break
            ticks += 1
        return self.store.status(self.scenario.config.error_budget)

    def run_tick(self) -> bool:
        with self.store.write_tx(snapshot=False) as conn:
            state = conn.execute("SELECT * FROM run_state WHERE singleton=1").fetchone()
            if state is None:
                fail("E_NO_RUN", "run_state is missing")
            if state["run_status"] in {"COMPLETE", "HORIZON"}:
                return False
            tick = int(state["clock_ms"])
            transactions = self._transactions(conn)
            if self._all_done(transactions):
                conn.execute(
                    "UPDATE run_state SET run_status='COMPLETE',"
                    " watermark_tick=?, clock_ms=? WHERE singleton=1",
                    (tick, tick),
                )
                self._log_event(
                    conn,
                    tick=tick,
                    tx_key=None,
                    event_type="RUN_COMPLETED",
                    details={"logical_clock_ms": tick},
                )
                self.store._snapshot_ledger(conn)
                return False

            horizon = self._horizon_tick()
            if tick >= horizon:
                self._finalize_horizon(conn, tick)
                self.store._snapshot_ledger(conn)
                return False

            self._start_pending(conn, transactions, tick)
            transactions = self._transactions(conn)
            self._expire_waiters(conn, transactions, tick)
            transactions = self._transactions(conn)
            self._check_deadlocks(conn, transactions, tick)
            transactions = self._transactions(conn)
            self._execute_ready(conn, transactions, tick)
            transactions = self._transactions(conn)
            self._process_queues(conn, tick)
            transactions = self._transactions(conn)
            self._maybe_crash("operation", tick)

            if self._all_done(transactions):
                conn.execute(
                    "UPDATE run_state SET run_status='COMPLETE',"
                    " watermark_tick=?, clock_ms=?, round_no=round_no+1"
                    " WHERE singleton=1",
                    (tick, tick),
                )
                self._log_event(
                    conn,
                    tick=tick,
                    tx_key=None,
                    event_type="RUN_COMPLETED",
                    details={"logical_clock_ms": tick},
                )
                self.store._snapshot_ledger(conn)
                return False

            next_tick = self._next_tick(conn, tick)
            if next_tick >= horizon:
                self._finalize_horizon(conn, horizon)
                self.store._snapshot_ledger(conn)
                return False
            conn.execute(
                "UPDATE run_state SET clock_ms=?, watermark_tick=?,"
                " round_no=round_no+1 WHERE singleton=1",
                (next_tick, tick),
            )
            self._log_event(
                conn,
                tick=tick,
                tx_key=None,
                event_type="TICK_COMMITTED",
                details={"next_tick": next_tick, "watermark_tick": tick},
            )
            self.store._snapshot_ledger(conn)
            return True

    def _maybe_crash(self, phase: str, tick: int) -> None:
        plan = self.crash_plan
        if plan is None or plan.phase != phase or plan.tick != tick:
            return
        count = self._crash_counts.get(phase, 0) + 1
        self._crash_counts[phase] = count
        if count >= plan.occurrence:
            os._exit(137)

    def _transactions(self, conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
        return {
            row["tx_key"]: row
            for row in conn.execute("SELECT * FROM transactions ORDER BY tx_no")
        }

    def _all_done(self, transactions: dict[str, sqlite3.Row]) -> bool:
        return all(
            row["status"]
            in {"COMMITTED", "TIMEOUT", "ABORTED", "SACRIFICED"}
            for row in transactions.values()
        )

    def _horizon_tick(self) -> int:
        if self.scenario.config.logical_horizon_ms is not None:
            return self.scenario.config.logical_horizon_ms
        return self.scenario.config.max_rounds * self.scenario.config.clock_step_ms

    def _next_tick(self, conn: sqlite3.Connection, tick: int) -> int:
        candidates = [tick + self.scenario.config.clock_step_ms]
        deadline = conn.execute(
            "SELECT MIN(waiting_deadline_tick) AS deadline FROM transactions"
            " WHERE status='WAITING' AND waiting_deadline_tick IS NOT NULL"
        ).fetchone()["deadline"]
        if deadline is not None and int(deadline) > tick:
            candidates.append(int(deadline))
        check_period = self.scenario.config.deadlock_check_period_ms
        candidates.append(tick + check_period)
        candidates.append(self._horizon_tick())
        return min(candidates)

    def _log_event(
        self,
        conn: sqlite3.Connection,
        *,
        tick: int,
        tx_key: str | None,
        event_type: str,
        details: dict[str, Any],
    ) -> int:
        return self.store._log_event(
            conn,
            tick=tick,
            tx_key=tx_key,
            event_type=event_type,
            details=details,
        )

    def _start_pending(
        self,
        conn: sqlite3.Connection,
        transactions: dict[str, sqlite3.Row],
        tick: int,
    ) -> None:
        active = sum(
            1 for row in transactions.values() if row["status"] in ACTIVE_STATUSES
        )
        capacity = self.scenario.config.max_concurrency - active
        if capacity <= 0:
            return
        for row in transactions.values():
            if capacity <= 0:
                break
            if row["status"] != "NOT_STARTED":
                continue
            conn.execute(
                "UPDATE transactions SET status='RUNNING', started_tick=?"
                " WHERE tx_key=?",
                (tick, row["tx_key"]),
            )
            self._log_event(
                conn,
                tick=tick,
                tx_key=row["tx_key"],
                event_type="TX_STARTED",
                details={"tx_no": row["tx_no"], "started_tick": tick},
            )
            capacity -= 1

    def _expire_waiters(
        self,
        conn: sqlite3.Connection,
        transactions: dict[str, sqlite3.Row],
        tick: int,
    ) -> None:
        waiting = conn.execute(
            "SELECT q.*, t.waiting_deadline_tick, t.tx_no, t.waited_ms,"
            " t.wait_budget_ms"
            " FROM lock_queue q JOIN transactions t ON t.tx_key=q.tx_key"
            " WHERE q.status='WAITING' AND t.status='WAITING'"
            " AND q.deadline_tick<=? ORDER BY q.deadline_tick, q.enqueue_seq",
            (tick,),
        ).fetchall()
        if not waiting:
            return
        candidates = self._all_live_candidates(conn, tick)
        for queue_row in waiting:
            tx_key = queue_row["tx_key"]
            deadline = int(queue_row["deadline_tick"])
            complete_tick = min(deadline, tick)
            held_before = self._held_resource_keys(conn, tx_key)
            registration_seq, conversion = self._complete_wait(
                conn,
                queue_row,
                complete_tick,
                outcome="TIMEOUT",
            )
            conn.execute(
                "UPDATE lock_queue SET status='TIMEOUT', resolved_tick=?,"
                " resolution='WAIT_BUDGET_EXCEEDED' WHERE queue_id=?",
                (complete_tick, queue_row["queue_id"]),
            )
            basis, sensitive, sensitivity = self._decision_diagnostics(
                conn,
                candidates,
                decision_tick=complete_tick,
                kind="WAIT_TIMEOUT",
            )
            self._record_decision(
                conn,
                kind="WAIT_TIMEOUT",
                tick=complete_tick,
                tx_key=tx_key,
                reason="WAIT_BUDGET_EXCEEDED",
                cycle=None,
                basis={
                    "transaction": tx_key,
                    "transaction_no": queue_row["tx_no"],
                    "enqueue_tick": queue_row["enqueue_tick"],
                    "deadline_tick": deadline,
                    "waited_before_ms": int(queue_row["waited_ms"]),
                    "wait_budget_ms": int(queue_row["wait_budget_ms"]),
                    "remaining_budget_ms": max(
                        0,
                        int(queue_row["wait_budget_ms"])
                        - int(queue_row["waited_ms"]),
                    ),
                    "raw_wait_ms": conversion.raw_ms,
                    "wait_fixed_units": conversion.fixed_units,
                    "discarded_remainder": fraction_text(conversion.remainder),
                    "error_upper_bound": fraction_text(conversion.error_units),
                    "held_locks_released": held_before,
                    "candidates": basis,
                },
                ledger_sensitive=sensitive,
                sensitivity=sensitivity,
            )
            self._terminate_tx(
                conn,
                tx_key,
                status="TIMEOUT",
                reason="WAIT_BUDGET_EXCEEDED",
                tick=complete_tick,
            )
            self._maybe_crash("abandon", tick)

    def _execute_ready(
        self,
        conn: sqlite3.Connection,
        transactions: dict[str, sqlite3.Row],
        tick: int,
    ) -> None:
        for row in transactions.values():
            current = conn.execute(
                "SELECT * FROM transactions WHERE tx_key=?", (row["tx_key"],)
            ).fetchone()
            if current["status"] != "RUNNING":
                continue
            tx = self._tx_by_key[current["tx_key"]]
            next_op = int(current["next_op"])
            rollback_after = current["rollback_after_ops"]
            if rollback_after is not None and next_op >= int(rollback_after):
                held_before = self._held_resource_keys(conn, tx.tx_key)
                self._record_decision(
                    conn,
                    kind="ACTIVE_ROLLBACK",
                    tick=tick,
                    tx_key=tx.tx_key,
                    reason="VOLUNTARY_ROLLBACK",
                    cycle=None,
                    basis={
                        "transaction": tx.tx_key,
                        "transaction_no": tx.tx_no,
                        "completed_ops": next_op,
                        "rollback_after_ops": int(rollback_after),
                        "retry_limit": tx.max_retries,
                        "held_locks_released": held_before,
                    },
                    ledger_sensitive=False,
                    sensitivity=None,
                )
                self._terminate_tx(
                    conn,
                    tx.tx_key,
                    status="ABORTED",
                    reason="VOLUNTARY_ROLLBACK",
                    tick=tick,
                )
                self._maybe_crash("abandon", tick)
                continue
            if next_op >= len(tx.operations):
                self._commit_tx(conn, current, tick)
                continue
            operation = tx.operations[next_op]
            target = operation_target(operation)
            acquired = self._try_acquire(
                conn,
                current,
                operation,
                target,
                tick,
            )
            if not acquired:
                self._enqueue(
                    conn,
                    current,
                    operation,
                    target,
                    tick,
                )

    def _try_acquire(
        self,
        conn: sqlite3.Connection,
        tx_row: sqlite3.Row,
        operation: Operation,
        target: Target,
        tick: int,
    ) -> bool:
        if self._covered_by_own_holder(
            conn, tx_row["tx_key"], target, operation.mode
        ):
            self._complete_operation(
                conn,
                tx_row,
                operation,
                tick,
                outcome="REUSED",
            )
            return True
        if self._holders_conflict(conn, target, operation.mode, exclude_tx=tx_row["tx_key"]):
            return False
        if self._earlier_queue_conflict(conn, target, operation.mode, None):
            return False
        if operation.mode == "X":
            upgrade = conn.execute(
                "SELECT * FROM lock_holders WHERE tx_key=? AND resource_key=?"
                " AND mode='S'",
                (tx_row["tx_key"], target.resource_key),
            ).fetchone()
            if upgrade is not None:
                conn.execute(
                    "UPDATE lock_holders SET mode='X', acquire_tick=?, op_index=?"
                    " WHERE holder_id=?",
                    (tick, operation.index, upgrade["holder_id"]),
                )
                self._maybe_crash("grant", tick)
                self._complete_operation(
                    conn,
                    tx_row,
                    operation,
                    tick,
                    outcome="UPGRADED",
                )
                return True
        holder_id = self.store._next(conn, "next_holder_id")
        acquire_seq = self.store._next(conn, "next_enqueue_seq")
        conn.execute(
            "INSERT INTO lock_holders("
            "holder_id, table_name, resource_kind, resource_key, row_value,"
            " range_lo, range_hi, tx_key, mode, acquire_seq, acquire_tick, op_index"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                holder_id,
                target.table_name,
                target.kind,
                target.resource_key,
                _json_value(target.row_value) if target.kind == "row" else None,
                _json_value(target.range_lo) if target.kind == "range" else None,
                _json_value(target.range_hi) if target.kind == "range" else None,
                tx_row["tx_key"],
                operation.mode,
                acquire_seq,
                tick,
                operation.index,
            ),
        )
        self._log_event(
            conn,
            tick=tick,
            tx_key=tx_row["tx_key"],
            event_type="LOCK_ACQUIRED",
            details={
                "resource_key": target.resource_key,
                "mode": operation.mode,
                "holder_id": holder_id,
                "acquire_seq": acquire_seq,
            },
        )
        self._maybe_crash("grant", tick)
        self._complete_operation(
            conn,
            tx_row,
            operation,
            tick,
            outcome="ACQUIRED",
        )
        return True

    def _enqueue(
        self,
        conn: sqlite3.Connection,
        tx_row: sqlite3.Row,
        operation: Operation,
        target: Target,
        tick: int,
    ) -> None:
        existing = conn.execute(
            "SELECT queue_id FROM lock_queue WHERE tx_key=? AND op_index=?"
            " AND operation_version=?",
            (
                tx_row["tx_key"],
                operation.index,
                tx_row["operation_version"],
            ),
        ).fetchone()
        if existing is not None:
            return
        queue_id = self.store._next(conn, "next_queue_id")
        enqueue_seq = self.store._next(conn, "next_enqueue_seq")
        remaining_budget = max(
            0, int(tx_row["wait_budget_ms"]) - int(tx_row["waited_ms"])
        )
        deadline = tick + remaining_budget
        conn.execute(
            "INSERT INTO lock_queue("
            "queue_id, enqueue_seq, table_name, resource_kind, resource_key,"
            " row_value, range_lo, range_hi, tx_key, op_index, operation_version,"
            " mode, enqueue_tick, deadline_tick, status, resolved_tick, resolution"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'WAITING', NULL, NULL)",
            (
                queue_id,
                enqueue_seq,
                target.table_name,
                target.kind,
                target.resource_key,
                _json_value(target.row_value) if target.kind == "row" else None,
                _json_value(target.range_lo) if target.kind == "range" else None,
                _json_value(target.range_hi) if target.kind == "range" else None,
                tx_row["tx_key"],
                operation.index,
                tx_row["operation_version"],
                operation.mode,
                tick,
                deadline,
            ),
        )
        conn.execute(
            "UPDATE transactions SET status='WAITING', waiting_deadline_tick=?,"
            " waiting_queue_id=? WHERE tx_key=?",
            (deadline, queue_id, tx_row["tx_key"]),
        )
        self._log_event(
            conn,
            tick=tick,
            tx_key=tx_row["tx_key"],
            event_type="WAIT_ENQUEUED",
            details={
                "queue_id": queue_id,
                "enqueue_seq": enqueue_seq,
                "resource_key": target.resource_key,
                "mode": operation.mode,
                "deadline_tick": deadline,
            },
        )
        self._maybe_crash("enqueue", tick)

    def _covered_by_own_holder(
        self,
        conn: sqlite3.Connection,
        tx_key: str,
        target: Target,
        requested_mode: str,
    ) -> bool:
        holders = conn.execute(
            "SELECT * FROM lock_holders WHERE tx_key=?", (tx_key,)
        ).fetchall()
        for holder in holders:
            held_target = _holder_target(holder)
            if holder["mode"] == "X" and target_covers(held_target, target):
                return True
            if (
                requested_mode == "S"
                and holder["mode"] == "S"
                and target_covers(held_target, target)
            ):
                return True
        return False

    def _holders_conflict(
        self,
        conn: sqlite3.Connection,
        target: Target,
        mode: str,
        *,
        exclude_tx: str | None = None,
    ) -> bool:
        for holder in conn.execute("SELECT * FROM lock_holders"):
            if exclude_tx is not None and holder["tx_key"] == exclude_tx:
                continue
            if not modes_conflict(mode, holder["mode"]):
                continue
            if targets_overlap(target, _holder_target(holder)):
                return True
        return False

    def _earlier_queue_conflict(
        self,
        conn: sqlite3.Connection,
        target: Target,
        mode: str,
        queue_id: int | None,
    ) -> bool:
        if queue_id is None:
            rows = conn.execute(
                "SELECT * FROM lock_queue WHERE status='WAITING'"
                " ORDER BY enqueue_seq"
            ).fetchall()
        else:
            own = conn.execute(
                "SELECT enqueue_seq FROM lock_queue WHERE queue_id=?", (queue_id,)
            ).fetchone()
            if own is None:
                return False
            rows = conn.execute(
                "SELECT * FROM lock_queue WHERE status='WAITING'"
                " AND enqueue_seq<? ORDER BY enqueue_seq",
                (own["enqueue_seq"],),
            ).fetchall()
        for waiting in rows:
            if not modes_conflict(mode, waiting["mode"]):
                continue
            if targets_overlap(target, _queue_target(waiting)):
                return True
        return False

    def _complete_operation(
        self,
        conn: sqlite3.Connection,
        tx_row: sqlite3.Row,
        operation: Operation,
        tick: int,
        *,
        outcome: str,
        wait_registration_seq: int | None = None,
    ) -> None:
        existing = conn.execute(
            "SELECT outcome FROM op_commits WHERE tx_key=? AND op_index=?"
            " AND operation_version=?",
            (
                tx_row["tx_key"],
                operation.index,
                tx_row["operation_version"],
            ),
        ).fetchone()
        if existing is not None:
            return
        next_op = operation.index + 1
        tx = self._tx_by_key[tx_row["tx_key"]]
        conn.execute(
            "INSERT INTO op_commits("
            "tx_key, op_index, operation_version, op_id, operation_kind,"
            " resource_key, completed_tick, outcome, wait_registration_seq"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(tx_key, op_index, operation_version) DO NOTHING",
            (
                tx_row["tx_key"],
                operation.index,
                tx_row["operation_version"],
                operation.op_id,
                operation.kind,
                target_key_for_operation(operation),
                tick,
                outcome,
                wait_registration_seq,
            ),
        )
        conn.execute(
            "UPDATE transactions SET next_op=?, status='RUNNING',"
            " waiting_deadline_tick=NULL, waiting_queue_id=NULL WHERE tx_key=?",
            (next_op, tx_row["tx_key"]),
        )
        self._log_event(
            conn,
            tick=tick,
            tx_key=tx_row["tx_key"],
            event_type="OPERATION_COMMITTED",
            details={
                "op_index": operation.index,
                "op_id": operation.op_id,
                "kind": operation.kind,
                "outcome": outcome,
                "next_op": next_op,
            },
        )
        current = conn.execute(
            "SELECT * FROM transactions WHERE tx_key=?", (tx_row["tx_key"],)
        ).fetchone()
        if next_op >= len(tx.operations):
            self._commit_tx(conn, current, tick)

    def _commit_tx(
        self,
        conn: sqlite3.Connection,
        tx_row: sqlite3.Row,
        tick: int,
    ) -> None:
        if tx_row["status"] == "COMMITTED":
            return
        conn.execute(
            "UPDATE transactions SET status='COMMITTED', reason=NULL, ended_tick=?,"
            " waiting_deadline_tick=NULL, waiting_queue_id=NULL WHERE tx_key=?",
            (tick, tx_row["tx_key"]),
        )
        self._release_holders(conn, tx_row["tx_key"])
        self._log_event(
            conn,
            tick=tick,
            tx_key=tx_row["tx_key"],
            event_type="TX_COMMITTED",
            details={"ended_tick": tick},
        )

    def _release_holders(self, conn: sqlite3.Connection, tx_key: str) -> list[str]:
        tables = [
            row["table_name"]
            for row in conn.execute(
                "SELECT DISTINCT table_name FROM lock_holders WHERE tx_key=?",
                (tx_key,),
            )
        ]
        conn.execute("DELETE FROM lock_holders WHERE tx_key=?", (tx_key,))
        return tables

    def _terminate_tx(
        self,
        conn: sqlite3.Connection,
        tx_key: str,
        *,
        status: str,
        reason: str,
        tick: int,
    ) -> None:
        conn.execute(
            "UPDATE transactions SET status=?, reason=?, ended_tick=?,"
            " waiting_deadline_tick=NULL, waiting_queue_id=NULL WHERE tx_key=?",
            (status, reason, tick, tx_key),
        )
        conn.execute(
            "UPDATE lock_queue SET status='CANCELLED', resolved_tick=?,"
            " resolution=? WHERE tx_key=? AND status='WAITING'",
            (tick, reason, tx_key),
        )
        self._release_holders(conn, tx_key)
        self._log_event(
            conn,
            tick=tick,
            tx_key=tx_key,
            event_type="TX_TERMINATED",
            details={"status": status, "reason": reason, "ended_tick": tick},
        )

    def _process_queues(self, conn: sqlite3.Connection, tick: int) -> None:
        while True:
            progress = False
            waiting = conn.execute(
                "SELECT q.*, t.status AS tx_status, t.next_op"
                " FROM lock_queue q JOIN transactions t ON t.tx_key=q.tx_key"
                " WHERE q.status='WAITING' ORDER BY q.enqueue_seq"
            ).fetchall()
            for queue_row in waiting:
                if queue_row["tx_status"] != "WAITING":
                    conn.execute(
                        "UPDATE lock_queue SET status='CANCELLED', resolved_tick=?,"
                        " resolution='TX_NOT_WAITING' WHERE queue_id=?",
                        (tick, queue_row["queue_id"]),
                    )
                    progress = True
                    continue
                target = _queue_target(queue_row)
                mode = queue_row["mode"]
                if self._holders_conflict(
                    conn,
                    target,
                    mode,
                    exclude_tx=queue_row["tx_key"],
                ):
                    continue
                if self._earlier_queue_conflict(
                    conn, target, mode, queue_row["queue_id"]
                ):
                    continue
                exact_upgrade = conn.execute(
                    "SELECT * FROM lock_holders WHERE tx_key=? AND resource_key=?"
                    " AND mode='S'",
                    (queue_row["tx_key"], target.resource_key),
                ).fetchone()
                if mode == "X" and exact_upgrade is not None:
                    conn.execute(
                        "UPDATE lock_holders SET mode='X', acquire_tick=?, op_index=?"
                        " WHERE holder_id=?",
                        (tick, queue_row["op_index"], exact_upgrade["holder_id"]),
                    )
                else:
                    holder_id = self.store._next(conn, "next_holder_id")
                    acquire_seq = self.store._next(conn, "next_enqueue_seq")
                    conn.execute(
                        "INSERT INTO lock_holders("
                        "holder_id, table_name, resource_kind, resource_key,"
                        " row_value, range_lo, range_hi, tx_key, mode, acquire_seq,"
                        " acquire_tick, op_index"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            holder_id,
                            target.table_name,
                            target.kind,
                            target.resource_key,
                            _json_value(target.row_value)
                            if target.kind == "row"
                            else None,
                            _json_value(target.range_lo)
                            if target.kind == "range"
                            else None,
                            _json_value(target.range_hi)
                            if target.kind == "range"
                            else None,
                            queue_row["tx_key"],
                            mode,
                            acquire_seq,
                            tick,
                            queue_row["op_index"],
                        ),
                    )
                registration_seq, conversion = self._complete_wait(
                    conn,
                    queue_row,
                    tick,
                    outcome="GRANTED",
                )
                conn.execute(
                    "UPDATE lock_queue SET status='GRANTED', resolved_tick=?,"
                    " resolution='LOCK_GRANTED' WHERE queue_id=?",
                    (tick, queue_row["queue_id"]),
                )
                tx_row = conn.execute(
                    "SELECT * FROM transactions WHERE tx_key=?",
                    (queue_row["tx_key"],),
                ).fetchone()
                tx = self._tx_by_key[queue_row["tx_key"]]
                operation = tx.operations[int(queue_row["op_index"])]
                conn.execute(
                    "UPDATE transactions SET status='RUNNING',"
                    " waiting_deadline_tick=NULL, waiting_queue_id=NULL"
                    " WHERE tx_key=?",
                    (queue_row["tx_key"],),
                )
                self._complete_operation(
                    conn,
                    tx_row,
                    operation,
                    tick,
                    outcome="WAITED_AND_ACQUIRED",
                    wait_registration_seq=registration_seq,
                )
                self._maybe_crash("grant", tick)
                progress = True
                break
            if not progress:
                break

    def _complete_wait(
        self,
        conn: sqlite3.Connection,
        queue_row: sqlite3.Row,
        complete_tick: int,
        *,
        outcome: str,
    ) -> tuple[int, Conversion]:
        existing = conn.execute(
            "SELECT registration_seq, raw_wait_ms, scale_num, scale_den"
            " FROM wait_records WHERE tx_key=? AND op_index=?"
            " AND operation_version=?",
            (
                queue_row["tx_key"],
                queue_row["op_index"],
                queue_row["operation_version"],
            ),
        ).fetchone()
        if existing is not None:
            stored_scale = Fraction(
                int(existing["scale_num"]), int(existing["scale_den"])
            )
            return (
                int(existing["registration_seq"]),
                convert_wait(int(existing["raw_wait_ms"]), stored_scale),
            )
        raw_wait_ms = complete_tick - int(queue_row["enqueue_tick"])
        conversion = convert_wait(raw_wait_ms, self.scenario.config.scale)
        registration_seq = self.store._next(conn, "next_registration_seq")
        ordinal_row = conn.execute(
            "SELECT COUNT(*) AS count FROM wait_records WHERE tx_key=?",
            (queue_row["tx_key"],),
        ).fetchone()
        wait_ordinal = int(ordinal_row["count"]) + 1
        record_id = f"W{registration_seq:08d}"
        conn.execute(
            "INSERT INTO wait_records("
            "registration_seq, record_id, tx_key, op_index, operation_version,"
            " resource_key, mode, enqueue_tick, complete_tick, raw_wait_ms,"
            " scale_num, scale_den, exact_units, fixed_units, remainder,"
            " rounding_direction, error_units, wait_ordinal, wait_outcome"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                registration_seq,
                record_id,
                queue_row["tx_key"],
                queue_row["op_index"],
                queue_row["operation_version"],
                queue_row["resource_key"],
                queue_row["mode"],
                queue_row["enqueue_tick"],
                complete_tick,
                raw_wait_ms,
                conversion.scale.numerator,
                conversion.scale.denominator,
                fraction_text(conversion.exact_units),
                conversion.fixed_units,
                fraction_text(conversion.remainder),
                conversion.direction,
                fraction_text(conversion.error_units),
                wait_ordinal,
                outcome,
            ),
        )
        conn.execute(
            "INSERT INTO rounding_entries("
            "registration_seq, tx_key, wait_ordinal, weight_position, raw_wait_ms,"
            " exact_units, fixed_units, discarded_remainder, rounding_direction,"
            " signed_error_units, resource_key, lock_mode"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                registration_seq,
                queue_row["tx_key"],
                wait_ordinal,
                wait_ordinal,
                raw_wait_ms,
                fraction_text(conversion.exact_units),
                conversion.fixed_units,
                fraction_text(conversion.remainder),
                conversion.direction,
                fraction_text(conversion.error_units),
                queue_row["resource_key"],
                queue_row["mode"],
            ),
        )
        conn.execute(
            "UPDATE transactions SET waited_ms=waited_ms+? WHERE tx_key=?",
            (raw_wait_ms, queue_row["tx_key"]),
        )
        self._log_event(
            conn,
            tick=complete_tick,
            tx_key=queue_row["tx_key"],
            event_type="WAIT_COMPLETED",
            details={
                "registration_seq": registration_seq,
                "resource_key": queue_row["resource_key"],
                "raw_wait_ms": raw_wait_ms,
                "fixed_units": conversion.fixed_units,
                "remainder": fraction_text(conversion.remainder),
                "outcome": outcome,
            },
        )
        return registration_seq, conversion

    def _all_live_candidates(
        self,
        conn: sqlite3.Connection,
        decision_tick: int,
    ) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT t.*, q.enqueue_tick, q.resource_key, q.mode, q.queue_id"
            " FROM transactions t JOIN lock_queue q ON q.queue_id=t.waiting_queue_id"
            " WHERE t.status='WAITING' AND q.status='WAITING'"
            " ORDER BY t.tx_no"
        ).fetchall()
        candidates = []
        for row in rows:
            candidates.append(self._live_stats(conn, row, decision_tick))
        return candidates

    def _live_stats(
        self,
        conn: sqlite3.Connection,
        tx_row: sqlite3.Row,
        decision_tick: int,
    ) -> dict[str, Any]:
        prior = conn.execute(
            "SELECT COALESCE(SUM(fixed_units), 0) AS fixed,"
            " COALESCE(SUM(raw_wait_ms), 0) AS raw"
            " FROM wait_records WHERE tx_key=?",
            (tx_row["tx_key"],),
        ).fetchone()
        prior_exact = sum(
            (
                Fraction(row["exact_units"])
                for row in conn.execute(
                    "SELECT exact_units FROM wait_records WHERE tx_key=?",
                    (tx_row["tx_key"],),
                )
            ),
            Fraction(0, 1),
        )
        enqueue_tick = tx_row["enqueue_tick"]
        current_raw = max(0, decision_tick - int(enqueue_tick))
        current = convert_wait(current_raw, self.scenario.config.scale)
        fixed_total = int(prior["fixed"]) + current.fixed_units
        exact_total = prior_exact + current.exact_units
        deviation = exact_total - fixed_total
        return {
            "tx_key": tx_row["tx_key"],
            "tx_no": int(tx_row["tx_no"]),
            "wait_raw_ms": int(prior["raw"]) + current_raw,
            "current_raw_wait_ms": current_raw,
            "wait_fixed_units": fixed_total,
            "exact_wait_units": fraction_text(exact_total),
            "deviation_units": fraction_text(deviation),
            "error_upper_bound": fraction_text(deviation),
            "resource_key": tx_row["resource_key"],
            "mode": tx_row["mode"],
            "enqueue_tick": int(enqueue_tick),
        }

    def _decision_diagnostics(
        self,
        conn: sqlite3.Connection,
        candidates: Iterable[dict[str, Any]],
        *,
        decision_tick: int,
        kind: str,
    ) -> tuple[list[dict[str, Any]], bool, dict[str, Any] | None]:
        ordered = sorted(candidates, key=lambda item: (item["tx_no"], item["tx_key"]))
        for candidate in ordered:
            candidate["victim_rank_key"] = [
                candidate["wait_fixed_units"],
                -candidate["tx_no"],
            ]
        if not ordered:
            return [], False, None
        current_victim = max(
            ordered,
            key=lambda item: (item["wait_fixed_units"], -item["tx_no"]),
        )
        if kind != "DEADLOCK_SACRIFICE":
            return ordered, False, None
        tolerance = self.scenario.config.tolerance
        high_scale = self.scenario.config.scale * 1000
        high_victim = max(
            ordered,
            key=lambda item: (
                int(Fraction(item["exact_wait_units"]) * 1000),
                -item["tx_no"],
            ),
        )
        pairs: list[dict[str, Any]] = []
        sensitive = False
        for index, left in enumerate(ordered):
            for right in ordered[index + 1 :]:
                fixed_delta = Fraction(
                    left["wait_fixed_units"] - right["wait_fixed_units"]
                )
                exact_delta = Fraction(left["exact_wait_units"]) - Fraction(
                    right["exact_wait_units"]
                )
                close = abs(fixed_delta) < tolerance
                if not close:
                    continue
                sensitive = True
                current_order = (
                    "TIE"
                    if fixed_delta == 0
                    else ("LEFT" if fixed_delta > 0 else "RIGHT")
                )
                exact_order = (
                    "TIE"
                    if exact_delta == 0
                    else ("LEFT" if exact_delta > 0 else "RIGHT")
                )
                flip = current_victim["tx_key"] != high_victim["tx_key"]
                pairs.append(
                    {
                        "left_tx": left["tx_key"],
                        "right_tx": right["tx_key"],
                        "fixed_wait_delta_units": fraction_text(fixed_delta),
                        "exact_wait_delta_units": fraction_text(exact_delta),
                        "tolerance_units": fraction_text(tolerance),
                        "current_order": current_order,
                        "exact_order": exact_order,
                        "current_victim": current_victim["tx_key"],
                        "higher_scale": fraction_text(high_scale),
                        "higher_scale_victim": high_victim["tx_key"],
                        "would_flip_at_higher_scale": flip,
                        "basis": (
                            "fixed-point floor collapses the two waits to the same "
                            "comparison bucket"
                            if current_order == "TIE" and exact_order != "TIE"
                            else "fixed-point error is inside configured tolerance"
                        ),
                    }
                )
        sensitivity = None
        if pairs:
            sensitivity = {
                "decision_kind": kind,
                "decision_tick": decision_tick,
                "tolerance_units": fraction_text(tolerance),
                "higher_scale_units_per_ms": fraction_text(high_scale),
                "would_flip_at_higher_scale": any(
                    pair["would_flip_at_higher_scale"] for pair in pairs
                ),
                "pairwise": pairs,
            }
        return ordered, sensitive, sensitivity

    def _check_deadlocks(
        self,
        conn: sqlite3.Connection,
        transactions: dict[str, sqlite3.Row],
        tick: int,
    ) -> None:
        state = conn.execute(
            "SELECT last_deadlock_check_tick FROM run_state WHERE singleton=1"
        ).fetchone()
        last_check = int(state["last_deadlock_check_tick"])
        if tick - last_check < self.scenario.config.deadlock_check_period_ms:
            return
        conn.execute(
            "UPDATE run_state SET last_deadlock_check_tick=? WHERE singleton=1",
            (tick,),
        )
        graph = self._wait_graph(conn)
        cycles = self._find_cycles(graph)
        if not cycles:
            return
        cycle = min(
            cycles,
            key=lambda nodes: tuple(
                int(transactions[tx_key]["tx_no"]) for tx_key in nodes
            ),
        )
        candidates = []
        for tx_key in cycle:
            tx_row = conn.execute(
                "SELECT t.*, q.op_index, q.operation_version, q.enqueue_tick,"
                " q.resource_key, q.mode, q.queue_id"
                " FROM transactions t JOIN lock_queue q"
                " ON q.queue_id=t.waiting_queue_id"
                " WHERE t.tx_key=? AND q.status='WAITING'",
                (tx_key,),
            ).fetchone()
            if tx_row is not None:
                candidates.append(self._live_stats(conn, tx_row, tick))
        if len(candidates) < 2:
            return
        basis, sensitive, sensitivity = self._decision_diagnostics(
            conn,
            candidates,
            decision_tick=tick,
            kind="DEADLOCK_SACRIFICE",
        )
        victim = max(
            candidates,
            key=lambda item: (item["wait_fixed_units"], -item["tx_no"]),
        )
        victim_key = victim["tx_key"]
        held_before = self._held_resource_keys(conn, victim_key)
        victim_row = conn.execute(
            "SELECT t.*, q.op_index, q.operation_version, q.enqueue_tick,"
            " q.resource_key, q.mode, q.queue_id"
            " FROM transactions t JOIN lock_queue q"
            " ON q.queue_id=t.waiting_queue_id"
            " WHERE t.tx_key=? AND q.status='WAITING'",
            (victim_key,),
        ).fetchone()
        conversion = None
        if victim_row is not None:
            _, conversion = self._complete_wait(
                conn,
                victim_row,
                tick,
                outcome="SACRIFICED",
            )
            conn.execute(
                "UPDATE lock_queue SET status='SACRIFICED', resolved_tick=?,"
                " resolution='DEADLOCK_VICTIM' WHERE queue_id=?",
                (tick, victim_row["queue_id"]),
            )
        self._record_decision(
            conn,
            kind="DEADLOCK_SACRIFICE",
            tick=tick,
            tx_key=victim_key,
            reason="DEADLOCK_VICTIM",
            cycle=list(cycle),
            basis={
                "victim": victim_key,
                "victim_transaction_no": victim["tx_no"],
                "victim_wait_raw_ms": victim["wait_raw_ms"],
                "victim_wait_fixed_units": victim["wait_fixed_units"],
                "victim_deviation_units": victim["deviation_units"],
                "victim_error_upper_bound": victim["error_upper_bound"],
                "victim_completed_wait": fraction_text(conversion.exact_units)
                if conversion is not None
                else None,
                "selection": "max(wait_fixed_units, -tx_no)",
                "held_locks_released": held_before,
                "candidates": basis,
            },
            ledger_sensitive=sensitive,
            sensitivity=sensitivity,
        )
        self._terminate_tx(
            conn,
            victim_key,
            status="SACRIFICED",
            reason="DEADLOCK_VICTIM",
            tick=tick,
        )
        self._maybe_crash("abandon", tick)
        self._process_queues(conn, tick)

    def _wait_graph(self, conn: sqlite3.Connection) -> dict[str, set[str]]:
        waiters = conn.execute(
            "SELECT * FROM lock_queue WHERE status='WAITING' ORDER BY enqueue_seq"
        ).fetchall()
        holders = conn.execute("SELECT * FROM lock_holders").fetchall()
        graph: dict[str, set[str]] = {}
        for waiting in waiters:
            tx_key = waiting["tx_key"]
            graph.setdefault(tx_key, set())
            target = _queue_target(waiting)
            for holder in holders:
                if holder["tx_key"] == tx_key:
                    continue
                if modes_conflict(waiting["mode"], holder["mode"]) and targets_overlap(
                    target, _holder_target(holder)
                ):
                    graph[tx_key].add(holder["tx_key"])
            for earlier in waiters:
                if earlier["enqueue_seq"] >= waiting["enqueue_seq"]:
                    continue
                if earlier["tx_key"] == tx_key:
                    continue
                if modes_conflict(waiting["mode"], earlier["mode"]) and targets_overlap(
                    target, _queue_target(earlier)
                ):
                    graph[tx_key].add(earlier["tx_key"])
        return graph

    def _held_resource_keys(
        self, conn: sqlite3.Connection, tx_key: str
    ) -> list[dict[str, Any]]:
        return [
            {
                "resource_key": row["resource_key"],
                "mode": row["mode"],
                "acquire_seq": row["acquire_seq"],
            }
            for row in conn.execute(
                "SELECT resource_key, mode, acquire_seq FROM lock_holders"
                " WHERE tx_key=? ORDER BY acquire_seq",
                (tx_key,),
            )
        ]

    def _find_cycles(self, graph: dict[str, set[str]]) -> list[tuple[str, ...]]:
        nodes = sorted(graph)
        cycles: set[tuple[str, ...]] = set()

        def canonical(path: list[str]) -> tuple[str, ...]:
            minimum = min(range(len(path)), key=lambda index: path[index])
            return tuple(path[minimum:] + path[:minimum])

        def visit(node: str, path: list[str], on_path: set[str]) -> None:
            for nxt in sorted(graph.get(node, ())):
                if nxt in on_path:
                    start = path.index(nxt)
                    found = path[start:]
                    if len(found) >= 2:
                        cycles.add(canonical(found))
                    continue
                if len(path) > len(nodes):
                    continue
                path.append(nxt)
                on_path.add(nxt)
                visit(nxt, path, on_path)
                on_path.remove(nxt)
                path.pop()

        for node in nodes:
            visit(node, [node], {node})
        return sorted(cycles)

    def _record_decision(
        self,
        conn: sqlite3.Connection,
        *,
        kind: str,
        tick: int,
        tx_key: str | None,
        reason: str,
        cycle: list[str] | None,
        basis: dict[str, Any],
        ledger_sensitive: bool,
        sensitivity: dict[str, Any] | None,
    ) -> int:
        seq = self.store._next(conn, "next_decision_seq")
        round_no = conn.execute(
            "SELECT round_no FROM run_state WHERE singleton=1"
        ).fetchone()["round_no"]
        conn.execute(
            "INSERT INTO decisions("
            "decision_seq, decision_id, kind, tick, tx_key, reason, cycle_json,"
            " basis_json, ledger_sensitive, sensitivity_json, created_round"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                seq,
                f"D{seq:08d}",
                kind,
                tick,
                tx_key,
                reason,
                canonical_json(cycle) if cycle is not None else None,
                canonical_json(basis),
                1 if ledger_sensitive else 0,
                canonical_json(sensitivity) if sensitivity is not None else None,
                int(round_no),
            ),
        )
        self._log_event(
            conn,
            tick=tick,
            tx_key=tx_key,
            event_type="DECISION_RECORDED",
            details={
                "decision_id": f"D{seq:08d}",
                "kind": kind,
                "reason": reason,
            },
        )
        return seq

    def _finalize_horizon(self, conn: sqlite3.Connection, tick: int) -> None:
        transactions = self._transactions(conn)
        for row in transactions.values():
            if row["status"] == "NOT_STARTED":
                conn.execute(
                    "UPDATE transactions SET reason='BUDGET_NOT_STARTED',"
                    " ended_tick=NULL WHERE tx_key=?",
                    (row["tx_key"],),
                )
                self._record_decision(
                    conn,
                    kind="NOT_STARTED",
                    tick=tick,
                    tx_key=row["tx_key"],
                    reason="BUDGET_NOT_STARTED",
                    cycle=None,
                    basis={
                        "transaction": row["tx_key"],
                        "transaction_no": row["tx_no"],
                        "wait_budget_ms": row["wait_budget_ms"],
                        "started_tick": None,
                        "note": "never started; no wait record or timeout was synthesized",
                    },
                    ledger_sensitive=False,
                    sensitivity=None,
                )
            elif row["status"] in ACTIVE_STATUSES:
                self._record_decision(
                    conn,
                    kind="ACTIVE_ROLLBACK",
                    tick=tick,
                    tx_key=row["tx_key"],
                    reason="GLOBAL_HORIZON",
                    cycle=None,
                    basis={
                        "transaction": row["tx_key"],
                        "transaction_no": row["tx_no"],
                        "status_before": row["status"],
                        "horizon_tick": tick,
                        "held_locks_released": self._held_resource_keys(
                            conn, row["tx_key"]
                        ),
                    },
                    ledger_sensitive=False,
                    sensitivity=None,
                )
                self._terminate_tx(
                    conn,
                    row["tx_key"],
                    status="ABORTED",
                    reason="GLOBAL_HORIZON",
                    tick=tick,
                )
        conn.execute(
            "UPDATE run_state SET run_status='HORIZON', watermark_tick=?, clock_ms=?,"
            " round_no=round_no+1 WHERE singleton=1",
            (tick, tick),
        )
        self._log_event(
            conn,
            tick=tick,
            tx_key=None,
            event_type="RUN_HORIZON",
            details={"logical_clock_ms": tick},
        )


def target_key_for_operation(operation: Operation) -> str:
    return operation_target(operation).resource_key

