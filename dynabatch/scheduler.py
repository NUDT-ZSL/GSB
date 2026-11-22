"""Deterministic logical-clock window scheduling and batch adjudication."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Iterable

from .canonical import canonical_json, digest_json, sha256_bytes
from .errors import BusinessError, E_STATE_INVARIANT
from .fixed import (
    MetricLedger,
    build_compute_ledger,
    build_latency_ledger,
    build_memory_ledger,
    estimate_request_activation_mib,
    estimate_request_compute,
    quantize,
)
from .models import ReplicaSpec, ReplicaVersion, RequestSpec
from .store import Store


CRASH_BATCH_DECISION = 86
CRASH_WINDOW_COMMIT = 87


@dataclass
class AssignmentCandidate:
    window_id: str
    replica: ReplicaSpec
    version: ReplicaVersion
    start_ms: int
    end_ms: int
    projected_compute_raw: float
    assigned_count: int


@dataclass
class BatchPlan:
    ordinal: int
    batch_id: str
    requests: list[RequestSpec]
    compute: MetricLedger
    memory: MetricLedger
    latency: MetricLedger
    first_constraint: str
    observations: dict[str, Any]
    candidate_constraints: list[str] = field(default_factory=list)


@dataclass
class WindowPlan:
    window_id: str
    replica: ReplicaSpec
    version: ReplicaVersion
    start_ms: int
    end_ms: int
    batches: list[BatchPlan]
    deferred: list[dict[str, Any]]
    expired: list[dict[str, Any]]
    priority_preemptions: list[dict[str, Any]]
    precision_flags: list[dict[str, Any]]
    rejected: list[dict[str, Any]]
    decision: dict[str, Any]

    @property
    def decision_digest(self) -> str:
        return digest_json(self.decision)


class SchedulerEngine:
    def __init__(
        self,
        store: Store,
        *,
        crash_at: str | None = None,
    ) -> None:
        self.store = store
        self.crash_at = crash_at
        self.model = store.get_model()
        self.precision = store.get_precision()
        self.scheduler_config = store.get_scheduler_config()
        self.now = store.logical_clock()

    @staticmethod
    def _window_id(replica: ReplicaSpec, version: int, start_ms: int) -> str:
        end_ms = start_ms + replica.batch_window_ms
        return f"w:{replica.replica_id}:v{version}:{start_ms}-{end_ms}"

    def _crash(self, phase: str) -> None:
        if self.crash_at != phase:
            return
        if phase == "batch_decision":
            os._exit(CRASH_BATCH_DECISION)
        if phase == "window_commit":
            os._exit(CRASH_WINDOW_COMMIT)
        raise BusinessError(
            E_STATE_INVARIANT,
            "unknown crash injection phase",
            details={"phase": phase},
        )

    def _request_spec(self, row: sqlite3.Row) -> RequestSpec:
        return RequestSpec(
            seq=int(row["seq"]),
            arrival_ms=int(row["arrival_ms"]),
            priority=int(row["priority"]),
            input_tokens=int(row["input_tokens"]),
            max_output_tokens=int(row["max_output_tokens"]),
            deadline_ms=int(row["deadline_ms"]),
        )

    def _request_compute(self, request: RequestSpec) -> float:
        return estimate_request_compute(self.model.total_parameters, request)

    def _request_activation(self, request: RequestSpec) -> float:
        return estimate_request_activation_mib(
            self.model.activation_bytes_per_token, request
        )

    def _batch_compute_fixed(self, requests: Iterable[RequestSpec]) -> tuple[float, int]:
        raw = sum(self._request_compute(request) for request in requests)
        return raw, quantize(
            raw, self.precision.compute_scale, self.precision.rounding
        ).fixed

    def _batch_memory_fixed(self, requests: Iterable[RequestSpec]) -> tuple[float, int]:
        raw = self.model.weights_mib + sum(
            self._request_activation(request) for request in requests
        )
        return raw, quantize(
            raw, self.precision.memory_scale, self.precision.rounding
        ).fixed

    def run(self, now: int, *, max_rounds: int = 10000) -> dict[str, Any]:
        self.now = max(int(now), self.store.logical_clock())
        with self.store.transaction():
            self.store.advance_logical_clock(self.now)
            self.store.set_meta("workflow_phase", "scheduling")

        report: dict[str, Any] = {
            "now_ms": self.now,
            "rounds": 0,
            "windows_committed": 0,
            "batches_committed": 0,
            "requests_expired": 0,
            "deferred_requests": 0,
            "recovered_proposals": 0,
            "logical_clock_ms": self.now,
        }
        recovered = self._finalize_proposed(report)
        report["recovered_proposals"] = recovered

        for round_index in range(max_rounds):
            report["rounds"] = round_index + 1
            assigned = self._assign_visible_requests()
            if assigned:
                report["deferred_requests"] = max(
                    report["deferred_requests"], 0
                )
            due_rows = self.store.fetchall(
                """
                SELECT * FROM windows
                WHERE status = 'open' AND end_ms <= ?
                ORDER BY end_ms, start_ms, replica_id, replica_version
                """,
                (self.now,),
            )
            if not due_rows:
                break
            anchor_end = int(due_rows[0]["end_ms"])
            time_limit = min(
                self.now,
                anchor_end + self.scheduler_config.logical_time_budget_ms,
            )
            eligible = [row for row in due_rows if int(row["end_ms"]) <= time_limit]
            committed_this_round = 0
            progressed = False
            for row in eligible:
                available_slots = (
                    self.scheduler_config.max_concurrent_batches - committed_this_round
                )
                if available_slots <= 0:
                    break
                window_id = str(row["window_id"])
                plan = self._plan_window(window_id)
                if plan is None:
                    continue
                if len(plan.batches) > available_slots:
                    break
                progressed = True
                self._prepare_window(plan)
                self._crash("batch_decision")
                committed = self._finalize_window(window_id)
                self._crash("window_commit")
                if committed:
                    report["windows_committed"] += 1
                    report["batches_committed"] += len(plan.batches)
                    report["requests_expired"] += len(plan.expired)
                    report["deferred_requests"] += len(plan.deferred)
                    committed_this_round += len(plan.batches)
                    if not plan.batches:
                        committed_this_round += 1
                    self._assign_visible_requests()
            if not progressed:
                break

        newly_expired = self._expire_overdue_pending()
        report["requests_expired"] += newly_expired
        with self.store.transaction():
            self.store.set_meta("workflow_phase", "scheduled")
        report["logical_clock_ms"] = self.store.logical_clock()
        report["state_digest"] = self._quick_digest()
        return report

    def _quick_digest(self) -> str:
        row = self.store.fetchone(
            """
            SELECT
                (SELECT COUNT(*) FROM windows WHERE status = 'committed') AS windows,
                (SELECT COUNT(*) FROM batches WHERE status = 'committed') AS batches,
                (SELECT COUNT(*) FROM requests WHERE status = 'BATCHED') AS batched,
                (SELECT COUNT(*) FROM requests WHERE status = 'EXPIRED') AS expired,
                (SELECT COALESCE(SUM(compute_fixed), 0) FROM resource_ledger) AS compute,
                (SELECT COALESCE(SUM(memory_fixed), 0) FROM resource_ledger) AS memory
            """
        )
        return digest_json(dict(row) if row else {})

    def _finalize_proposed(self, report: dict[str, Any]) -> int:
        rows = self.store.fetchall(
            """
            SELECT window_id FROM windows
            WHERE status = 'proposed'
            ORDER BY end_ms, start_ms, replica_id, replica_version
            """
        )
        recovered = 0
        for row in rows:
            window_id = str(row["window_id"])
            if self._finalize_window(window_id):
                recovered += 1
                report["windows_committed"] += 1
                count = self.store.fetchone(
                    "SELECT COUNT(*) AS c FROM batches WHERE window_id = ?",
                    (window_id,),
                )
                report["batches_committed"] += int(count["c"]) if count else 0
            self._crash("window_commit")
        return recovered

    def _expire_overdue_pending(self) -> int:
        expired = 0
        with self.store.transaction():
            rows = self.store.fetchall(
                """
                SELECT * FROM requests
                WHERE status = 'PENDING' AND deadline_ms < ?
                ORDER BY seq
                """,
                (self.now,),
            )
            for row in rows:
                self._expire_request(
                    row, reason="overdue_before_assignment", clock_ms=self.now
                )
                expired += 1
        return expired

    def _expire_request(
        self, row: sqlite3.Row, *, reason: str, clock_ms: int | None = None
    ) -> None:
        seq = int(row["seq"])
        effective_clock = self.now if clock_ms is None else int(clock_ms)
        current_status = str(row["status"])
        if current_status == "EXPIRED":
            return
        self.store.execute(
            """
            UPDATE requests
            SET status = 'EXPIRED', expiry_count = expiry_count + 1,
                batch_id = NULL, updated_clock_ms = ?
            WHERE seq = ? AND status = 'PENDING'
            """,
            (effective_clock, seq),
        )
        self.store.execute(
            "DELETE FROM request_assignments WHERE request_seq = ?", (seq,)
        )
        event_key = f"{seq}:PENDING:EXPIRED:{reason}:{effective_clock}"
        self.store.execute(
            """
            INSERT OR IGNORE INTO lifecycle_events(
                event_key, request_seq, from_status, to_status, reason,
                clock_ms, window_id, batch_id
            ) VALUES (?, ?, 'PENDING', 'EXPIRED', ?, ?, ?, NULL)
            """,
            (event_key, seq, reason, effective_clock, row["window_id"]),
        )

    def _window_capacity(self, replica: ReplicaSpec) -> int:
        batch_limit = min(
            replica.max_concurrent_batches,
            self.scheduler_config.max_concurrent_batches,
        )
        return max(1, batch_limit) * replica.max_batch_size

    def _assign_visible_requests(self) -> int:
        assigned_count = 0
        with self.store.transaction():
            pending = self.store.fetchall(
                """
                SELECT * FROM requests
                WHERE status = 'PENDING' AND arrival_ms <= ? AND window_id IS NULL
                ORDER BY arrival_ms, priority DESC, seq
                """,
                (self.now,),
            )
            if not pending:
                return 0
            loads = self._open_window_loads()
            counts = self._open_window_counts()
            version_cache: dict[int, ReplicaVersion] = {}
            for row in pending:
                request = self._request_spec(row)
                candidates: list[AssignmentCandidate] = []
                not_before = int(row["not_before_ms"])
                for version_number, replica_template in self._replica_version_templates():
                    offset = 0
                    for _attempt in range(4096):
                        base = max(request.arrival_ms, not_before) + offset
                        replica_start = (
                            base // replica_template.batch_window_ms
                        ) * replica_template.batch_window_ms
                        version = version_cache.get(replica_start)
                        if version is None:
                            version = self.store.version_at(replica_start)
                            version_cache[replica_start] = version
                        if version.version != version_number:
                            offset += replica_template.batch_window_ms
                            continue
                        replica = version.by_id().get(replica_template.replica_id)
                        if replica is None:
                            break
                        window_id = self._window_id(
                            replica, version.version, replica_start
                        )
                        existing_window = self.store.fetchone(
                            "SELECT status FROM windows WHERE window_id = ?",
                            (window_id,),
                        )
                        if existing_window is not None and str(
                            existing_window["status"]
                        ) != "open":
                            offset += replica.batch_window_ms
                            continue
                        count = counts.get(window_id, 0)
                        if count < self._window_capacity(replica):
                            load = loads.get(window_id, 0.0) + self._request_compute(request)
                            candidates.append(
                                AssignmentCandidate(
                                    window_id=window_id,
                                    replica=replica,
                                    version=version,
                                    start_ms=replica_start,
                                    end_ms=replica_start + replica.batch_window_ms,
                                    projected_compute_raw=load,
                                    assigned_count=count,
                                )
                            )
                            break
                        offset += replica.batch_window_ms
                if not candidates:
                    continue
                candidates.sort(
                    key=lambda item: (
                        item.end_ms,
                        item.projected_compute_raw / item.replica.compute_units,
                        item.assigned_count,
                        item.replica.replica_id,
                        item.version.version,
                    )
                )
                chosen = candidates[0]
                if len(candidates) > 1:
                    alternative = candidates[1]
                    if (
                        alternative.end_ms == chosen.end_ms
                        and abs(
                            chosen.projected_compute_raw
                            - alternative.projected_compute_raw
                        )
                        < self.precision.tolerance
                    ):
                        flag = {
                            "flag_id": "pf:replica:"
                            + sha256_bytes(
                                canonical_json(
                                    [
                                        request.seq,
                                        chosen.window_id,
                                        alternative.window_id,
                                    ]
                                ).encode("utf-8")
                            )[:24],
                            "window_id": chosen.window_id,
                            "kind": "replica_choice",
                            "subject_a": chosen.window_id,
                            "subject_b": alternative.window_id,
                            "metric": "projected_compute_raw",
                            "raw_a": chosen.projected_compute_raw,
                            "raw_b": alternative.projected_compute_raw,
                            "difference": abs(
                                chosen.projected_compute_raw
                                - alternative.projected_compute_raw
                            ),
                            "tolerance": self.precision.tolerance,
                            "depended": 1,
                            "reason": (
                                "replica choice is within tolerance; stable replica_id "
                                "tie-break selected the first candidate"
                            ),
                        }
                        self._insert_precision_flag(flag)
                self.store.execute(
                    """
                    INSERT INTO windows(
                        window_id, replica_id, replica_version, start_ms, end_ms,
                        status, batch_count, used_compute_fixed,
                        used_memory_peak_fixed, deferred_count
                    )
                    VALUES (?, ?, ?, ?, ?, 'open', 0, 0, 0, 0)
                    ON CONFLICT(window_id) DO NOTHING
                    """,
                    (
                        chosen.window_id,
                        chosen.replica.replica_id,
                        chosen.version.version,
                        chosen.start_ms,
                        chosen.end_ms,
                    ),
                )
                self.store.execute(
                    """
                    INSERT INTO request_assignments(
                        request_seq, window_id, replica_id, replica_version,
                        start_ms, end_ms, state, assigned_clock_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, 'assigned', ?)
                    ON CONFLICT(request_seq) DO UPDATE SET
                        window_id = excluded.window_id,
                        replica_id = excluded.replica_id,
                        replica_version = excluded.replica_version,
                        start_ms = excluded.start_ms,
                        end_ms = excluded.end_ms,
                        state = 'assigned',
                        assigned_clock_ms = excluded.assigned_clock_ms
                    """,
                    (
                        request.seq,
                        chosen.window_id,
                        chosen.replica.replica_id,
                        chosen.version.version,
                        chosen.start_ms,
                        chosen.end_ms,
                        self.now,
                    ),
                )
                self.store.execute(
                    """
                    UPDATE requests
                    SET replica_id = ?, replica_version = ?, window_id = ?,
                        updated_clock_ms = ?
                    WHERE seq = ?
                    """,
                    (
                        chosen.replica.replica_id,
                        chosen.version.version,
                        chosen.window_id,
                        self.now,
                        request.seq,
                    ),
                )
                evidence_id = (
                    f"assign:{request.seq}:{chosen.window_id}:{self.now}"
                )
                self.store.execute(
                    """
                    INSERT OR IGNORE INTO assignment_history(
                        evidence_id, request_seq, window_id, replica_id,
                        replica_version, action, clock_ms
                    ) VALUES (?, ?, ?, ?, ?, 'assigned', ?)
                    """,
                    (
                        evidence_id,
                        request.seq,
                        chosen.window_id,
                        chosen.replica.replica_id,
                        chosen.version.version,
                        self.now,
                    ),
                )
                counts[chosen.window_id] = counts.get(chosen.window_id, 0) + 1
                loads[chosen.window_id] = chosen.projected_compute_raw
                assigned_count += 1
        return assigned_count

    def _replica_version_templates(self) -> list[tuple[int, ReplicaSpec]]:
        templates = [
            (version.version, replica)
            for version in self.store.get_versions()
            for replica in version.replicas
        ]
        return sorted(
            templates,
            key=lambda item: (
                item[1].batch_window_ms,
                item[1].replica_id,
                item[0],
            ),
        )

    def _open_window_loads(self) -> dict[str, float]:
        rows = self.store.fetchall(
            """
            SELECT ra.window_id, r.input_tokens, r.max_output_tokens
            FROM request_assignments ra
            JOIN requests r ON r.seq = ra.request_seq
            JOIN windows w ON w.window_id = ra.window_id
            WHERE ra.state = 'assigned' AND w.status = 'open'
            """
        )
        result: dict[str, float] = {}
        for row in rows:
            request = RequestSpec(
                seq=0,
                arrival_ms=0,
                priority=0,
                input_tokens=int(row["input_tokens"]),
                max_output_tokens=int(row["max_output_tokens"]),
                deadline_ms=0,
            )
            window_id = str(row["window_id"])
            result[window_id] = result.get(window_id, 0.0) + self._request_compute(request)
        return result

    def _open_window_counts(self) -> dict[str, int]:
        rows = self.store.fetchall(
            """
            SELECT ra.window_id, COUNT(*) AS count
            FROM request_assignments ra
            JOIN windows w ON w.window_id = ra.window_id
            WHERE ra.state = 'assigned' AND w.status = 'open'
            GROUP BY ra.window_id
            """
        )
        return {str(row["window_id"]): int(row["count"]) for row in rows}

    def _insert_precision_flag(self, flag: dict[str, Any]) -> None:
        self.store.execute(
            """
            INSERT OR IGNORE INTO precision_flags(
                flag_id, window_id, kind, subject_a, subject_b, metric,
                raw_a, raw_b, difference, tolerance, depended, reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                flag["flag_id"],
                flag["window_id"],
                flag["kind"],
                flag["subject_a"],
                flag["subject_b"],
                flag["metric"],
                float(flag["raw_a"]),
                float(flag["raw_b"]),
                float(flag["difference"]),
                float(flag["tolerance"]),
                int(flag["depended"]),
                flag["reason"],
            ),
        )

    def _plan_window(self, window_id: str) -> WindowPlan | None:
        window = self.store.fetchone(
            "SELECT * FROM windows WHERE window_id = ?", (window_id,)
        )
        if window is None or str(window["status"]) != "open":
            return None
        version = self.store.get_version(int(window["replica_version"]))
        replica = version.by_id().get(str(window["replica_id"]))
        if replica is None:
            raise BusinessError(
                E_STATE_INVARIANT,
                "window references a replica absent from its bound version",
                details={"window_id": window_id, "version": version.version},
            )
        rows = self.store.fetchall(
            """
            SELECT r.*
            FROM request_assignments ra
            JOIN requests r ON r.seq = ra.request_seq
            WHERE ra.window_id = ? AND ra.state = 'assigned' AND r.status = 'PENDING'
            ORDER BY r.priority DESC, r.arrival_ms, r.seq
            """,
            (window_id,),
        )
        requests = [self._request_spec(row) for row in rows]
        effective_clock = min(self.now, int(window["end_ms"]))
        batch_limit = min(
            replica.max_concurrent_batches,
            self.scheduler_config.max_concurrent_batches,
        )
        (
            batch_members,
            batch_constraints,
            rejection_reasons,
        ) = self._compose_batches(requests, replica, max(0, batch_limit))
        selected = {request.seq for members in batch_members for request in members}
        priority_preemptions = self._priority_preemptions(requests, selected)
        expired: list[dict[str, Any]] = []
        deferred: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for request in requests:
            if request.seq in selected:
                continue
            reason = rejection_reasons.get(request.seq, "window_capacity")
            if request.deadline_ms < effective_clock:
                reason = "deadline"
                expired.append(
                    {
                        "request_seq": request.seq,
                        "reason": "deadline",
                        "deadline_ms": request.deadline_ms,
                        "logical_clock_ms": effective_clock,
                    }
                )
            else:
                deferred.append(
                    {
                        "request_seq": request.seq,
                        "reason": reason,
                        "not_before_ms": int(window["end_ms"]),
                    }
                )
            rejected.append(
                {
                    "request_seq": request.seq,
                    "reason": reason,
                    "first_constraint": reason,
                    "priority": request.priority,
                    "arrival_ms": request.arrival_ms,
                }
            )

        batches: list[BatchPlan] = []
        compute_capacity = quantize(
            replica.compute_units,
            self.precision.compute_scale,
            self.precision.rounding,
        ).fixed
        memory_capacity = quantize(
            float(replica.memory_mib),
            self.precision.memory_scale,
            self.precision.rounding,
        ).fixed
        used_compute_fixed = 0
        peak_memory_fixed = 0
        for ordinal, members in enumerate(batch_members, start=1):
            batch_id = f"{window_id}:b{ordinal}"
            compute = build_compute_ledger(
                batch_id, self.model.total_parameters, members, self.precision
            )
            memory = build_memory_ledger(
                batch_id,
                self.model.total_parameters,
                self.model.bytes_per_parameter,
                self.model.activation_bytes_per_token,
                members,
                self.precision,
            )
            latency = build_latency_ledger(
                batch_id,
                members,
                int(window["end_ms"]),
                compute.raw,
                self.model.latency_ms_per_compute_unit,
                self.precision,
            )
            constraints = batch_constraints.get(ordinal - 1, [])
            first_constraint = constraints[0] if constraints else "window_end"
            observations = {
                "capacity_compute_fixed": compute_capacity,
                "capacity_memory_fixed": memory_capacity,
                "remaining_compute_before_fixed": compute_capacity - used_compute_fixed,
                "remaining_memory_before_fixed": memory_capacity,
                "requests": [
                    {
                        "seq": request.seq,
                        "priority": request.priority,
                        "arrival_ms": request.arrival_ms,
                        "deadline_ms": request.deadline_ms,
                        "input_tokens": request.input_tokens,
                        "max_output_tokens": request.max_output_tokens,
                        "token_sum": request.total_tokens,
                        "compute_raw": self._request_compute(request),
                        "activation_mib_raw": self._request_activation(request),
                    }
                    for request in members
                ],
                "remaining_compute_after_fixed": compute_capacity
                - used_compute_fixed
                - compute.fixed,
                "remaining_memory_after_fixed": memory_capacity - memory.fixed,
            }
            batches.append(
                BatchPlan(
                    ordinal=ordinal,
                    batch_id=batch_id,
                    requests=members,
                    compute=compute,
                    memory=memory,
                    latency=latency,
                    first_constraint=first_constraint,
                    observations=observations,
                    candidate_constraints=constraints,
                )
            )
            used_compute_fixed += compute.fixed
            peak_memory_fixed = max(peak_memory_fixed, memory.fixed)

        precision_flags = self._batch_precision_flags(
            window_id,
            requests,
            replica,
            int(window["end_ms"]),
            batch_members,
            selected,
        )
        decision = {
            "window": {
                "window_id": window_id,
                "replica_id": replica.replica_id,
                "replica_version": version.version,
                "start_ms": int(window["start_ms"]),
                "end_ms": int(window["end_ms"]),
                "scheduler_clock_ms": self.now,
                "effective_clock_ms": effective_clock,
            },
            "fixed_point_scales": {
                "compute": self.precision.compute_scale,
                "memory": self.precision.memory_scale,
                "latency": self.precision.latency_scale,
            },
            "budgets": {
                "compute_units_raw": replica.compute_units,
                "compute_capacity_fixed": compute_capacity,
                "memory_mib_raw": replica.memory_mib,
                "memory_capacity_fixed": memory_capacity,
                "max_batch_size": replica.max_batch_size,
                "batch_slots": batch_limit,
            },
            "used": {
                "compute_fixed": used_compute_fixed,
                "memory_peak_fixed": peak_memory_fixed,
            },
            "batches": [
                {
                    "batch_id": batch.batch_id,
                    "request_seqs": [request.seq for request in batch.requests],
                    "first_constraint": batch.first_constraint,
                    "observations": batch.observations,
                    "rejected": [
                        item
                        for item in rejected
                        if item["first_constraint"] == batch.first_constraint
                    ],
                }
                for batch in batches
            ],
            "rejected": rejected,
            "deferred": deferred,
            "expired": expired,
            "priority_preemptions": priority_preemptions,
            "precision_sensitive": bool(precision_flags),
        }
        return WindowPlan(
            window_id=window_id,
            replica=replica,
            version=version,
            start_ms=int(window["start_ms"]),
            end_ms=int(window["end_ms"]),
            batches=batches,
            deferred=deferred,
            expired=expired,
            priority_preemptions=priority_preemptions,
            precision_flags=precision_flags,
            rejected=rejected,
            decision=decision,
        )

    def _compose_batches(
        self,
        requests: list[RequestSpec],
        replica: ReplicaSpec,
        batch_limit: int,
    ) -> tuple[list[list[RequestSpec]], dict[int, list[str]], dict[int, str]]:
        batches: list[list[RequestSpec]] = []
        constraints: dict[int, list[str]] = {}
        rejection_reasons: dict[int, str] = {}
        compute_capacity = quantize(
            replica.compute_units,
            self.precision.compute_scale,
            self.precision.rounding,
        ).fixed
        memory_capacity = quantize(
            float(replica.memory_mib),
            self.precision.memory_scale,
            self.precision.rounding,
        ).fixed
        def fits(
            members: list[RequestSpec], candidate: RequestSpec
        ) -> tuple[bool, str | None]:
            if len(members) >= replica.max_batch_size:
                return False, "max_batch_size"
            prospective = members + [candidate]
            _, compute_fixed = self._batch_compute_fixed(prospective)
            _, memory_fixed = self._batch_memory_fixed(prospective)
            other_compute = sum(
                self._batch_compute_fixed(other)[1]
                for other in batches
                if other is not members
            )
            if other_compute + compute_fixed > compute_capacity:
                return False, "compute"
            if memory_fixed > memory_capacity:
                return False, "memory"
            return True, None

        for request in requests:
            placed = False
            observed: list[str] = []
            for index, members in enumerate(batches):
                ok, constraint = fits(members, request)
                if ok:
                    members.append(request)
                    placed = True
                    break
                if constraint:
                    observed.append(constraint)
                    constraints.setdefault(index, [])
                    if constraint not in constraints[index]:
                        constraints[index].append(constraint)
            if placed:
                continue
            if len(batches) < batch_limit:
                alone_ok, alone_constraint = fits([], request)
                if alone_ok:
                    batches.append([request])
                    constraints.setdefault(len(batches) - 1, [])
                    continue
                reason = alone_constraint or "compute"
            else:
                reason = "concurrency"
            rejection_reasons[request.seq] = reason
            for constraint in observed:
                if constraint == reason:
                    break

        for index, members in enumerate(batches):
            if len(members) >= replica.max_batch_size:
                constraints.setdefault(index, ["max_batch_size"])
            elif not constraints.get(index):
                constraints[index] = ["window_end"]
        return batches, constraints, rejection_reasons

    def _priority_preemptions(
        self, requests: list[RequestSpec], selected: set[int]
    ) -> list[dict[str, Any]]:
        sorted_requests = sorted(
            requests, key=lambda item: (-item.priority, item.arrival_ms, item.seq)
        )
        position = {request.seq: index for index, request in enumerate(sorted_requests)}
        result: list[dict[str, Any]] = []
        for high in requests:
            if high.seq not in selected:
                continue
            skipped = [
                {
                    "request_seq": low.seq,
                    "priority": low.priority,
                    "arrival_ms": low.arrival_ms,
                    "first_constraint": "priority",
                    "reason": "priority_preempted",
                }
                for low in requests
                if low.arrival_ms < high.arrival_ms
                and low.priority < high.priority
                and position[low.seq] > position[high.seq]
            ]
            if skipped:
                result.append(
                    {
                        "request_seq": high.seq,
                        "priority": high.priority,
                        "skipped": skipped,
                        "first_constraint": "priority",
                    }
                )
        return result

    def _batch_precision_flags(
        self,
        window_id: str,
        requests: list[RequestSpec],
        replica: ReplicaSpec,
        window_end_ms: int,
        primary_batches: list[list[RequestSpec]],
        selected: set[int],
    ) -> list[dict[str, Any]]:
        flags: list[dict[str, Any]] = []
        arrival_order = sorted(requests, key=lambda item: (item.arrival_ms, item.seq))
        alternative_batches, _, _ = self._compose_batches(
            arrival_order, replica, len(primary_batches) or 1
        )
        primary_signature = [[request.seq for request in batch] for batch in primary_batches]
        alternative_signature = [
            [request.seq for request in batch] for batch in alternative_batches
        ]
        if primary_signature != alternative_signature:
            primary_metric = self._plan_latency_raw(primary_batches, window_end_ms)
            alternative_metric = self._plan_latency_raw(
                alternative_batches, window_end_ms
            )
            difference = abs(primary_metric - alternative_metric)
            if difference < self.precision.tolerance:
                flags.append(
                    {
                        "flag_id": "pf:batch:"
                        + sha256_bytes(
                            canonical_json(
                                [window_id, primary_signature, alternative_signature]
                            ).encode("utf-8")
                        )[:24],
                        "window_id": window_id,
                        "kind": "batch_plan",
                        "subject_a": "priority_order",
                        "subject_b": "arrival_order",
                        "metric": "estimated_total_latency_ms_raw",
                        "raw_a": primary_metric,
                        "raw_b": alternative_metric,
                        "difference": difference,
                        "tolerance": self.precision.tolerance,
                        "depended": 1,
                        "reason": (
                            "two deterministic batch plans differ but their estimated "
                            "latency is inside the configured tolerance"
                        ),
                    }
                )
        for left_index, left in enumerate(requests):
            left_compute = self._request_compute(left)
            for right in requests[left_index + 1 :]:
                right_compute = self._request_compute(right)
                difference = abs(left_compute - right_compute)
                if difference >= self.precision.tolerance:
                    continue
                if (left.seq in selected) == (right.seq in selected):
                    continue
                flag_id = "pf:batch-candidate:" + sha256_bytes(
                    canonical_json([window_id, left.seq, right.seq]).encode("utf-8")
                )[:24]
                flags.append(
                    {
                        "flag_id": flag_id,
                        "window_id": window_id,
                        "kind": "batch_candidate",
                        "subject_a": f"request:{left.seq}",
                        "subject_b": f"request:{right.seq}",
                        "metric": "request_compute_raw",
                        "raw_a": left_compute,
                        "raw_b": right_compute,
                        "difference": difference,
                        "tolerance": self.precision.tolerance,
                        "depended": 1,
                        "reason": (
                            "request selection differs while compute estimates are "
                            "inside tolerance"
                        ),
                    }
                )
        return flags

    def _plan_latency_raw(
        self, batches: list[list[RequestSpec]], window_end_ms: int
    ) -> float:
        total = 0.0
        for members in batches:
            if not members:
                continue
            compute_raw, _ = self._batch_compute_fixed(members)
            wait = sum(max(0, window_end_ms - item.arrival_ms) for item in members) / len(
                members
            )
            total += float(wait) + compute_raw * self.model.latency_ms_per_compute_unit
        return total

    def _prepare_window(self, plan: WindowPlan) -> None:
        prepared_clock = min(self.now, plan.end_ms)
        with self.store.transaction():
            existing = self.store.fetchone(
                "SELECT status, decision_digest FROM windows WHERE window_id = ?",
                (plan.window_id,),
            )
            if existing is None:
                raise BusinessError(
                    E_STATE_INVARIANT,
                    "cannot prepare a missing window",
                    details={"window_id": plan.window_id},
                )
            status = str(existing["status"])
            if status == "committed":
                return
            if status == "proposed":
                if str(existing["decision_digest"]) != plan.decision_digest:
                    raise BusinessError(
                        E_STATE_INVARIANT,
                        "replayed window decision differs from the prepared proposal",
                        details={"window_id": plan.window_id},
                    )
                return
            used_compute_fixed = sum(batch.compute.fixed for batch in plan.batches)
            peak_memory_fixed = max(
                (batch.memory.fixed for batch in plan.batches), default=0
            )
            for batch in plan.batches:
                self.store.execute(
                    """
                    INSERT OR IGNORE INTO batches(
                        batch_id, window_id, ordinal, replica_id, replica_version,
                        status, request_count, compute_raw, memory_raw, latency_raw,
                        compute_fixed, memory_fixed, latency_fixed,
                        compute_error_bound, memory_error_bound, latency_error_bound,
                        first_constraint, precision_sensitive, decision_json,
                        prepared_clock_ms
                    ) VALUES (?, ?, ?, ?, ?, 'proposed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        batch.batch_id,
                        plan.window_id,
                        batch.ordinal,
                        plan.replica.replica_id,
                        plan.version.version,
                        len(batch.requests),
                        batch.compute.raw,
                        batch.memory.raw,
                        batch.latency.raw,
                        batch.compute.fixed,
                        batch.memory.fixed,
                        batch.latency.fixed,
                        batch.compute.error_bound,
                        batch.memory.error_bound,
                        batch.latency.error_bound,
                        batch.first_constraint,
                        int(bool(plan.precision_flags)),
                        canonical_json(
                            {
                                "batch_id": batch.batch_id,
                                "request_seqs": [
                                    request.seq for request in batch.requests
                                ],
                                "first_constraint": batch.first_constraint,
                                "observations": batch.observations,
                                "candidate_constraints": batch.candidate_constraints,
                                "rejected": plan.rejected,
                                "priority_preemptions": plan.priority_preemptions,
                            }
                        ),
                        prepared_clock,
                    ),
                )
                for member_index, request in enumerate(batch.requests, start=1):
                    self.store.execute(
                        """
                        INSERT OR IGNORE INTO batch_members(
                            batch_id, request_seq, replica_version, ordinal, state
                        ) VALUES (?, ?, ?, ?, 'proposed')
                        """,
                        (
                            batch.batch_id,
                            request.seq,
                            plan.version.version,
                            member_index,
                        ),
                    )
                for ledger in (batch.compute, batch.memory, batch.latency):
                    for operation in ledger.operations:
                        self.store.execute(
                            """
                            INSERT OR IGNORE INTO error_operations(
                                op_id, batch_id, metric, ordinal, operation,
                                operand_a_raw, operand_b_raw, result_raw, scale,
                                fixed_result, rounding, propagated_error_bound,
                                cumulative_error_bound, details_json
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                operation.op_id,
                                operation.batch_id,
                                operation.metric,
                                operation.ordinal,
                                operation.operation,
                                operation.operand_a_raw,
                                operation.operand_b_raw,
                                operation.result_raw,
                                operation.scale,
                                operation.fixed_result,
                                operation.rounding,
                                operation.propagated_error_bound,
                                operation.cumulative_error_bound,
                                canonical_json(operation.details),
                            ),
                        )
            for flag in plan.precision_flags:
                self._insert_precision_flag(flag)
            self.store.execute(
                """
                UPDATE windows
                SET status = 'proposed', batch_count = ?,
                    used_compute_fixed = ?, used_memory_peak_fixed = ?,
                    deferred_count = ?, decision_digest = ?,
                    decision_json = ?, prepared_clock_ms = ?
                WHERE window_id = ? AND status = 'open'
                """,
                (
                    len(plan.batches),
                    used_compute_fixed,
                    peak_memory_fixed,
                    len(plan.deferred) + len(plan.expired),
                    plan.decision_digest,
                    canonical_json(plan.decision),
                    prepared_clock,
                    plan.window_id,
                ),
            )

    def _finalize_window(self, window_id: str) -> bool:
        plan_row = self.store.fetchone(
            "SELECT * FROM windows WHERE window_id = ?", (window_id,)
        )
        if plan_row is None:
            return False
        status = str(plan_row["status"])
        if status == "committed":
            return False
        if status != "proposed":
            return False
        prepared_clock = int(plan_row["prepared_clock_ms"] or self.now)
        self.store.conn.execute("BEGIN IMMEDIATE")
        try:
            batch_rows = self.store.fetchall(
                "SELECT * FROM batches WHERE window_id = ? ORDER BY ordinal",
                (window_id,),
            )
            for batch_row in batch_rows:
                batch_id = str(batch_row["batch_id"])
                members = self.store.fetchall(
                    """
                    SELECT r.* FROM batch_members bm
                    JOIN requests r ON r.seq = bm.request_seq
                    WHERE bm.batch_id = ?
                    ORDER BY bm.ordinal
                    """,
                    (batch_id,),
                )
                for member in members:
                    seq = int(member["seq"])
                    current = self.store.fetchone(
                        "SELECT status, batch_id FROM requests WHERE seq = ?", (seq,)
                    )
                    if current is None:
                        raise BusinessError(
                            E_STATE_INVARIANT,
                            "batch member request is missing",
                            details={"request_seq": seq, "batch_id": batch_id},
                        )
                    if str(current["status"]) == "PENDING":
                        self.store.execute(
                            """
                            UPDATE requests
                            SET status = 'BATCHED', batch_id = ?,
                                replica_id = ?, replica_version = ?,
                                window_id = ?, updated_clock_ms = ?
                            WHERE seq = ? AND status = 'PENDING'
                            """,
                            (
                                batch_id,
                                plan_row["replica_id"],
                                plan_row["replica_version"],
                                window_id,
                                prepared_clock,
                                seq,
                            ),
                        )
                        event_key = f"{seq}:PENDING:BATCHED:window_commit:{window_id}:{batch_id}"
                        self.store.execute(
                            """
                            INSERT OR IGNORE INTO lifecycle_events(
                                event_key, request_seq, from_status, to_status,
                                reason, clock_ms, window_id, batch_id
                            ) VALUES (?, ?, 'PENDING', 'BATCHED',
                                      'window_commit', ?, ?, ?)
                            """,
                            (event_key, seq, prepared_clock, window_id, batch_id),
                        )
                    elif str(current["batch_id"] or "") != batch_id:
                        raise BusinessError(
                            E_STATE_INVARIANT,
                            "request already belongs to another committed batch",
                            details={
                                "request_seq": seq,
                                "batch_id": batch_id,
                                "existing_batch_id": current["batch_id"],
                            },
                        )
                self.store.execute(
                    "UPDATE batch_members SET state = 'committed' WHERE batch_id = ?",
                    (batch_id,),
                )
                self.store.execute(
                    """
                    UPDATE batches
                    SET status = 'committed', committed_clock_ms = ?
                    WHERE batch_id = ? AND status = 'proposed'
                    """,
                    (prepared_clock, batch_id),
                )
                self.store.execute(
                    """
                    INSERT OR IGNORE INTO resource_ledger(
                        batch_id, window_id, replica_id, replica_version,
                        compute_fixed, memory_fixed, committed_clock_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        batch_id,
                        window_id,
                        plan_row["replica_id"],
                        plan_row["replica_version"],
                        int(batch_row["compute_fixed"]),
                        int(batch_row["memory_fixed"]),
                        prepared_clock,
                    ),
                )
                self.store.execute(
                    """
                    INSERT OR IGNORE INTO replica_runtime(
                        replica_id, replica_version, consecutive_failures
                    ) VALUES (?, ?, 0)
                    """,
                    (plan_row["replica_id"], plan_row["replica_version"]),
                )
            decision_rows = self._assignment_rows_for_window(window_id)
            batch_request_ids = {
                int(row["request_seq"])
                for row in self.store.fetchall(
                    "SELECT request_seq FROM batch_members WHERE batch_id IN "
                    "(SELECT batch_id FROM batches WHERE window_id = ?)",
                    (window_id,),
                )
            }
            windows_decision = self._window_decision_json(window_id)
            for row in decision_rows:
                seq = int(row["request_seq"])
                if seq in batch_request_ids:
                    self.store.execute(
                        """
                        UPDATE request_assignments SET state = 'committed'
                        WHERE request_seq = ? AND window_id = ?
                        """,
                        (seq, window_id),
                    )
                    continue
                expired = any(
                    int(item.get("request_seq", -1)) == seq
                    for item in windows_decision.get("expired", [])
                )
                if expired:
                    request_row = self.store.fetchone(
                        "SELECT * FROM requests WHERE seq = ?", (seq,)
                    )
                    if request_row is not None and str(request_row["status"]) == "PENDING":
                        expired_clock = next(
                            (
                                int(item.get("logical_clock_ms", prepared_clock))
                                for item in windows_decision.get("expired", [])
                                if int(item.get("request_seq", -1)) == seq
                            ),
                            prepared_clock,
                        )
                        self._expire_request(
                            request_row,
                            reason="window_expiry",
                            clock_ms=expired_clock,
                        )
                    self.store.execute(
                        "DELETE FROM request_assignments WHERE request_seq = ?", (seq,)
                    )
                else:
                    reason = next(
                        (
                            item.get("reason", "capacity")
                            for item in windows_decision.get("deferred", [])
                            if int(item.get("request_seq", -1)) == seq
                        ),
                        "capacity",
                    )
                    self.store.execute(
                        """
                        UPDATE request_assignments SET state = 'deferred'
                        WHERE request_seq = ? AND window_id = ?
                        """,
                        (seq, window_id),
                    )
                    self.store.execute(
                        """
                        UPDATE requests
                        SET window_id = NULL, batch_id = NULL,
                            not_before_ms = ?, updated_clock_ms = ?
                        WHERE seq = ? AND status = 'PENDING'
                        """,
                        (int(plan_row["end_ms"]), prepared_clock, seq),
                    )
                    event_key = f"{seq}:PENDING:PENDING:deferred:{window_id}:{reason}"
                    self.store.execute(
                        """
                        INSERT OR IGNORE INTO lifecycle_events(
                            event_key, request_seq, from_status, to_status,
                            reason, clock_ms, window_id, batch_id
                        ) VALUES (?, ?, 'PENDING', 'PENDING', ?, ?, ?, NULL)
                        """,
                        (event_key, seq, reason, prepared_clock, window_id),
                    )
            used = self.store.fetchone(
                """
                SELECT COALESCE(SUM(compute_fixed), 0) AS compute,
                       COALESCE(MAX(memory_fixed), 0) AS memory
                FROM resource_ledger WHERE window_id = ?
                """,
                (window_id,),
            )
            self.store.execute(
                """
                UPDATE windows
                SET status = 'committed', committed_clock_ms = ?,
                    used_compute_fixed = ?, used_memory_peak_fixed = ?
                WHERE window_id = ? AND status = 'proposed'
                """,
                (
                    prepared_clock,
                    int(used["compute"]) if used else 0,
                    int(used["memory"]) if used else 0,
                    window_id,
                ),
            )
            if self.crash_at == "window_commit":
                os._exit(CRASH_WINDOW_COMMIT)
            self.store.conn.execute("COMMIT")
        except BaseException:
            try:
                self.store.conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        return True

    def _assignment_rows_for_window(self, window_id: str) -> list[sqlite3.Row]:
        return self.store.fetchall(
            """
            SELECT * FROM request_assignments
            WHERE window_id = ? ORDER BY request_seq
            """,
            (window_id,),
        )

    def _window_decision_json(self, window_id: str) -> dict[str, Any]:
        row = self.store.fetchone(
            "SELECT decision_json FROM windows WHERE window_id = ?", (window_id,)
        )
        if row is None or row["decision_json"] is None:
            return {}
        return json_loads_or_empty(str(row["decision_json"]))


def json_loads_or_empty(value: str) -> dict[str, Any]:
    import json

    try:
        result = json.loads(value)
    except (ValueError, TypeError):
        return {}
    return result if isinstance(result, dict) else {}
