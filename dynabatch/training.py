"""Deterministic simulated execution and lifecycle transitions."""

from __future__ import annotations

import math
from typing import Any

from .models import ModelSpec, RequestSpec
from .store import Store


class TrainingEngine:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.model: ModelSpec = store.get_model()

    def _request(self, row: Any) -> RequestSpec:
        return RequestSpec(
            seq=int(row["seq"]),
            arrival_ms=int(row["arrival_ms"]),
            priority=int(row["priority"]),
            input_tokens=int(row["input_tokens"]),
            max_output_tokens=int(row["max_output_tokens"]),
            deadline_ms=int(row["deadline_ms"]),
        )

    def run(
        self,
        now: int,
        *,
        fail_batches: tuple[str, ...] = (),
        max_batches: int | None = None,
    ) -> dict[str, Any]:
        execution_config = self.store.get_execution_config()
        fail_set = set(execution_config.fail_batches) | set(fail_batches)
        all_rows = self.store.fetchall(
            """
            SELECT * FROM batches
            WHERE status = 'committed'
              AND execution_status IN ('pending', 'running')
            ORDER BY COALESCE(committed_clock_ms, 0), window_id, ordinal
            """
        )
        if all_rows:
            first_committed = int(all_rows[0]["committed_clock_ms"] or 0)
            cursor = min(int(now), first_committed)
        else:
            cursor = self.store.logical_clock()
        with self.store.transaction():
            self.store.advance_logical_clock(cursor)
            self.store.set_meta("workflow_phase", "training")
        rows = all_rows
        if max_batches is not None:
            rows = rows[: max(0, max_batches)]
        completed = 0
        expired = 0
        failed = 0
        for batch in rows:
            batch_id = str(batch["batch_id"])
            members = self.store.fetchall(
                """
                SELECT r.*, bm.ordinal AS member_ordinal
                FROM batch_members bm
                JOIN requests r ON r.seq = bm.request_seq
                WHERE bm.batch_id = ?
                ORDER BY bm.ordinal
                """,
                (batch_id,),
            )
            duration = max(
                1,
                int(
                    math.ceil(
                        float(batch["latency_raw"])
                        + execution_config.latency_overhead_ms
                    )
                ),
            )
            start_clock = max(cursor, int(batch["committed_clock_ms"] or 0))
            if str(batch["execution_status"]) == "running" and batch[
                "started_clock_ms"
            ] is not None:
                start_clock = int(batch["started_clock_ms"])
            finish_clock = start_clock + duration
            failure = batch_id in fail_set
            expired_here: list[int] = []
            with self.store.transaction():
                self.store.execute(
                    """
                    UPDATE batches
                    SET execution_status = 'running', started_clock_ms = ?
                    WHERE batch_id = ? AND execution_status IN ('pending', 'running')
                    """,
                    (start_clock, batch_id),
                )
                for member in members:
                    seq = int(member["seq"])
                    self.store.execute(
                        """
                        UPDATE requests SET status = 'RUNNING', updated_clock_ms = ?
                        WHERE seq = ? AND status = 'BATCHED'
                        """,
                        (start_clock, seq),
                    )
                    self._event(
                        seq,
                        "BATCHED",
                        "RUNNING",
                        "execution_start",
                        start_clock,
                        str(batch["window_id"]),
                        batch_id,
                    )
            with self.store.transaction():
                replica_runtime = self.store.fetchone(
                    """
                    SELECT * FROM replica_runtime
                    WHERE replica_id = ? AND replica_version = ?
                    """,
                    (batch["replica_id"], batch["replica_version"]),
                )
                consecutive = (
                    0 if replica_runtime is None else int(replica_runtime["consecutive_failures"])
                )
                for member in members:
                    request = self._request(member)
                    seq = request.seq
                    if failure:
                        self.store.execute(
                            """
                            UPDATE requests
                            SET status = 'BATCHED', attempts = attempts + 1,
                                failure_count = failure_count + 1,
                                consecutive_failures = consecutive_failures + 1,
                                updated_clock_ms = ?
                            WHERE seq = ? AND status = 'RUNNING'
                            """,
                            (finish_clock, seq),
                        )
                        self._event(
                            seq,
                            "RUNNING",
                            "BATCHED",
                            "execution_failure",
                            finish_clock,
                            str(batch["window_id"]),
                            batch_id,
                        )
                    elif finish_clock > request.deadline_ms:
                        self.store.execute(
                            """
                            UPDATE requests
                            SET status = 'EXPIRED', attempts = attempts + 1,
                                expiry_count = expiry_count + 1,
                                updated_clock_ms = ?
                            WHERE seq = ? AND status = 'RUNNING'
                            """,
                            (finish_clock, seq),
                        )
                        self._event(
                            seq,
                            "RUNNING",
                            "EXPIRED",
                            "deadline_missed",
                            finish_clock,
                            str(batch["window_id"]),
                            batch_id,
                        )
                        expired_here.append(seq)
                    else:
                        self.store.execute(
                            """
                            UPDATE requests
                            SET status = 'COMPLETED', attempts = attempts + 1,
                                consecutive_failures = 0,
                                updated_clock_ms = ?
                            WHERE seq = ? AND status = 'RUNNING'
                            """,
                            (finish_clock, seq),
                        )
                        self._event(
                            seq,
                            "RUNNING",
                            "COMPLETED",
                            "execution_success",
                            finish_clock,
                            str(batch["window_id"]),
                            batch_id,
                        )
                if failure:
                    execution_status = "failed"
                    failure_reason = "injected_execution_failure"
                    consecutive += 1
                elif expired_here:
                    execution_status = "completed_with_expiry"
                    failure_reason = None
                    consecutive = 0
                else:
                    execution_status = "completed"
                    failure_reason = None
                    consecutive = 0
                self.store.execute(
                    """
                    UPDATE batches
                    SET execution_status = ?, finished_clock_ms = ?, failure_reason = ?
                    WHERE batch_id = ? AND execution_status = 'running'
                    """,
                    (execution_status, finish_clock, failure_reason, batch_id),
                )
                self.store.execute(
                    """
                    INSERT INTO replica_runtime(
                        replica_id, replica_version, consecutive_failures, last_batch_id
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(replica_id, replica_version) DO UPDATE SET
                        consecutive_failures = excluded.consecutive_failures,
                        last_batch_id = excluded.last_batch_id
                    """,
                    (
                        batch["replica_id"],
                        batch["replica_version"],
                        consecutive,
                        batch_id,
                    ),
                )
                cursor = finish_clock
                self.store.advance_logical_clock(cursor)
            completed += 1
            expired += len(expired_here)
            failed += int(failure)
        with self.store.transaction():
            self.store.advance_logical_clock(cursor)
            phase_row = self.store.fetchone(
                """
                SELECT COUNT(*) AS count FROM batches
                WHERE status = 'committed'
                  AND execution_status IN ('pending', 'running')
                """
            )
            pending_requests = self.store.fetchone(
                "SELECT COUNT(*) AS count FROM requests WHERE status = 'PENDING'"
            )
            complete = (
                (not phase_row or int(phase_row["count"]) == 0)
                and (not pending_requests or int(pending_requests["count"]) == 0)
            )
            phase = "complete" if complete else "training"
            self.store.set_meta("workflow_phase", phase)
        return {
            "now_ms": int(now),
            "batches_executed": completed,
            "requests_expired": expired,
            "batches_failed": failed,
            "logical_clock_ms": self.store.logical_clock(),
            "workflow_phase": self.store.get_meta("workflow_phase"),
        }

    def _event(
        self,
        seq: int,
        from_status: str,
        to_status: str,
        reason: str,
        clock_ms: int,
        window_id: str,
        batch_id: str,
    ) -> None:
        event_key = (
            f"{seq}:{from_status}:{to_status}:{reason}:{window_id}:{batch_id}:{clock_ms}"
        )
        self.store.execute(
            """
            INSERT OR IGNORE INTO lifecycle_events(
                event_key, request_seq, from_status, to_status, reason,
                clock_ms, window_id, batch_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_key,
                seq,
                from_status,
                to_status,
                reason,
                clock_ms,
                window_id,
                batch_id,
            ),
        )
