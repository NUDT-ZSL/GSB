"""Deterministic parallel transaction scheduler for the lock laboratory."""

from __future__ import annotations

from collections import deque
from typing import Any, Callable

from .errors import E_DEADLOCK, E_ISOLATION, E_LOCKTIMEOUT, LabError
from .locks import LockManager, S_LOCK, X_LOCK
from .models import EditScript, Operation, SchedulerConfig, SessionRuntime
from .storage import SQLiteStore
from .util import script_fingerprint, state_digest, utc_now_iso


EventCallback = Callable[[dict[str, Any], dict[str, Any]], None]


class Scheduler:
    """A cooperative discrete-time scheduler.

    Each scheduling quantum advances a deterministic logical clock by
    ``tick_ms``.  A waiting transaction remains in an active concurrency slot
    until it obtains the lock, times out, or is selected as a deadlock victim.
    """

    def __init__(
        self,
        script: EditScript,
        store: SQLiteStore,
        config: SchedulerConfig,
        on_event: EventCallback | None = None,
    ) -> None:
        self.script = script
        self.store = store
        self.config = config
        self.on_event = on_event

        self.clock_ms = 0
        self.events: list[dict[str, Any]] = []
        self.deadlocks: list[dict[str, Any]] = []
        self.locks = LockManager()
        self.runtimes: dict[str, SessionRuntime] = {}
        self.active_ids: set[str] = set()
        self.ready_queue: deque[str] = deque()
        self.commit_order: list[str] = []
        self._wall_started_at = utc_now_iso()

    def run(self) -> dict[str, Any]:
        initial_rows = self.store.load_rows()
        initial_version = self.store.data_version()

        pending = deque(self.script.sessions)
        for ordinal, spec in enumerate(self.script.sessions, start=1):
            self.runtimes[spec.session_id] = SessionRuntime(
                spec=spec,
                tx_id=f"tx-{ordinal:03d}",
                tx_seq=ordinal,
            )

        while pending or self.active_ids:
            self._fill_slots(pending)
            if not self.active_ids:
                break

            runtime = self._next_active_runtime()
            if runtime is None:
                # Defensive recovery: preserve a stable order if a stale queue
                # ever leaves active transactions unqueued.
                for session_id in sorted(
                    self.active_ids,
                    key=lambda sid: self.runtimes[sid].tx_seq,
                ):
                    self.ready_queue.append(session_id)
                runtime = self._next_active_runtime()
            if runtime is None:
                break

            if runtime.status == "waiting":
                self._step_waiting(runtime)
            elif runtime.status == "running":
                self._execute_next_operation(runtime)

            if runtime.status != "finished" and runtime.tx_id in self.locks.transactions:
                self.ready_queue.append(runtime.session_id)
            self.clock_ms += self.config.tick_ms

        final_rows = self.store.load_rows()
        final_version = self.store.data_version()
        expected_payloads: dict[str, Any] = {
            key: row.payload for key, row in initial_rows.items()
        }
        committed_write_count = 0
        for session_id in self.commit_order:
            runtime = self.runtimes[session_id]
            if runtime.committed_writes:
                committed_write_count += 1
                for key, payload in runtime.committed_writes.items():
                    expected_payloads[key] = payload

        expected_digest = state_digest(expected_payloads)
        actual_digest = state_digest(final_rows)
        expected_version = initial_version + committed_write_count
        isolation_ok = (
            expected_digest == actual_digest and expected_version == final_version
        )
        execution_code = None if isolation_ok else E_ISOLATION
        if not isolation_ok:
            self._event(
                "isolation_failed",
                session_id=None,
                expected_digest=expected_digest,
                actual_digest=actual_digest,
                expected_data_version=expected_version,
                actual_data_version=final_version,
            )

        sessions_summary = []
        for spec in self.script.sessions:
            runtime = self.runtimes[spec.session_id]
            sessions_summary.append(
                {
                    "session_id": spec.session_id,
                    "isolation": spec.isolation,
                    "tx_id": runtime.tx_id,
                    "tx_seq": runtime.tx_seq,
                    "outcome": runtime.outcome,
                    "code": runtime.code,
                    "started_at_ms": runtime.started_at_ms,
                    "ended_at_ms": runtime.ended_at_ms,
                    "wait_budget_used_ms": runtime.last_wait_ms,
                    "committed_data_version": runtime.committed_data_version,
                    "reads": runtime.reads,
                    "writes": runtime.writes,
                    "committed_writes": runtime.committed_writes,
                    "lock_waits": runtime.lock_waits,
                }
            )

        summary_counts: dict[str, int] = {
            "sessions": len(sessions_summary),
            "committed": sum(
                1 for session in sessions_summary if session["outcome"] == "commit"
            ),
            "rolled_back": sum(
                1 for session in sessions_summary if session["outcome"] == "rollback"
            ),
            "deadlocks": len(self.deadlocks),
            "timeouts": sum(
                1
                for session in sessions_summary
                if session["code"] == E_LOCKTIMEOUT
            ),
        }

        return {
            "format_version": 1,
            "script_name": self.script.name,
            "script_fingerprint": script_fingerprint(self.script.raw),
            "script": self.script.raw,
            "config": self.config.to_dict(),
            "wall_started_at": self._wall_started_at,
            "wall_finished_at": utc_now_iso(),
            "sim_time_ms": self.clock_ms,
            "code": execution_code,
            "isolation_ok": isolation_ok,
            "initial_data_version": initial_version,
            "final_data_version": final_version,
            "expected_data_version": expected_version,
            "actual_digest": actual_digest,
            "expected_digest": expected_digest,
            "initial_rows": [
                initial_rows[key].to_dict() for key in sorted(initial_rows)
            ],
            "final_rows": [final_rows[key].to_dict() for key in sorted(final_rows)],
            "serialization_order": list(self.commit_order),
            "sessions": sessions_summary,
            "events": self.events,
            "deadlocks": self.deadlocks,
            "summary": summary_counts,
            "final_lock_state": self.locks.rows_snapshot(),
        }

    def state_snapshot(self) -> dict[str, Any]:
        sessions = []
        for spec in self.script.sessions:
            runtime = self.runtimes.get(spec.session_id)
            if runtime is None:
                sessions.append(
                    {
                        "session_id": spec.session_id,
                        "isolation": spec.isolation,
                        "phase": "pending",
                        "op_index": 0,
                        "op": None,
                        "held_locks": [],
                        "wait_position": None,
                        "wait_budget_used_ms": 0,
                        "outcome": None,
                        "code": None,
                    }
                )
                continue
            operation = (
                spec.operations[runtime.next_op_index]
                if runtime.next_op_index < len(spec.operations)
                else None
            )
            if runtime.status == "finished":
                phase = "committed" if runtime.outcome == "commit" else "rolled_back"
            elif runtime.status == "waiting":
                phase = "waiting_lock"
            else:
                phase = runtime.status
            wait_used = runtime.last_wait_ms
            if runtime.status == "waiting" and runtime.wait_started_ms is not None:
                wait_used = max(0, self.clock_ms - runtime.wait_started_ms)
            sessions.append(
                {
                    "session_id": spec.session_id,
                    "isolation": spec.isolation,
                    "phase": phase,
                    "op_index": runtime.next_op_index,
                    "op": operation.name if operation is not None else None,
                    "held_locks": self.locks.held_locks(runtime.tx_id),
                    "wait_position": self.locks.queue_position(runtime.tx_id),
                    "wait_budget_used_ms": wait_used,
                    "outcome": runtime.outcome,
                    "code": runtime.code,
                }
            )
        return {
            "sim_time_ms": self.clock_ms,
            "active_sessions": len(self.active_ids),
            "sessions": sessions,
            "locks": self.locks.rows_snapshot(),
            "events_count": len(self.events),
        }

    def _fill_slots(self, pending: deque[Any]) -> None:
        while pending and len(self.active_ids) < self.config.max_concurrency:
            spec = pending.popleft()
            runtime = self.runtimes[spec.session_id]
            runtime.status = "running"
            runtime.started_at_ms = self.clock_ms
            self.active_ids.add(runtime.session_id)
            self.ready_queue.append(runtime.session_id)
            self.locks.begin_transaction(
                tx_id=runtime.tx_id,
                session_id=runtime.spec.session_id,
                start_seq=runtime.tx_seq,
                isolation=runtime.spec.isolation,
            )
            self._event(
                "transaction_begin",
                session_id=runtime.session_id,
                tx_id=runtime.tx_id,
                isolation=runtime.spec.isolation,
                tx_seq=runtime.tx_seq,
            )

    def _next_active_runtime(self) -> SessionRuntime | None:
        while self.ready_queue:
            session_id = self.ready_queue.popleft()
            if session_id not in self.active_ids:
                continue
            runtime = self.runtimes[session_id]
            if runtime.status in ("running", "waiting"):
                return runtime
        return None

    def _execute_next_operation(self, runtime: SessionRuntime) -> None:
        operation = runtime.spec.operations[runtime.next_op_index]
        if operation.name == "commit":
            self._commit(runtime)
            return
        if operation.name == "rollback":
            self._rollback(runtime, code="ROLLBACK", reason="explicit_rollback")
            return

        mode = S_LOCK if operation.name == "read" else X_LOCK
        result = self.locks.request(
            tx_id=runtime.tx_id,
            row_key=operation.key or "",
            mode=mode,
            op_index=operation.index,
            now_ms=self.clock_ms,
        )
        if result.granted:
            if result.newly_acquired:
                self._event(
                    "lock_acquired",
                    session_id=runtime.session_id,
                    tx_id=runtime.tx_id,
                    row_key=operation.key,
                    mode=mode,
                    op_index=operation.index,
                    waited_ms=0,
                )
            self._complete_data_operation(runtime, operation)
        else:
            runtime.status = "waiting"
            runtime.pending_lock = {
                "op_index": operation.index,
                "mode": mode,
                "row_key": operation.key,
            }
            runtime.wait_started_ms = self.clock_ms
            wait_record = self._wait_record(runtime, result.queue_position, result.blockers)
            runtime.lock_waits.append(wait_record)
            self._event(
                "lock_wait",
                session_id=runtime.session_id,
                tx_id=runtime.tx_id,
                row_key=operation.key,
                mode=mode,
                op_index=operation.index,
                queue_position=result.queue_position,
                blockers=self._blocker_sessions(result.blockers),
                wait_budget_ms=self.config.lock_wait_budget_ms,
            )
            self._resolve_deadlocks()

    def _step_waiting(self, runtime: SessionRuntime) -> None:
        pending_lock = runtime.pending_lock
        if pending_lock is None:
            runtime.status = "running"
            return
        assert runtime.wait_started_ms is not None
        elapsed = max(0, self.clock_ms - runtime.wait_started_ms)
        runtime.last_wait_ms = elapsed

        if elapsed >= self.config.lock_wait_budget_ms:
            waiter = self.locks.queue_entry(runtime.tx_id)
            self._event(
                "lock_timeout",
                session_id=runtime.session_id,
                tx_id=runtime.tx_id,
                row_key=pending_lock["row_key"],
                mode=pending_lock["mode"],
                op_index=pending_lock["op_index"],
                waited_ms=elapsed,
                wait_budget_ms=self.config.lock_wait_budget_ms,
                queue_position=self.locks.queue_position(runtime.tx_id),
            )
            self._rollback(
                runtime,
                code=E_LOCKTIMEOUT,
                reason="lock_wait_budget_exhausted",
            )
            self._resolve_deadlocks()
            return

        result = self.locks.retry(runtime.tx_id)
        if not result.granted:
            self._resolve_deadlocks()
            return

        waited_ms = elapsed
        runtime.last_wait_ms = waited_ms
        self._event(
            "lock_acquired",
            session_id=runtime.session_id,
            tx_id=runtime.tx_id,
            row_key=pending_lock["row_key"],
            mode=pending_lock["mode"],
            op_index=pending_lock["op_index"],
            waited_ms=waited_ms,
            queue_position=None,
        )
        runtime.pending_lock = None
        runtime.wait_started_ms = None
        runtime.status = "running"
        operation = runtime.spec.operations[runtime.next_op_index]
        self._complete_data_operation(runtime, operation)

    def _complete_data_operation(
        self,
        runtime: SessionRuntime,
        operation: Operation,
    ) -> None:
        key = operation.key or ""
        if operation.name == "read":
            if key in runtime.pending_writes:
                payload = runtime.pending_writes[key]
                value_version = runtime.committed_data_version
                source = "own_write"
            elif runtime.spec.isolation == "repeatable_read" and key in runtime.read_cache:
                payload, value_version = runtime.read_cache[key]
                source = "repeatable_read_cache"
            else:
                row = self.store.get_row(key)
                if row is None:
                    self._rollback(runtime, code="E_SCRIPT", reason="row_disappeared")
                    return
                payload = row.payload
                value_version = row.row_version
                source = "committed"
                if runtime.spec.isolation == "repeatable_read":
                    runtime.read_cache[key] = (payload, value_version)

            read_record = {
                "op_index": operation.index,
                "row_key": key,
                "payload": payload,
                "value_version": value_version,
                "source": source,
                "isolation": runtime.spec.isolation,
                "sim_time_ms": self.clock_ms,
            }
            runtime.reads.append(read_record)
            self._event(
                "read",
                session_id=runtime.session_id,
                tx_id=runtime.tx_id,
                row_key=key,
                op_index=operation.index,
                payload=payload,
                value_version=value_version,
                source=source,
                isolation=runtime.spec.isolation,
            )
            if runtime.spec.isolation == "read_committed":
                released = self.locks.release_statement_lock(runtime.tx_id, key)
                if released is not None:
                    self._event(
                        "lock_released",
                        session_id=runtime.session_id,
                        tx_id=runtime.tx_id,
                        row_key=key,
                        mode=released["mode"],
                        scope=released["scope"],
                        isolation=runtime.spec.isolation,
                    )
        else:
            runtime.pending_writes[key] = operation.value
            write_record = {
                "op_index": operation.index,
                "row_key": key,
                "payload": operation.value,
                "sim_time_ms": self.clock_ms,
            }
            runtime.writes.append(write_record)
            self._event(
                "write",
                session_id=runtime.session_id,
                tx_id=runtime.tx_id,
                row_key=key,
                op_index=operation.index,
                payload=operation.value,
            )

        runtime.next_op_index += 1
        runtime.status = "running"

    def _commit(self, runtime: SessionRuntime) -> None:
        try:
            version = self.store.commit_writes(
                runtime.session_id,
                runtime.pending_writes,
            )
        except LabError as exc:
            self._rollback(runtime, code=exc.code, reason="storage_commit_failed")
            return

        runtime.committed_writes = dict(runtime.pending_writes)
        runtime.committed_data_version = version
        runtime.outcome = "commit"
        runtime.code = None
        runtime.ended_at_ms = self.clock_ms
        runtime.status = "finished"
        runtime.next_op_index += 1
        released = self.locks.end_transaction(runtime.tx_id)
        self.commit_order.append(runtime.session_id)
        self.active_ids.discard(runtime.session_id)
        self._event(
            "transaction_commit",
            session_id=runtime.session_id,
            tx_id=runtime.tx_id,
            data_version=version,
            write_count=len(runtime.committed_writes),
            locked_rows=sorted(runtime.committed_writes),
            released_locks=released,
        )

    def _rollback(self, runtime: SessionRuntime, *, code: str, reason: str) -> None:
        if runtime.status == "finished":
            return
        runtime.pending_writes.clear()
        runtime.outcome = "rollback"
        runtime.code = code
        runtime.ended_at_ms = self.clock_ms
        runtime.status = "finished"
        released = self.locks.end_transaction(runtime.tx_id)
        self.active_ids.discard(runtime.session_id)
        self._event(
            "transaction_rollback",
            session_id=runtime.session_id,
            tx_id=runtime.tx_id,
            code=code,
            reason=reason,
            released_locks=released,
        )

    def _resolve_deadlocks(self) -> None:
        while True:
            deadlock = self.locks.find_deadlock()
            if deadlock is None:
                return
            self.deadlocks.append(deadlock)
            self._event(
                "deadlock_detected",
                session_id=None,
                rule=deadlock["rule"],
                cycle_members=deadlock["cycle_members"],
                cycle_transactions=deadlock["cycle_transactions"],
                victim_session_id=deadlock["victim_session_id"],
                victim_tx_id=deadlock["victim_tx_id"],
                wait_graph=deadlock["wait_graph"],
            )
            victim = next(
                (
                    runtime
                    for runtime in self.runtimes.values()
                    if runtime.tx_id == deadlock["victim_tx_id"]
                ),
                None,
            )
            if victim is None or victim.status == "finished":
                return
            self._rollback(
                victim,
                code=E_DEADLOCK,
                reason="deadlock_victim_" + deadlock["rule"],
            )

    def _blocker_sessions(self, tx_ids: list[str]) -> list[dict[str, Any]]:
        rows = []
        for tx_id in tx_ids:
            for runtime in self.runtimes.values():
                if runtime.tx_id == tx_id:
                    rows.append(
                        {"tx_id": tx_id, "session_id": runtime.session_id}
                    )
                    break
        rows.sort(key=lambda item: (item["session_id"], item["tx_id"]))
        return rows

    def _wait_record(
        self,
        runtime: SessionRuntime,
        queue_position: int | None,
        blockers: list[str],
    ) -> dict[str, Any]:
        return {
            "op_index": runtime.pending_lock["op_index"] if runtime.pending_lock else None,
            "row_key": runtime.pending_lock["row_key"] if runtime.pending_lock else None,
            "mode": runtime.pending_lock["mode"] if runtime.pending_lock else None,
            "queue_position": queue_position,
            "blockers": self._blocker_sessions(blockers),
            "started_at_ms": self.clock_ms,
            "wait_budget_ms": self.config.lock_wait_budget_ms,
        }

    def _event(self, event_type: str, **fields: Any) -> None:
        event = {
            "seq": len(self.events) + 1,
            "ts_ms": self.clock_ms,
            "timestamp": utc_now_iso(),
            "type": event_type,
        }
        event.update(fields)
        self.events.append(event)
        if self.on_event is not None:
            self.on_event(event, self.state_snapshot())
