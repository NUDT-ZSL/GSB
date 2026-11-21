from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from typing import Any

from .config import (
    ConfigBundle,
    ModelSpec,
    PrecisionSpec,
    ReplicaSnapshot,
    ReplicaSpec,
    RequestSpec,
    canonical_json,
    digest_json,
    parse_model,
    parse_precision,
    parse_replicas,
    validate_cross_constraints,
)
from .errors import BusinessError
from .precision import (
    Estimator,
    sensitivity_chain,
    within_relative_tolerance,
)
from .storage import (
    append_ledger,
    file_lock,
    get_meta,
    open_database,
    reset_database,
    set_meta,
    state_exists,
    transaction,
)

AGGREGATE_ORDER = ["priority_desc", "arrival_ms_asc", "request_seq_asc"]


@dataclass
class WindowEvent:
    replica: ReplicaSpec
    version: int
    index: int
    start_ms: int
    end_ms: int


class FaultInjector:
    """Test-only process termination points. No wall clock is involved."""

    def __init__(self) -> None:
        self.mode = os.environ.get("BATCHKERNEL_FAULT", "")
        try:
            self.batch_target = int(
                os.environ.get("BATCHKERNEL_FAULT_BATCH_COUNT", "1")
            )
        except ValueError:
            self.batch_target = 1
        self.batch_hits = 0

    def hit(self, point: str) -> None:
        if self.mode != point:
            return
        if point == "after_batch_commit":
            self.batch_hits += 1
            if self.batch_hits < self.batch_target:
                return
        os._exit(91)


def _load_current_snapshot(connection: sqlite3.Connection) -> ReplicaSnapshot:
    row = connection.execute(
        """
        SELECT rv.version, rv.payload_json
        FROM replica_current rc
        JOIN replica_versions rv ON rv.version = rc.version
        WHERE rc.singleton = 1
        """
    ).fetchone()
    if row is None:
        raise BusinessError("E_STATE_CORRUPT", "replica snapshot pointer is missing")
    return parse_replicas(json.loads(row["payload_json"]))


def _snapshot_by_version(
    connection: sqlite3.Connection, version: int
) -> ReplicaSnapshot:
    row = connection.execute(
        "SELECT payload_json FROM replica_versions WHERE version = ?", (version,)
    ).fetchone()
    if row is None:
        raise BusinessError(
            "E_STATE_CORRUPT",
            "window references a missing replica version",
            {"version": version},
        )
    return parse_replicas(json.loads(row["payload_json"]))


def _load_model(connection: sqlite3.Connection) -> ModelSpec:
    value = get_meta(connection, "model_json")
    if value is None:
        raise BusinessError("E_STATE_CORRUPT", "model specification is missing")
    return parse_model(json.loads(value))


def _load_precision(connection: sqlite3.Connection) -> PrecisionSpec:
    value = get_meta(connection, "precision_json")
    if value is None:
        raise BusinessError("E_STATE_CORRUPT", "precision configuration is missing")
    return parse_precision(json.loads(value))


def _clock(connection: sqlite3.Connection) -> int:
    value = get_meta(connection, "logical_clock_ms")
    if value is None:
        raise BusinessError("E_STATE_CORRUPT", "logical clock watermark is missing")
    return int(value)


def _assign_replica(
    request_seq: int,
    declared_replica_id: str | None,
    replica_ids: list[str],
) -> str:
    if declared_replica_id is not None:
        if declared_replica_id not in replica_ids:
            raise BusinessError(
                "E_BAD_REQUEST_INPUT",
                "request references an unavailable replica",
                {
                    "request_seq": request_seq,
                    "replica_id": declared_replica_id,
                },
            )
        return declared_replica_id
    ordered = sorted(replica_ids)
    if not ordered:
        raise BusinessError("E_BAD_REPLICA_INPUT", "at least one replica is required")
    return ordered[request_seq % len(ordered)]


def _derive_next_watermark(
    connection: sqlite3.Connection,
    replica: ReplicaSpec,
    clock_ms: int,
) -> tuple[int, int, int]:
    previous = connection.execute(
        """
        SELECT window_index, end_ms
        FROM windows
        WHERE replica_id = ?
        ORDER BY window_index DESC
        LIMIT 1
        """,
        (replica.replica_id,),
    ).fetchone()
    if previous is not None:
        next_index = int(previous["window_index"]) + 1
        next_start_ms = int(previous["end_ms"])
    else:
        next_index = clock_ms // replica.batch_window_ms
        next_start_ms = clock_ms
    return (
        next_index,
        next_start_ms,
        next_start_ms + replica.batch_window_ms,
    )


def _request_to_json(request: RequestSpec) -> dict[str, Any]:
    return request.to_json()


def _insert_request(
    connection: sqlite3.Connection,
    request: RequestSpec,
    replica_ids: list[str],
    clock_ms: int,
) -> None:
    replica_id = _assign_replica(
        request.request_seq, request.replica_id, replica_ids
    )
    connection.execute(
        """
        INSERT INTO requests(
            request_seq, arrival_ms, priority, input_tokens, max_output_tokens,
            deadline_ms, declared_replica_id, replica_id, state, failure_count,
            attempt_count, remaining_failures, window_key, batch_key,
            completion_ms, updated_clock_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, 0, ?, NULL, NULL, NULL, ?)
        """,
        (
            request.request_seq,
            request.arrival_ms,
            request.priority,
            request.input_tokens,
            request.max_output_tokens,
            request.deadline_ms,
            request.replica_id,
            replica_id,
            request.simulate_failures,
            clock_ms,
        ),
    )
    append_ledger(
        connection,
        clock_ms=clock_ms,
        event_type="REQUEST_LOADED",
        request_seq=request.request_seq,
        to_state="pending",
        detail={"replica_id": replica_id, "request": _request_to_json(request)},
    )


def _install_initial_snapshot(
    connection: sqlite3.Connection,
    snapshot: ReplicaSnapshot,
    clock_ms: int,
) -> None:
    connection.execute(
        """
        INSERT INTO replica_versions(
            version, content_digest, payload_json, created_clock_ms, action
        ) VALUES (?, ?, ?, ?, 'initial')
        """,
        (
            snapshot.version,
            snapshot.content_digest,
            canonical_json(snapshot.to_json()),
            clock_ms,
        ),
    )
    connection.execute(
        "INSERT INTO replica_current(singleton, version) VALUES(1, ?)",
        (snapshot.version,),
    )
    for replica in snapshot.replicas:
        connection.execute(
            """
            INSERT INTO replica_watermarks(
                replica_id, current_version, next_window_index,
                next_window_start_ms, next_window_end_ms, updated_clock_ms
            ) VALUES (?, ?, 0, 0, ?, ?)
            """,
            (
                replica.replica_id,
                snapshot.version,
                replica.batch_window_ms,
                clock_ms,
            ),
        )
        connection.execute(
            """
            INSERT INTO replica_usage(
                replica_id, replica_version, compute_fixed, memory_fixed,
                updated_clock_ms
            ) VALUES (?, ?, 0, 0, ?)
            """,
            (replica.replica_id, snapshot.version, clock_ms),
        )
    append_ledger(
        connection,
        clock_ms=clock_ms,
        event_type="REPLICA_SNAPSHOT",
        detail={
            "version": snapshot.version,
            "action": "initial",
            "replicas": [replica.to_json() for replica in snapshot.replicas],
        },
    )


def _classify_replica_change(
    current: ReplicaSnapshot, incoming: ReplicaSnapshot
) -> str:
    current_by_id = current.by_id
    incoming_by_id = incoming.by_id
    if set(current_by_id).issubset(incoming_by_id):
        if all(
            current_by_id[replica_id].to_json()
            == incoming_by_id[replica_id].to_json()
            for replica_id in current_by_id
        ):
            return "incremental_append"
    return "full_replace"


def _reassign_pending_requests(
    connection: sqlite3.Connection,
    snapshot: ReplicaSnapshot,
    clock_ms: int,
) -> None:
    replica_ids = list(snapshot.by_id)
    rows = connection.execute(
        """
        SELECT r.*
        FROM requests r
        WHERE r.state = 'pending'
          AND r.window_key IS NULL
          AND r.replica_id NOT IN ({})
          AND NOT EXISTS (
              SELECT 1 FROM window_requests wr WHERE wr.request_seq = r.request_seq
          )
        ORDER BY r.request_seq
        """.format(",".join("?" for _ in replica_ids)),
        tuple(replica_ids),
    ).fetchall()
    for row in rows:
        new_replica = _assign_replica(
            int(row["request_seq"]), row["declared_replica_id"], replica_ids
        )
        connection.execute(
            "UPDATE requests SET replica_id = ?, updated_clock_ms = ? "
            "WHERE request_seq = ?",
            (new_replica, clock_ms, row["request_seq"]),
        )
        append_ledger(
            connection,
            clock_ms=clock_ms,
            event_type="REQUEST_REASSIGNED",
            request_seq=int(row["request_seq"]),
            detail={
                "from_replica_id": row["replica_id"],
                "to_replica_id": new_replica,
                "reason": "replica_snapshot_replacement",
            },
        )


def _install_replica_snapshot(
    connection: sqlite3.Connection,
    incoming: ReplicaSnapshot,
    clock_ms: int,
) -> dict[str, Any]:
    current = _load_current_snapshot(connection)
    if incoming.version < current.version:
        raise BusinessError(
            "E_REPLICA_VERSION_ROLLBACK",
            "replica snapshot version rollback is forbidden",
            {
                "current_version": current.version,
                "requested_version": incoming.version,
            },
        )
    if incoming.content_digest == current.content_digest:
        return {
            "action": "no_op",
            "requested_version": incoming.version,
            "effective_version": current.version,
        }
    if incoming.version == current.version:
        raise BusinessError(
            "E_REPLICA_VERSION_CONFLICT",
            "different replica content requires a new version number",
            {"current_version": current.version},
        )
    action = _classify_replica_change(current, incoming)
    connection.execute(
        """
        INSERT INTO replica_versions(
            version, content_digest, payload_json, created_clock_ms, action
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            incoming.version,
            incoming.content_digest,
            canonical_json(incoming.to_json()),
            clock_ms,
            action,
        ),
    )
    connection.execute(
        "UPDATE replica_current SET version = ? WHERE singleton = 1",
        (incoming.version,),
    )
    incoming_ids = set(incoming.by_id)
    for replica in incoming.replicas:
        row = connection.execute(
            "SELECT * FROM replica_watermarks WHERE replica_id = ?",
            (replica.replica_id,),
        ).fetchone()
        if row is None:
            next_index, next_start_ms, next_end_ms = _derive_next_watermark(
                connection, replica, clock_ms
            )
            connection.execute(
                """
                INSERT INTO replica_watermarks(
                    replica_id, current_version, next_window_index,
                    next_window_start_ms, next_window_end_ms, updated_clock_ms
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    replica.replica_id,
                    incoming.version,
                    next_index,
                    next_start_ms,
                    next_end_ms,
                    clock_ms,
                ),
            )
        else:
            next_index = int(row["next_window_index"])
            next_start_ms = int(row["next_window_start_ms"])
            connection.execute(
                """
                UPDATE replica_watermarks
                SET current_version = ?, next_window_start_ms = ?,
                    next_window_end_ms = ?, updated_clock_ms = ?
                WHERE replica_id = ?
                """,
                (
                    incoming.version,
                    next_start_ms,
                    next_start_ms + replica.batch_window_ms,
                    clock_ms,
                    replica.replica_id,
                ),
            )
        connection.execute(
            """
            INSERT INTO replica_usage(
                replica_id, replica_version, compute_fixed, memory_fixed,
                updated_clock_ms
            ) VALUES (?, ?, 0, 0, ?)
            ON CONFLICT(replica_id) DO UPDATE SET
                replica_version = excluded.replica_version,
                updated_clock_ms = excluded.updated_clock_ms
            """,
            (replica.replica_id, incoming.version, clock_ms),
        )
    if incoming_ids:
        placeholders = ",".join("?" for _ in incoming_ids)
        connection.execute(
            f"DELETE FROM replica_watermarks WHERE replica_id NOT IN ({placeholders})",
            tuple(sorted(incoming_ids)),
        )
    _reassign_pending_requests(connection, incoming, clock_ms)
    append_ledger(
        connection,
        clock_ms=clock_ms,
        event_type="REPLICA_SNAPSHOT",
        detail={
            "version": incoming.version,
            "previous_version": current.version,
            "action": action,
            "replicas": [replica.to_json() for replica in incoming.replicas],
        },
    )
    return {
        "action": action,
        "requested_version": incoming.version,
        "effective_version": incoming.version,
    }


def _merge_requests(
    connection: sqlite3.Connection,
    requests: tuple[RequestSpec, ...],
    snapshot: ReplicaSnapshot,
    clock_ms: int,
) -> tuple[int, int]:
    inserted = 0
    unchanged = 0
    for request in requests:
        row = connection.execute(
            "SELECT * FROM requests WHERE request_seq = ?",
            (request.request_seq,),
        ).fetchone()
        if row is None:
            _insert_request(connection, request, list(snapshot.by_id), clock_ms)
            inserted += 1
            continue
        existing = {
            "request_seq": int(row["request_seq"]),
            "arrival_ms": int(row["arrival_ms"]),
            "priority": int(row["priority"]),
            "input_tokens": int(row["input_tokens"]),
            "max_output_tokens": int(row["max_output_tokens"]),
            "deadline_ms": int(row["deadline_ms"]),
            "replica_id": row["declared_replica_id"],
            "simulate_failures": int(row["remaining_failures"])
            + int(row["failure_count"]),
        }
        if existing != request.to_json():
            raise BusinessError(
                "E_REQUESTS_CONFLICT",
                "an existing request has different immutable content",
                {
                    "request_seq": request.request_seq,
                    "existing": existing,
                    "incoming": request.to_json(),
                },
            )
        unchanged += 1
    return inserted, unchanged


def load_configuration(
    state_dir: str,
    bundle: ConfigBundle,
    initial_clock_ms: int | None,
    reset: bool,
) -> dict[str, Any]:
    present = state_exists(state_dir)
    required = (
        bundle.model is not None
        and bundle.replicas is not None
        and bundle.requests is not None
        and bundle.precision is not None
    )
    if reset and not required:
        raise BusinessError(
            "E_BAD_LOAD_INPUT",
            "reset load requires model, replicas, requests and precision",
        )
    if not present and not required:
        raise BusinessError(
            "E_BAD_LOAD_INPUT",
            "initial load requires model, replicas, requests and precision",
        )
    if present and not any(
        (
            bundle.model,
            bundle.replicas,
            bundle.requests,
            bundle.precision,
        )
    ):
        raise BusinessError("E_BAD_LOAD_INPUT", "no input files were supplied")
    if initial_clock_ms is not None and (
        isinstance(initial_clock_ms, bool)
        or not isinstance(initial_clock_ms, int)
        or initial_clock_ms < 0
    ):
        raise BusinessError(
            "E_REQUEST_CLOCK_INVALID",
            "initial logical clock must be a non-negative integer",
        )

    with file_lock(state_dir, exclusive=True):
        if reset:
            reset_database(state_dir)
        connection = open_database(state_dir, create=True)
        try:
            with transaction(connection):
                fresh = get_meta(connection, "model_json") is None
                if fresh:
                    assert bundle.model is not None
                    assert bundle.replicas is not None
                    assert bundle.requests is not None
                    assert bundle.precision is not None
                    clock_ms = 0 if initial_clock_ms is None else initial_clock_ms
                    set_meta(connection, "logical_clock_ms", clock_ms)
                    set_meta(connection, "model_json", canonical_json(bundle.model.to_json()))
                    set_meta(connection, "model_digest", digest_json(bundle.model.to_json()))
                    set_meta(
                        connection,
                        "precision_json",
                        canonical_json(bundle.precision.to_json()),
                    )
                    set_meta(
                        connection,
                        "precision_digest",
                        digest_json(bundle.precision.to_json()),
                    )
                    _install_initial_snapshot(connection, bundle.replicas, clock_ms)
                    for request in bundle.requests:
                        _insert_request(
                            connection,
                            request,
                            list(bundle.replicas.by_id),
                            clock_ms,
                        )
                    validate_cross_constraints(bundle.replicas, bundle.requests)
                    result = {
                        "action": "initial",
                        "clock_ms": clock_ms,
                        "replica_version": bundle.replicas.version,
                        "request_count": len(bundle.requests),
                    }
                else:
                    current_clock = _clock(connection)
                    if (
                        initial_clock_ms is not None
                        and initial_clock_ms != current_clock
                    ):
                        raise BusinessError(
                            "E_CLOCK_INJECTION_ONLY",
                            "load cannot advance or rewind the scheduler clock",
                            {
                                "current_clock_ms": current_clock,
                                "requested_clock_ms": initial_clock_ms,
                            },
                        )
                    if bundle.model is not None:
                        if get_meta(connection, "model_digest") != digest_json(
                            bundle.model.to_json()
                        ):
                            raise BusinessError(
                                "E_IMMUTABLE_CONFIG_CONFLICT",
                                "model configuration is immutable after load",
                            )
                    if bundle.precision is not None:
                        if get_meta(
                            connection, "precision_digest"
                        ) != digest_json(bundle.precision.to_json()):
                            raise BusinessError(
                                "E_IMMUTABLE_CONFIG_CONFLICT",
                                "precision configuration is immutable after load",
                            )
                    snapshot = _load_current_snapshot(connection)
                    replica_result = {
                        "action": "no_op",
                        "requested_version": snapshot.version,
                        "effective_version": snapshot.version,
                    }
                    if bundle.replicas is not None:
                        replica_result = _install_replica_snapshot(
                            connection, bundle.replicas, current_clock
                        )
                        snapshot = _load_current_snapshot(connection)
                    inserted = 0
                    unchanged = 0
                    if bundle.requests is not None:
                        inserted, unchanged = _merge_requests(
                            connection, bundle.requests, snapshot, current_clock
                        )
                    all_rows = connection.execute(
                        "SELECT * FROM requests ORDER BY request_seq"
                    ).fetchall()
                    all_requests: list[RequestSpec] = []
                    for row in all_rows:
                        all_requests.append(
                            RequestSpec(
                                request_seq=int(row["request_seq"]),
                                arrival_ms=int(row["arrival_ms"]),
                                priority=int(row["priority"]),
                                input_tokens=int(row["input_tokens"]),
                                max_output_tokens=int(row["max_output_tokens"]),
                                deadline_ms=int(row["deadline_ms"]),
                                replica_id=row["declared_replica_id"],
                                simulate_failures=int(row["remaining_failures"])
                                + int(row["failure_count"]),
                                raw={
                                    "request_seq": int(row["request_seq"]),
                                    "arrival_ms": int(row["arrival_ms"]),
                                    "priority": int(row["priority"]),
                                    "input_tokens": int(row["input_tokens"]),
                                    "max_output_tokens": int(row["max_output_tokens"]),
                                    "deadline_ms": int(row["deadline_ms"]),
                                    "replica_id": row["declared_replica_id"],
                                    "simulate_failures": int(
                                        row["remaining_failures"]
                                    )
                                    + int(row["failure_count"]),
                                },
                            )
                        )
                    validate_cross_constraints(snapshot, tuple(all_requests))
                    result = {
                        "action": "update",
                        "clock_ms": current_clock,
                        "replica_version": snapshot.version,
                        "replica_change": replica_result,
                        "requests_inserted": inserted,
                        "requests_unchanged": unchanged,
                    }
            return result
        finally:
            connection.close()


def _ensure_watermarks(
    connection: sqlite3.Connection,
    snapshot: ReplicaSnapshot,
    clock_ms: int,
) -> None:
    for replica in snapshot.replicas:
        row = connection.execute(
            "SELECT * FROM replica_watermarks WHERE replica_id = ?",
            (replica.replica_id,),
        ).fetchone()
        if row is None:
            next_index, next_start_ms, next_end_ms = _derive_next_watermark(
                connection, replica, clock_ms
            )
            connection.execute(
                """
                INSERT INTO replica_watermarks(
                    replica_id, current_version, next_window_index,
                    next_window_start_ms, next_window_end_ms, updated_clock_ms
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    replica.replica_id,
                    snapshot.version,
                    next_index,
                    next_start_ms,
                    next_end_ms,
                    clock_ms,
                ),
            )


def _next_window_event(
    connection: sqlite3.Connection,
    snapshot: ReplicaSnapshot,
    target_ms: int,
) -> WindowEvent | None:
    candidates: list[WindowEvent] = []
    for replica in snapshot.replicas:
        row = connection.execute(
            "SELECT * FROM replica_watermarks WHERE replica_id = ?",
            (replica.replica_id,),
        ).fetchone()
        if row is None:
            raise BusinessError(
                "E_STATE_CORRUPT",
                "replica watermark is missing",
                {"replica_id": replica.replica_id},
            )
        end_ms = int(row["next_window_end_ms"])
        if end_ms <= target_ms:
            candidates.append(
                WindowEvent(
                    replica=replica,
                    version=snapshot.version,
                    index=int(row["next_window_index"]),
                    start_ms=int(row["next_window_start_ms"]),
                    end_ms=end_ms,
                )
            )
    if not candidates:
        return None
    candidates.sort(key=lambda event: (event.end_ms, event.replica.replica_id))
    return candidates[0]


def _compose_batch_plans(
    candidates: list[tuple[RequestSpec, dict[str, Any]]],
    replica: ReplicaSpec,
    precision: PrecisionSpec,
    estimator: Estimator,
) -> list[dict[str, Any]]:
    if not candidates:
        return []
    pending = list(candidates)
    plans: list[dict[str, Any]] = []
    while pending:
        selected: list[tuple[RequestSpec, dict[str, Any]]] = []
        skipped: list[dict[str, Any]] = []
        skipped_candidates: list[tuple[RequestSpec, dict[str, Any]]] = []
        observations: list[dict[str, Any]] = []
        sensitive: list[dict[str, Any]] = []
        selected_seqs: set[int] = set()
        compute_sum = 0.0
        memory_sum = 0.0
        cursor = 0
        while cursor < len(pending):
            if len(selected) >= replica.max_batch_size:
                break
            request, estimate = pending[cursor]
            request_compute = estimate["compute"].value
            request_memory = estimate["memory"].value
            remaining_compute = replica.compute_units - compute_sum
            remaining_memory = float(replica.memory_limit_mib) - memory_sum
            new_compute = compute_sum + request_compute
            new_memory = memory_sum + request_memory
            if request.total_tokens > replica.max_sequence_length:
                raise BusinessError(
                    "E_BATCH_UNSCHEDULABLE",
                    "request exceeds replica sequence length",
                    {
                        "request_seq": request.request_seq,
                        "replica_id": replica.replica_id,
                        "request_tokens": request.total_tokens,
                        "max_sequence_length": replica.max_sequence_length,
                    },
                )
            if within_relative_tolerance(
                remaining_compute,
                request_compute,
                precision.relative_tolerance,
            ):
                sensitive.append(
                    sensitivity_chain(
                        "batch_budget_check",
                        "compute",
                        "remaining_compute",
                        remaining_compute,
                        "request_compute",
                        request_compute,
                        precision.relative_tolerance,
                        estimate["compute"].chain,
                    )
                )
            if within_relative_tolerance(
                remaining_memory,
                request_memory,
                precision.relative_tolerance,
            ):
                sensitive.append(
                    sensitivity_chain(
                        "batch_budget_check",
                        "memory",
                        "remaining_memory_mib",
                        remaining_memory,
                        "request_memory_mib",
                        request_memory,
                        precision.relative_tolerance,
                        estimate["memory"].chain,
                    )
                )
            first_trigger: str | None = None
            if new_compute > replica.compute_units:
                first_trigger = "compute"
            elif new_memory > float(replica.memory_limit_mib):
                first_trigger = "memory"
            observation = {
                "request_seq": request.request_seq,
                "priority": request.priority,
                "input_tokens": request.input_tokens,
                "max_output_tokens": request.max_output_tokens,
                "token_sum": request.total_tokens,
                "remaining_compute_before": remaining_compute,
                "request_compute": request_compute,
                "aggregate_compute_after": new_compute,
                "remaining_memory_mib_before": remaining_memory,
                "request_memory_mib": request_memory,
                "aggregate_memory_mib_after": new_memory,
                "decision": "selected",
            }
            if first_trigger is not None:
                observation["decision"] = "skipped"
                observation["first_trigger"] = first_trigger
                skipped.append(
                    {
                        "request_seq": request.request_seq,
                        "priority": request.priority,
                        "input_tokens": request.input_tokens,
                        "max_output_tokens": request.max_output_tokens,
                        "token_sum": request.total_tokens,
                        "first_trigger": first_trigger,
                        "remaining_compute_before": remaining_compute,
                        "request_compute": request_compute,
                        "aggregate_compute_after": new_compute,
                        "remaining_memory_mib_before": remaining_memory,
                        "request_memory_mib": request_memory,
                        "aggregate_memory_mib_after": new_memory,
                        "observation_order": len(observations),
                    }
                )
                skipped_candidates.append((request, estimate))
                observations.append(observation)
                cursor += 1
                continue
            selected.append((request, estimate))
            selected_seqs.add(request.request_seq)
            compute_sum = new_compute
            memory_sum = new_memory
            observations.append(observation)
            cursor += 1
        if not selected:
            request, estimate = pending[0]
            raise BusinessError(
                "E_BATCH_UNSCHEDULABLE",
                "first pending request cannot fit any batch under replica budgets",
                {
                    "request_seq": request.request_seq,
                    "replica_id": replica.replica_id,
                    "request_compute": estimate["compute"].value,
                    "replica_compute_units": replica.compute_units,
                    "request_memory_mib": estimate["memory"].value,
                    "replica_memory_limit_mib": replica.memory_limit_mib,
                    "first_trigger": "compute"
                    if estimate["compute"].value > replica.compute_units
                    else "memory",
                },
            )
        batch_estimates = estimator.batch_estimate(
            selected, replica, AGGREGATE_ORDER
        )
        for skipped_request, skipped_estimate in skipped_candidates:
            alternative_selected = selected[:-1] + [
                (skipped_request, skipped_estimate)
            ]
            alternative_compute = sum(
                item_estimate["compute"].value
                for _, item_estimate in alternative_selected
            )
            alternative_memory = sum(
                item_estimate["memory"].value
                for _, item_estimate in alternative_selected
            )
            if (
                alternative_compute > replica.compute_units
                or alternative_memory > float(replica.memory_limit_mib)
                or len(alternative_selected) > replica.max_batch_size
            ):
                continue
            alternative_estimates = estimator.batch_estimate(
                alternative_selected,
                replica,
                AGGREGATE_ORDER + ["alternative_swap"],
            )
            for metric in ("compute", "memory", "queue"):
                actual_value = batch_estimates[metric].value
                alternative_value = alternative_estimates[metric].value
                if not within_relative_tolerance(
                    actual_value,
                    alternative_value,
                    precision.relative_tolerance,
                ):
                    continue
                decision = sensitivity_chain(
                    "candidate_batch_alternative",
                    metric,
                    "actual_batch_" + metric,
                    actual_value,
                    "alternative_batch_" + metric,
                    alternative_value,
                    precision.relative_tolerance,
                    list(batch_estimates[metric].chain)
                    + ["alternative_batch"]
                    + list(alternative_estimates[metric].chain),
                )
                decision["left"]["request_seqs"] = [
                    item.request_seq for item, _ in selected
                ]
                decision["right"]["request_seqs"] = [
                    item.request_seq for item, _ in alternative_selected
                ]
                decision["replaced_request_seq"] = selected[-1][0].request_seq
                decision["alternative_request_seq"] = skipped_request.request_seq
                sensitive.append(decision)
        plans.append(
            {
                "selected": selected,
                "estimates": batch_estimates,
                "skipped": skipped,
                "observations": observations,
                "sensitive": sensitive,
                "remaining_compute_after": replica.compute_units - compute_sum,
                "remaining_memory_mib_after": float(replica.memory_limit_mib)
                - memory_sum,
            }
        )
        pending = [
            item for item in pending if item[0].request_seq not in selected_seqs
        ]
    return plans


def _insert_precision_batch_plan(
    connection: sqlite3.Connection,
    window_key: str,
    batch_key: str,
    batch_index: int,
    window_end_ms: int,
    replica: ReplicaSpec,
    version: int,
    plan: dict[str, Any],
    precision: PrecisionSpec,
    estimator: Estimator,
) -> dict[str, Any]:
    records = estimator.batch_precision_records(plan["estimates"], AGGREGATE_ORDER)
    record_by_metric = {record["metric"]: record for record in records}
    queue_record = record_by_metric["queue"]
    completion_ms = window_end_ms + (
        queue_record["fixed_value"] + precision.queue_scale - 1
    ) // precision.queue_scale
    request_seqs = [request.request_seq for request, _ in plan["selected"]]
    decision = {
        "aggregate_order": AGGREGATE_ORDER,
        "replica_observation": {
            "replica_id": replica.replica_id,
            "replica_version": version,
            "compute_units": replica.compute_units,
            "memory_limit_mib": replica.memory_limit_mib,
            "max_batch_size": replica.max_batch_size,
            "max_sequence_length": replica.max_sequence_length,
            "remaining_compute_after": plan["remaining_compute_after"],
            "remaining_memory_mib_after": plan["remaining_memory_mib_after"],
        },
        "request_seqs": request_seqs,
        "observations": plan["observations"],
        "skipped": plan["skipped"],
        "precision_sensitive_decisions": plan["sensitive"],
        "estimated_completion_ms": completion_ms,
    }
    connection.execute(
        """
        INSERT INTO batches(
            batch_key, window_key, batch_index, replica_id, replica_version,
            state, executed, compute_fixed, memory_fixed, queue_fixed,
            compute_raw, memory_raw, queue_raw, compute_error_bound,
            memory_error_bound, queue_error_bound, precision_sensitive,
            decision_json, estimated_completion_ms, window_end_ms
        ) VALUES (?, ?, ?, ?, ?, 'planned', 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            batch_key,
            window_key,
            batch_index,
            replica.replica_id,
            version,
            record_by_metric["compute"]["fixed_value"],
            record_by_metric["memory"]["fixed_value"],
            record_by_metric["queue"]["fixed_value"],
            record_by_metric["compute"]["raw_value"],
            record_by_metric["memory"]["raw_value"],
            record_by_metric["queue"]["raw_value"],
            record_by_metric["compute"]["total_error_bound"],
            record_by_metric["memory"]["total_error_bound"],
            record_by_metric["queue"]["total_error_bound"],
            1 if plan["sensitive"] else 0,
            canonical_json(decision),
            completion_ms,
            window_end_ms,
        ),
    )
    for ordinal, (request, _) in enumerate(plan["selected"]):
        connection.execute(
            "INSERT INTO batch_requests(batch_key, request_seq, ordinal) "
            "VALUES (?, ?, ?)",
            (batch_key, request.request_seq, ordinal),
        )
    for record in records:
        connection.execute(
            """
            INSERT INTO precision_records(
                batch_key, window_key, metric, scale, raw_value, fixed_value,
                rounding_direction, quantization_error_bound,
                arithmetic_error_bound, total_error_bound, error_budget,
                within_error_budget, source_chain_json, operation_order_json,
                aggregate_order_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                batch_key,
                window_key,
                record["metric"],
                record["scale"],
                record["raw_value"],
                record["fixed_value"],
                record["rounding_direction"],
                record["quantization_error_bound"],
                record["arithmetic_error_bound"],
                record["total_error_bound"],
                record["error_budget"],
                1 if record["within_error_budget"] else 0,
                canonical_json(record["source_chain"]),
                canonical_json(record["operation_order"]),
                canonical_json(record["aggregate_order"]),
            ),
        )
    for sensitive in plan["sensitive"]:
        connection.execute(
            """
            INSERT INTO sensitive_decisions(
                batch_key, window_key, decision, metric, tolerance, left_json,
                right_json, absolute_gap, source_chain_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                batch_key,
                window_key,
                sensitive["decision"],
                sensitive["metric"],
                sensitive["relative_tolerance"],
                canonical_json(sensitive["left"]),
                canonical_json(sensitive["right"]),
                sensitive["absolute_gap"],
                canonical_json(sensitive["source_chain"]),
            ),
        )
    return decision


def _append_error_budget_sensitivities(
    batch_plan: dict[str, Any],
    precision: PrecisionSpec,
    estimator: Estimator,
) -> None:
    records = estimator.batch_precision_records(
        batch_plan["estimates"], AGGREGATE_ORDER
    )
    for record in records:
        if record["within_error_budget"]:
            continue
        batch_plan["sensitive"].append(
            {
                "decision": "error_budget_exceeded",
                "metric": record["metric"],
                "left": {
                    "name": "total_error_bound",
                    "value": record["total_error_bound"],
                },
                "right": {
                    "name": "error_budget",
                    "value": record["error_budget"],
                },
                "absolute_gap": abs(
                    record["total_error_bound"] - record["error_budget"]
                ),
                "relative_tolerance": precision.relative_tolerance,
                "near_tolerance_boundary": False,
                "depends_on_difference": False,
                "source_chain": list(record["source_chain"])
                + ["error_budget_exceeded"],
            }
        )


def _plan_window(
    connection: sqlite3.Connection,
    model: ModelSpec,
    precision: PrecisionSpec,
    event: WindowEvent,
    fault: FaultInjector,
) -> str:
    window_key = (
        f"{event.replica.replica_id}:v{event.version}:w{event.index}"
    )
    existing = connection.execute(
        "SELECT window_key FROM windows WHERE replica_id = ? AND window_index = ?",
        (event.replica.replica_id, event.index),
    ).fetchone()
    if existing is not None:
        return str(existing["window_key"])

    rows = connection.execute(
        """
        SELECT r.*
        FROM requests r
        WHERE r.replica_id = ?
          AND r.state = 'pending'
          AND r.window_key IS NULL
          AND r.arrival_ms < ?
          AND NOT EXISTS (
              SELECT 1 FROM window_requests wr WHERE wr.request_seq = r.request_seq
          )
        ORDER BY r.request_seq
        """,
        (event.replica.replica_id, event.end_ms),
    ).fetchall()
    estimator = Estimator(model, precision)
    active: list[tuple[RequestSpec, dict[str, Any]]] = []
    expired: list[tuple[RequestSpec, dict[str, Any]]] = []
    for row in rows:
        request = RequestSpec(
            request_seq=int(row["request_seq"]),
            arrival_ms=int(row["arrival_ms"]),
            priority=int(row["priority"]),
            input_tokens=int(row["input_tokens"]),
            max_output_tokens=int(row["max_output_tokens"]),
            deadline_ms=int(row["deadline_ms"]),
            replica_id=row["declared_replica_id"],
            simulate_failures=int(row["remaining_failures"]),
            raw={},
        )
        estimate = estimator.request_estimate(request)
        if request.deadline_ms < event.end_ms:
            expired.append((request, estimate))
        else:
            active.append((request, estimate))
    active.sort(
        key=lambda item: (
            -item[0].priority,
            item[0].arrival_ms,
            item[0].request_seq,
        )
    )
    batch_plans = _compose_batch_plans(
        active, event.replica, precision, estimator
    )
    for batch_plan in batch_plans:
        _append_error_budget_sensitivities(batch_plan, precision, estimator)
    candidate_order = [
        {
            "request_seq": request.request_seq,
            "priority": request.priority,
            "arrival_ms": request.arrival_ms,
            "input_tokens": request.input_tokens,
            "max_output_tokens": request.max_output_tokens,
            "token_sum": request.total_tokens,
            "compute_raw": estimate["compute"].value,
            "memory_raw": estimate["memory"].value,
        }
        for request, estimate in active
    ]
    expired_plan = [
        {
            "request_seq": request.request_seq,
            "deadline_ms": request.deadline_ms,
            "window_end_ms": event.end_ms,
            "reason": "deadline_before_window_close",
        }
        for request, _ in sorted(expired, key=lambda item: item[0].request_seq)
    ]
    plan_json = {
        "window_key": window_key,
        "replica_observation": {
            "replica_id": event.replica.replica_id,
            "replica_version": event.version,
            "compute_units": event.replica.compute_units,
            "memory_limit_mib": event.replica.memory_limit_mib,
            "batch_window_ms": event.replica.batch_window_ms,
            "max_batch_size": event.replica.max_batch_size,
            "max_sequence_length": event.replica.max_sequence_length,
        },
        "start_ms": event.start_ms,
        "end_ms": event.end_ms,
        "aggregate_order": AGGREGATE_ORDER,
        "candidate_order": candidate_order,
        "expired_requests": expired_plan,
        "batches": [
            {
                "batch_index": index,
                "request_seqs": [
                    request.request_seq for request, _ in batch_plan["selected"]
                ],
                "skipped": batch_plan["skipped"],
                "precision_sensitive": bool(batch_plan["sensitive"]),
            }
            for index, batch_plan in enumerate(batch_plans)
        ],
    }
    connection.execute(
        """
        INSERT INTO windows(
            window_key, replica_id, replica_version, window_index,
            start_ms, end_ms, state, plan_json, planned_clock_ms,
            committed_clock_ms
        ) VALUES (?, ?, ?, ?, ?, ?, 'planned', ?, ?, NULL)
        """,
        (
            window_key,
            event.replica.replica_id,
            event.version,
            event.index,
            event.start_ms,
            event.end_ms,
            canonical_json(plan_json),
            event.end_ms,
        ),
    )
    ordinal = 0
    for request, _ in sorted(expired, key=lambda item: item[0].request_seq):
        connection.execute(
            """
            INSERT INTO window_requests(
                window_key, request_seq, batch_index, ordinal, disposition
            ) VALUES (?, ?, NULL, ?, 'expired')
            """,
            (window_key, request.request_seq, ordinal),
        )
        ordinal += 1
    for batch_index, batch_plan in enumerate(batch_plans):
        batch_key = f"{window_key}:b{batch_index}"
        _insert_precision_batch_plan(
            connection,
            window_key,
            batch_key,
            batch_index,
            event.end_ms,
            event.replica,
            event.version,
            batch_plan,
            precision,
            estimator,
        )
        for request, _ in batch_plan["selected"]:
            connection.execute(
                """
                INSERT INTO window_requests(
                    window_key, request_seq, batch_index, ordinal, disposition
                ) VALUES (?, ?, ?, ?, 'batch')
                """,
                (window_key, request.request_seq, batch_index, ordinal),
            )
            ordinal += 1
    connection.execute(
        """
        UPDATE replica_watermarks
        SET next_window_index = ?, next_window_start_ms = ?,
            next_window_end_ms = ?, updated_clock_ms = ?
        WHERE replica_id = ?
        """,
        (
            event.index + 1,
            event.end_ms,
            event.end_ms + event.replica.batch_window_ms,
            event.end_ms,
            event.replica.replica_id,
        ),
    )
    append_ledger(
        connection,
        clock_ms=event.end_ms,
        event_type="WINDOW_PLANNED",
        replica_id=event.replica.replica_id,
        replica_version=event.version,
        window_key=window_key,
        detail={
            "window_index": event.index,
            "batch_count": len(batch_plans),
            "expired_count": len(expired),
            "candidate_order": plan_json["candidate_order"],
        },
    )
    fault.hit("during_plan_build")
    return window_key


def _apply_batch(
    connection: sqlite3.Connection,
    batch_key: str,
    window_key: str,
    clock_ms: int,
    fault: FaultInjector,
) -> None:
    with transaction(connection):
        batch = connection.execute(
            "SELECT * FROM batches WHERE batch_key = ?", (batch_key,)
        ).fetchone()
        if batch is None:
            raise BusinessError(
                "E_STATE_CORRUPT", "planned batch disappeared", {"batch_key": batch_key}
            )
        if batch["state"] == "committed":
            return
        updated = connection.execute(
            "UPDATE batches SET state = 'committed' "
            "WHERE batch_key = ? AND state = 'planned'",
            (batch_key,),
        ).rowcount
        if updated != 1:
            return
        request_rows = connection.execute(
            """
            SELECT r.*, br.ordinal AS batch_ordinal
            FROM batch_requests br
            JOIN requests r ON r.request_seq = br.request_seq
            WHERE br.batch_key = ?
            ORDER BY br.ordinal
            """,
            (batch_key,),
        ).fetchall()
        for row in request_rows:
            if row["state"] != "pending":
                raise BusinessError(
                    "E_RECOVERY_STATE_CONFLICT",
                    "batch contains a request that is not pending",
                    {
                        "batch_key": batch_key,
                        "request_seq": int(row["request_seq"]),
                        "state": row["state"],
                    },
                )
            connection.execute(
                """
                UPDATE requests
                SET state = 'batched', window_key = ?, batch_key = ?,
                    updated_clock_ms = ?
                WHERE request_seq = ? AND state = 'pending'
                """,
                (window_key, batch_key, clock_ms, row["request_seq"]),
            )
            append_ledger(
                connection,
                clock_ms=clock_ms,
                event_type="REQUEST_BATCHED",
                replica_id=batch["replica_id"],
                replica_version=int(batch["replica_version"]),
                window_key=window_key,
                batch_key=batch_key,
                request_seq=int(row["request_seq"]),
                from_state="pending",
                to_state="batched",
                failure_count_before=int(row["failure_count"]),
                failure_count_after=int(row["failure_count"]),
                detail={"batch_ordinal": int(row["batch_ordinal"])},
            )
        connection.execute(
            """
            INSERT INTO replica_usage(
                replica_id, replica_version, compute_fixed, memory_fixed,
                updated_clock_ms
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(replica_id) DO UPDATE SET
                replica_version = excluded.replica_version,
                compute_fixed = replica_usage.compute_fixed + excluded.compute_fixed,
                memory_fixed = replica_usage.memory_fixed + excluded.memory_fixed,
                updated_clock_ms = excluded.updated_clock_ms
            """,
            (
                batch["replica_id"],
                int(batch["replica_version"]),
                int(batch["compute_fixed"]),
                int(batch["memory_fixed"]),
                clock_ms,
            ),
        )
        append_ledger(
            connection,
            clock_ms=clock_ms,
            event_type="BATCH_COMMITTED",
            replica_id=batch["replica_id"],
            replica_version=int(batch["replica_version"]),
            window_key=window_key,
            batch_key=batch_key,
            detail={
                "request_seqs": [int(row["request_seq"]) for row in request_rows],
                "compute_fixed": int(batch["compute_fixed"]),
                "memory_fixed": int(batch["memory_fixed"]),
                "estimated_completion_ms": int(batch["estimated_completion_ms"]),
            },
        )
        fault.hit("during_batch_commit")
    fault.hit("after_batch_commit")


def _apply_window(
    connection: sqlite3.Connection,
    window_key: str,
    fault: FaultInjector,
) -> None:
    window = connection.execute(
        "SELECT * FROM windows WHERE window_key = ?", (window_key,)
    ).fetchone()
    if window is None:
        raise BusinessError(
            "E_STATE_CORRUPT", "window does not exist", {"window_key": window_key}
        )
    if window["state"] == "committed":
        return
    with transaction(connection):
        updated = connection.execute(
            "UPDATE windows SET state = 'applying' WHERE window_key = ? AND state = 'planned'",
            (window_key,),
        ).rowcount
        if updated == 1:
            append_ledger(
                connection,
                clock_ms=int(window["end_ms"]),
                event_type="WINDOW_APPLYING",
                replica_id=window["replica_id"],
                replica_version=int(window["replica_version"]),
                window_key=window_key,
                detail={"window_index": int(window["window_index"])},
            )
    batches = connection.execute(
        "SELECT batch_key FROM batches WHERE window_key = ? ORDER BY batch_index",
        (window_key,),
    ).fetchall()
    for batch in batches:
        _apply_batch(
            connection,
            str(batch["batch_key"]),
            window_key,
            int(window["end_ms"]),
            fault,
        )
    with transaction(connection):
        expired_rows = connection.execute(
            """
            SELECT r.*, wr.ordinal
            FROM window_requests wr
            JOIN requests r ON r.request_seq = wr.request_seq
            WHERE wr.window_key = ? AND wr.disposition = 'expired'
            ORDER BY wr.ordinal
            """,
            (window_key,),
        ).fetchall()
        for row in expired_rows:
            if row["state"] != "pending":
                continue
            connection.execute(
                """
                UPDATE requests
                SET state = 'expired', window_key = ?, updated_clock_ms = ?
                WHERE request_seq = ? AND state = 'pending'
                """,
                (window_key, int(window["end_ms"]), row["request_seq"]),
            )
            append_ledger(
                connection,
                clock_ms=int(window["end_ms"]),
                event_type="REQUEST_EXPIRED",
                replica_id=window["replica_id"],
                replica_version=int(window["replica_version"]),
                window_key=window_key,
                request_seq=int(row["request_seq"]),
                from_state="pending",
                to_state="expired",
                failure_count_before=int(row["failure_count"]),
                failure_count_after=int(row["failure_count"]),
                detail={
                    "reason": "deadline_before_window_close",
                    "deadline_ms": int(row["deadline_ms"]),
                    "window_end_ms": int(window["end_ms"]),
                },
            )
        updated = connection.execute(
            """
            UPDATE windows
            SET state = 'committed', committed_clock_ms = ?
            WHERE window_key = ? AND state = 'applying'
            """,
            (int(window["end_ms"]), window_key),
        ).rowcount
        if updated == 1:
            append_ledger(
                connection,
                clock_ms=int(window["end_ms"]),
                event_type="WINDOW_COMMITTED",
                replica_id=window["replica_id"],
                replica_version=int(window["replica_version"]),
                window_key=window_key,
                detail={"window_index": int(window["window_index"])},
            )
    fault.hit("after_window_commit")


def _recover_active_windows(
    connection: sqlite3.Connection,
    fault: FaultInjector,
) -> int:
    rows = connection.execute(
        """
        SELECT window_key
        FROM windows
        WHERE state IN ('planned', 'applying')
        ORDER BY end_ms, replica_id, window_index
        """
    ).fetchall()
    for row in rows:
        _apply_window(connection, str(row["window_key"]), fault)
    return len(rows)


def _finalize_executions(
    connection: sqlite3.Connection,
    clock_ms: int,
) -> int:
    rows = connection.execute(
        """
        SELECT *
        FROM batches
        WHERE state = 'committed' AND executed = 0
          AND estimated_completion_ms <= ?
        ORDER BY estimated_completion_ms, batch_key
        """,
        (clock_ms,),
    ).fetchall()
    count = 0
    for batch in rows:
        batch_key = str(batch["batch_key"])
        with transaction(connection):
            current = connection.execute(
                "SELECT * FROM batches WHERE batch_key = ?", (batch_key,)
            ).fetchone()
            if current is None or int(current["executed"]) != 0:
                continue
            requests = connection.execute(
                """
                SELECT r.*
                FROM batch_requests br
                JOIN requests r ON r.request_seq = br.request_seq
                WHERE br.batch_key = ?
                ORDER BY br.ordinal
                """,
                (batch_key,),
            ).fetchall()
            completion_ms = int(current["estimated_completion_ms"])
            for request in requests:
                if request["state"] != "batched":
                    continue
                if int(request["deadline_ms"]) < completion_ms:
                    connection.execute(
                        """
                        UPDATE requests
                        SET state = 'expired', completion_ms = ?, updated_clock_ms = ?
                        WHERE request_seq = ?
                        """,
                        (completion_ms, completion_ms, request["request_seq"]),
                    )
                    append_ledger(
                        connection,
                        clock_ms=completion_ms,
                        event_type="REQUEST_EXPIRED",
                        replica_id=current["replica_id"],
                        replica_version=int(current["replica_version"]),
                        window_key=current["window_key"],
                        batch_key=batch_key,
                        request_seq=int(request["request_seq"]),
                        from_state="batched",
                        to_state="expired",
                        failure_count_before=int(request["failure_count"]),
                        failure_count_after=int(request["failure_count"]),
                        detail={
                            "reason": "deadline_before_execution_completion",
                            "deadline_ms": int(request["deadline_ms"]),
                            "completion_ms": completion_ms,
                        },
                    )
                    continue
                configured_failures = int(request["remaining_failures"])
                attempts = configured_failures if configured_failures > 0 else 1
                attempt_count_after = int(request["attempt_count"]) + attempts
                connection.execute(
                    """
                    UPDATE requests
                    SET state = 'executing', attempt_count = ?,
                        updated_clock_ms = ?
                    WHERE request_seq = ?
                    """,
                    (attempt_count_after, completion_ms, request["request_seq"]),
                )
                append_ledger(
                    connection,
                    clock_ms=completion_ms,
                    event_type="REQUEST_EXECUTING",
                    replica_id=current["replica_id"],
                    replica_version=int(current["replica_version"]),
                    window_key=current["window_key"],
                    batch_key=batch_key,
                    request_seq=int(request["request_seq"]),
                    from_state="batched",
                    to_state="executing",
                    failure_count_before=int(request["failure_count"]),
                    failure_count_after=int(request["failure_count"]),
                    detail={
                        "completion_ms": completion_ms,
                        "attempt_count_after": attempt_count_after,
                    },
                )
                if configured_failures > 0:
                    failure_count_cursor = int(request["failure_count"])
                    for attempt_index in range(1, configured_failures + 1):
                        if attempt_index > 1:
                            append_ledger(
                                connection,
                                clock_ms=completion_ms,
                                event_type="REQUEST_RETRY_EXECUTING",
                                replica_id=current["replica_id"],
                                replica_version=int(current["replica_version"]),
                                window_key=current["window_key"],
                                batch_key=batch_key,
                                request_seq=int(request["request_seq"]),
                                from_state="failed",
                                to_state="executing",
                                failure_count_before=failure_count_cursor,
                                failure_count_after=failure_count_cursor,
                                detail={
                                    "attempt_index": attempt_index,
                                    "completion_ms": completion_ms,
                                },
                            )
                        failure_count_before = failure_count_cursor
                        failure_count_cursor += 1
                        remaining_after = configured_failures - attempt_index
                        append_ledger(
                            connection,
                            clock_ms=completion_ms,
                            event_type="REQUEST_EXECUTION_FAILED",
                            replica_id=current["replica_id"],
                            replica_version=int(current["replica_version"]),
                            window_key=current["window_key"],
                            batch_key=batch_key,
                            request_seq=int(request["request_seq"]),
                            from_state="executing",
                            to_state="failed",
                            failure_count_before=failure_count_before,
                            failure_count_after=failure_count_cursor,
                            detail={
                                "reason": "simulated_execution_failure",
                                "attempt_index": attempt_index,
                                "remaining_failures": remaining_after,
                            },
                        )
                    connection.execute(
                        """
                        UPDATE requests
                        SET state = 'failed', failure_count = ?,
                            remaining_failures = 0, completion_ms = ?,
                            updated_clock_ms = ?
                        WHERE request_seq = ?
                        """,
                        (
                            failure_count_cursor,
                            completion_ms,
                            completion_ms,
                            request["request_seq"],
                        ),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE requests
                        SET state = 'completed', completion_ms = ?,
                            updated_clock_ms = ?
                        WHERE request_seq = ?
                        """,
                        (completion_ms, completion_ms, request["request_seq"]),
                    )
                    append_ledger(
                        connection,
                        clock_ms=completion_ms,
                        event_type="REQUEST_COMPLETED",
                        replica_id=current["replica_id"],
                        replica_version=int(current["replica_version"]),
                        window_key=current["window_key"],
                        batch_key=batch_key,
                        request_seq=int(request["request_seq"]),
                        from_state="executing",
                        to_state="completed",
                        failure_count_before=int(request["failure_count"]),
                        failure_count_after=int(request["failure_count"]),
                        detail={"completion_ms": completion_ms},
                    )
            updated = connection.execute(
                "UPDATE batches SET executed = 1 "
                "WHERE batch_key = ? AND executed = 0",
                (batch_key,),
            ).rowcount
            if updated == 1:
                connection.execute(
                    """
                    UPDATE replica_usage
                    SET compute_fixed = MAX(0, compute_fixed - ?),
                        memory_fixed = MAX(0, memory_fixed - ?),
                        updated_clock_ms = ?
                    WHERE replica_id = ?
                    """,
                    (
                        int(current["compute_fixed"]),
                        int(current["memory_fixed"]),
                        completion_ms,
                        current["replica_id"],
                    ),
                )
                append_ledger(
                    connection,
                    clock_ms=completion_ms,
                    event_type="BATCH_EXECUTED",
                    replica_id=current["replica_id"],
                    replica_version=int(current["replica_version"]),
                    window_key=current["window_key"],
                    batch_key=batch_key,
                    detail={"released": True},
                )
        count += 1
    return count


def _set_schedule_target(
    connection: sqlite3.Connection,
    current_clock: int,
    target_ms: int,
) -> int:
    pending_value = get_meta(connection, "schedule_target_ms")
    pending_target = int(pending_value) if pending_value is not None else None
    if target_ms < current_clock:
        raise BusinessError(
            "E_CLOCK_REGRESSION",
            "logical clock regression is forbidden",
            {"current_clock_ms": current_clock, "requested_clock_ms": target_ms},
        )
    if pending_target is not None and target_ms < pending_target:
        raise BusinessError(
            "E_SCHEDULE_TARGET_REGRESSION",
            "requested target is earlier than the persisted in-flight target",
            {
                "inflight_target_ms": pending_target,
                "requested_clock_ms": target_ms,
            },
        )
    effective = target_ms if pending_target is None else max(target_ms, pending_target)
    if effective != pending_target and not (
        pending_target is None and effective == current_clock
    ):
        set_meta(connection, "schedule_target_ms", effective)
    return effective


def _run_to_target(
    connection: sqlite3.Connection,
    target_ms: int,
    fault: FaultInjector,
) -> dict[str, int]:
    _recover_active_windows(connection, fault)
    model = _load_model(connection)
    precision = _load_precision(connection)
    snapshot = _load_current_snapshot(connection)
    with transaction(connection):
        _ensure_watermarks(connection, snapshot, _clock(connection))
    planned_windows = 0
    tick_clock = _clock(connection)
    while tick_clock < target_ms:
        tick_clock = min(
            target_ms, tick_clock + precision.clock_step_ms
        )
        while True:
            event = _next_window_event(connection, snapshot, tick_clock)
            if event is None:
                break
            with transaction(connection):
                window_key = _plan_window(
                    connection, model, precision, event, fault
                )
            planned_windows += 1
            fault.hit("after_window_plan")
            _apply_window(connection, window_key, fault)
        with transaction(connection):
            set_meta(connection, "logical_clock_ms", tick_clock)
            append_ledger(
                connection,
                clock_ms=tick_clock,
                event_type="CLOCK_TICK",
                detail={"logical_clock_ms": tick_clock},
            )
    executed_batches = _finalize_executions(connection, target_ms)
    with transaction(connection):
        set_meta(connection, "logical_clock_ms", target_ms)
        set_meta(connection, "schedule_target_ms", None)
        append_ledger(
            connection,
            clock_ms=target_ms,
            event_type="CLOCK_ADVANCED",
            detail={"logical_clock_ms": target_ms},
        )
    return {
        "target_clock_ms": target_ms,
        "windows_planned": planned_windows,
        "batches_executed": executed_batches,
    }


def schedule(
    state_dir: str,
    target_ms: int,
    fault: FaultInjector | None = None,
) -> dict[str, Any]:
    if isinstance(target_ms, bool) or not isinstance(target_ms, int) or target_ms < 0:
        raise BusinessError(
            "E_REQUEST_CLOCK_INVALID",
            "schedule clock must be a non-negative integer",
        )
    injector = fault or FaultInjector()
    with file_lock(state_dir, exclusive=True):
        connection = open_database(state_dir, create=False)
        try:
            current_clock = _clock(connection)
            if target_ms < current_clock:
                raise BusinessError(
                    "E_CLOCK_REGRESSION",
                    "logical clock regression is forbidden",
                    {
                        "current_clock_ms": current_clock,
                        "requested_clock_ms": target_ms,
                    },
                )
            _recover_active_windows(connection, injector)
            pending_value = get_meta(connection, "schedule_target_ms")
            with transaction(connection):
                effective = _set_schedule_target(
                    connection, current_clock, target_ms
                )
            if pending_value is None and effective == current_clock:
                executed = _finalize_executions(connection, effective)
                return {
                    "action": "no_op",
                    "clock_ms": current_clock,
                    "windows_planned": 0,
                    "batches_executed": executed,
                }
            result = _run_to_target(connection, effective, injector)
            return {"action": "scheduled", **result}
        finally:
            connection.close()


def resume(
    state_dir: str,
    fault: FaultInjector | None = None,
) -> dict[str, Any]:
    injector = fault or FaultInjector()
    with file_lock(state_dir, exclusive=True):
        connection = open_database(state_dir, create=False)
        try:
            recovered = _recover_active_windows(connection, injector)
            pending_value = get_meta(connection, "schedule_target_ms")
            if pending_value is not None:
                target = int(pending_value)
                result = _run_to_target(connection, target, injector)
                return {"action": "resumed", "recovered_windows": recovered, **result}
            clock_ms = _clock(connection)
            executed = _finalize_executions(connection, clock_ms)
            return {
                "action": "resumed",
                "recovered_windows": recovered,
                "target_clock_ms": clock_ms,
                "windows_planned": 0,
                "batches_executed": executed,
            }
        finally:
            connection.close()


def _status_replica_rows(
    connection: sqlite3.Connection,
) -> dict[str, dict[str, Any]]:
    snapshot = _load_current_snapshot(connection)
    result: dict[str, dict[str, Any]] = {}
    for replica in snapshot.replicas:
        watermark = connection.execute(
            "SELECT * FROM replica_watermarks WHERE replica_id = ?",
            (replica.replica_id,),
        ).fetchone()
        usage = connection.execute(
            "SELECT * FROM replica_usage WHERE replica_id = ?",
            (replica.replica_id,),
        ).fetchone()
        result[replica.replica_id] = {
            "replica_id": replica.replica_id,
            "current_version": snapshot.version,
            "specification": replica.to_json(),
            "window_watermark": {
                "next_window_index": int(watermark["next_window_index"]),
                "next_window_start_ms": int(watermark["next_window_start_ms"]),
                "next_window_end_ms": int(watermark["next_window_end_ms"]),
            },
            "usage": {
                "compute_fixed": int(usage["compute_fixed"]) if usage else 0,
                "memory_fixed": int(usage["memory_fixed"]) if usage else 0,
            },
            "windows": [],
        }
    historical = connection.execute(
        """
        SELECT DISTINCT w.replica_id
        FROM windows w
        LEFT JOIN replica_watermarks rw ON rw.replica_id = w.replica_id
        WHERE rw.replica_id IS NULL
        ORDER BY w.replica_id
        """
    ).fetchall()
    for row in historical:
        usage = connection.execute(
            "SELECT * FROM replica_usage WHERE replica_id = ?",
            (row["replica_id"],),
        ).fetchone()
        result[str(row["replica_id"])] = {
            "replica_id": str(row["replica_id"]),
            "current_version": None,
            "specification": None,
            "window_watermark": None,
            "usage": {
                "compute_fixed": int(usage["compute_fixed"]) if usage else 0,
                "memory_fixed": int(usage["memory_fixed"]) if usage else 0,
            },
            "windows": [],
        }
    return result


def status(state_dir: str) -> dict[str, Any]:
    with file_lock(state_dir, exclusive=False):
        connection = open_database(state_dir, create=False)
        try:
            connection.execute("BEGIN")
            try:
                clock_ms = _clock(connection)
                precision = _load_precision(connection)
                pending_value = get_meta(connection, "schedule_target_ms")
                current = _load_current_snapshot(connection)
                replica_groups = _status_replica_rows(connection)
                windows = connection.execute(
                    """
                    SELECT *
                    FROM windows
                    ORDER BY replica_id, window_index, replica_version
                    """
                ).fetchall()
                for window in windows:
                    replica_id = str(window["replica_id"])
                    group = replica_groups.setdefault(
                        replica_id,
                        {
                            "replica_id": replica_id,
                            "current_version": None,
                            "specification": None,
                            "window_watermark": None,
                            "usage": None,
                            "windows": [],
                        },
                    )
                    request_rows = connection.execute(
                        """
                        SELECT request_seq, batch_index, disposition
                        FROM window_requests
                        WHERE window_key = ?
                        ORDER BY disposition, batch_index, ordinal
                        """,
                        (window["window_key"],),
                    ).fetchall()
                    batches = []
                    batch_rows = connection.execute(
                        "SELECT * FROM batches WHERE window_key = ? ORDER BY batch_index",
                        (window["window_key"],),
                    ).fetchall()
                    for batch in batch_rows:
                        batch_requests = [
                            int(row["request_seq"])
                            for row in connection.execute(
                                "SELECT request_seq FROM batch_requests "
                                "WHERE batch_key = ? ORDER BY ordinal",
                                (batch["batch_key"],),
                            ).fetchall()
                        ]
                        decision = json.loads(batch["decision_json"])
                        sensitive_rows = connection.execute(
                            """
                            SELECT decision, metric, tolerance, left_json, right_json,
                                   absolute_gap, source_chain_json
                            FROM sensitive_decisions
                            WHERE batch_key = ?
                            ORDER BY decision, metric, decision_id
                            """,
                            (batch["batch_key"],),
                        ).fetchall()
                        sensitive = [
                            {
                                "decision": row["decision"],
                                "metric": row["metric"],
                                "tolerance": float(row["tolerance"]),
                                "left": json.loads(row["left_json"]),
                                "right": json.loads(row["right_json"]),
                                "absolute_gap": float(row["absolute_gap"]),
                                "source_chain": json.loads(row["source_chain_json"]),
                            }
                            for row in sensitive_rows
                        ]
                        precision_records = []
                        for row in connection.execute(
                            """
                            SELECT * FROM precision_records
                            WHERE batch_key = ?
                            ORDER BY metric
                            """,
                            (batch["batch_key"],),
                        ).fetchall():
                            precision_records.append(
                                {
                                    "metric": row["metric"],
                                    "scale": int(row["scale"]),
                                    "raw_value": float(row["raw_value"]),
                                    "fixed_value": int(row["fixed_value"]),
                                    "rounding_direction": row["rounding_direction"],
                                    "quantization_error_bound": float(
                                        row["quantization_error_bound"]
                                    ),
                                    "arithmetic_error_bound": float(
                                        row["arithmetic_error_bound"]
                                    ),
                                    "total_error_bound": float(
                                        row["total_error_bound"]
                                    ),
                                    "error_budget": float(row["error_budget"]),
                                    "within_error_budget": bool(
                                        row["within_error_budget"]
                                    ),
                                    "source_chain": json.loads(
                                        row["source_chain_json"]
                                    ),
                                    "operation_order": json.loads(
                                        row["operation_order_json"]
                                    ),
                                    "aggregate_order": json.loads(
                                        row["aggregate_order_json"]
                                    ),
                                }
                            )
                        batches.append(
                            {
                                "batch_key": batch["batch_key"],
                                "batch_index": int(batch["batch_index"]),
                                "replica_version": int(batch["replica_version"]),
                                "state": batch["state"],
                                "executed": bool(batch["executed"]),
                                "request_seqs": batch_requests,
                                "skipped": decision.get("skipped", []),
                                "aggregate_order": decision.get("aggregate_order", []),
                                "precision_sensitive": bool(
                                    batch["precision_sensitive"]
                                ),
                                "precision_sensitive_decisions": sensitive,
                                "estimates": precision_records,
                                "estimated_completion_ms": int(
                                    batch["estimated_completion_ms"]
                                ),
                            }
                        )
                    group["windows"].append(
                        {
                            "window_key": window["window_key"],
                            "replica_version": int(window["replica_version"]),
                            "window_index": int(window["window_index"]),
                            "start_ms": int(window["start_ms"]),
                            "end_ms": int(window["end_ms"]),
                            "state": window["state"],
                            "request_seqs": sorted(
                                int(row["request_seq"])
                                for row in request_rows
                                if row["disposition"] == "batch"
                            ),
                            "expired_request_seqs": sorted(
                                int(row["request_seq"])
                                for row in request_rows
                                if row["disposition"] == "expired"
                            ),
                            "batches": batches,
                        }
                    )
                request_rows = connection.execute(
                    """
                    SELECT request_seq, replica_id, state, failure_count,
                           attempt_count, window_key, batch_key, completion_ms
                    FROM requests
                    ORDER BY request_seq
                    """
                ).fetchall()
                requests = []
                for row in request_rows:
                    lifecycle_events = [
                        {
                            "clock_ms": int(event["clock_ms"]),
                            "event_type": event["event_type"],
                            "from_state": event["from_state"],
                            "to_state": event["to_state"],
                            "failure_count_before": event["failure_count_before"],
                            "failure_count_after": event["failure_count_after"],
                            "detail": json.loads(event["detail_json"]),
                        }
                        for event in connection.execute(
                            """
                            SELECT clock_ms, event_type, from_state, to_state,
                                   failure_count_before, failure_count_after,
                                   detail_json
                            FROM ledger
                            WHERE request_seq = ? AND to_state IS NOT NULL
                            ORDER BY event_id
                            """,
                            (row["request_seq"],),
                        ).fetchall()
                    ]
                    requests.append(
                        {
                            "request_seq": int(row["request_seq"]),
                            "replica_id": row["replica_id"],
                            "state": row["state"],
                            "failure_count": int(row["failure_count"]),
                            "attempt_count": int(row["attempt_count"]),
                            "window_key": row["window_key"],
                            "batch_key": row["batch_key"],
                            "completion_ms": row["completion_ms"],
                            "lifecycle_events": lifecycle_events,
                        }
                    )
                counters: dict[str, int] = {}
                for row in requests:
                    counters[row["state"]] = counters.get(row["state"], 0) + 1
                versions = [
                    {
                        "version": int(row["version"]),
                        "content_digest": row["content_digest"],
                        "action": row["action"],
                        "created_clock_ms": int(row["created_clock_ms"]),
                        "replicas": json.loads(row["payload_json"])["replicas"],
                    }
                    for row in connection.execute(
                        "SELECT * FROM replica_versions ORDER BY version"
                    ).fetchall()
                ]
                return {
                    "clock": {
                        "logical_clock_ms": clock_ms,
                        "clock_step_ms": precision.clock_step_ms,
                        "logical_clock_ticks": clock_ms // precision.clock_step_ms,
                        "schedule_target_ms": int(pending_value)
                        if pending_value is not None
                        else None,
                    },
                    "current_replica_version": current.version,
                    "replica_versions": versions,
                    "replicas": [
                        replica_groups[replica_id]
                        for replica_id in sorted(replica_groups)
                    ],
                    "requests": requests,
                    "request_state_counters": dict(sorted(counters.items())),
                }
            finally:
                connection.execute("COMMIT")
        finally:
            connection.close()


def _verification_checks(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: dict[str, Any] | None = None) -> None:
        checks.append({"check": name, "ok": ok, "detail": detail or {}})

    incomplete = connection.execute(
        "SELECT COUNT(*) AS n FROM windows WHERE state != 'committed'"
    ).fetchone()["n"]
    check("all_windows_committed", int(incomplete) == 0, {"count": int(incomplete)})

    duplicate_assignments = connection.execute(
        """
        SELECT COUNT(*) AS n FROM (
            SELECT request_seq FROM window_requests
            GROUP BY request_seq HAVING COUNT(*) > 1
        )
        """
    ).fetchone()["n"]
    check(
        "requests_unique_window_assignment",
        int(duplicate_assignments) == 0,
        {"duplicates": int(duplicate_assignments)},
    )

    duplicate_batch_membership = connection.execute(
        """
        SELECT COUNT(*) AS n FROM (
            SELECT request_seq FROM batch_requests
            GROUP BY request_seq HAVING COUNT(*) > 1
        )
        """
    ).fetchone()["n"]
    check(
        "requests_unique_batch_membership",
        int(duplicate_batch_membership) == 0,
        {"duplicates": int(duplicate_batch_membership)},
    )

    missing_precision = connection.execute(
        """
        SELECT COUNT(*) AS n
        FROM batches b
        WHERE (SELECT COUNT(*) FROM precision_records p WHERE p.batch_key = b.batch_key) != 3
        """
    ).fetchone()["n"]
    check(
        "three_precision_records_per_batch",
        int(missing_precision) == 0,
        {"violations": int(missing_precision)},
    )

    fixed_mismatch = connection.execute(
        """
        SELECT COUNT(*) AS n
        FROM batches b
        WHERE b.compute_fixed != COALESCE((
                  SELECT fixed_value FROM precision_records p
                  WHERE p.batch_key = b.batch_key AND p.metric = 'compute'
              ), -1)
           OR b.memory_fixed != COALESCE((
                  SELECT fixed_value FROM precision_records p
                  WHERE p.batch_key = b.batch_key AND p.metric = 'memory'
              ), -1)
           OR b.queue_fixed != COALESCE((
                  SELECT fixed_value FROM precision_records p
                  WHERE p.batch_key = b.batch_key AND p.metric = 'queue'
              ), -1)
        """
    ).fetchone()["n"]
    check(
        "batch_fixed_values_match_precision_records",
        int(fixed_mismatch) == 0,
        {"violations": int(fixed_mismatch)},
    )

    sensitivity_mismatch = connection.execute(
        """
        SELECT COUNT(*) AS n
        FROM batches b
        WHERE b.precision_sensitive != CASE WHEN EXISTS(
            SELECT 1 FROM sensitive_decisions s WHERE s.batch_key = b.batch_key
        ) THEN 1 ELSE 0 END
        """
    ).fetchone()["n"]
    check(
        "precision_sensitive_flags_match_source_chains",
        int(sensitivity_mismatch) == 0,
        {"violations": int(sensitivity_mismatch)},
    )

    window_batch_mismatch = connection.execute(
        """
        SELECT COUNT(*) AS n
        FROM window_requests wr
        JOIN batches b ON b.window_key = wr.window_key
             AND b.batch_index = wr.batch_index
        LEFT JOIN batch_requests br
             ON br.batch_key = b.batch_key AND br.request_seq = wr.request_seq
        WHERE wr.disposition = 'batch' AND br.request_seq IS NULL
        """
    ).fetchone()["n"]
    check(
        "batch_membership_matches_window_plan",
        int(window_batch_mismatch) == 0,
        {"violations": int(window_batch_mismatch)},
    )

    invalid_usage = connection.execute(
        """
        SELECT COUNT(*) AS n
        FROM replica_usage u
        WHERE u.compute_fixed < 0 OR u.memory_fixed < 0
        """
    ).fetchone()["n"]
    check("resource_usage_non_negative", int(invalid_usage) == 0)

    usage_mismatch = 0
    for row in connection.execute("SELECT * FROM replica_usage").fetchall():
        expected = connection.execute(
            """
            SELECT COALESCE(SUM(compute_fixed), 0) AS compute_fixed,
                   COALESCE(SUM(memory_fixed), 0) AS memory_fixed
            FROM batches
            WHERE replica_id = ? AND state = 'committed' AND executed = 0
            """,
            (row["replica_id"],),
        ).fetchone()
        if (
            int(row["compute_fixed"]) != int(expected["compute_fixed"])
            or int(row["memory_fixed"]) != int(expected["memory_fixed"])
        ):
            usage_mismatch += 1
    check(
        "resource_usage_matches_unexecuted_batches",
        usage_mismatch == 0,
        {"violations": usage_mismatch},
    )

    bad_versions = connection.execute(
        """
        SELECT COUNT(*) AS n
        FROM windows w
        LEFT JOIN replica_versions rv ON rv.version = w.replica_version
        WHERE rv.version IS NULL
        """
    ).fetchone()["n"]
    check(
        "windows_bind_existing_replica_version",
        int(bad_versions) == 0,
        {"violations": int(bad_versions)},
    )

    overlap_violations = 0
    last_end_by_replica: dict[str, int] = {}
    for row in connection.execute(
        "SELECT replica_id, start_ms, end_ms FROM windows "
        "ORDER BY replica_id, start_ms, end_ms, replica_version"
    ).fetchall():
        replica_id = str(row["replica_id"])
        if int(row["start_ms"]) < last_end_by_replica.get(replica_id, -1):
            overlap_violations += 1
        last_end_by_replica[replica_id] = max(
            last_end_by_replica.get(replica_id, -1), int(row["end_ms"])
        )
    check(
        "replica_windows_do_not_overlap",
        overlap_violations == 0,
        {"violations": overlap_violations},
    )

    current_pointer = connection.execute(
        """
        SELECT COUNT(*) AS n
        FROM replica_current rc
        JOIN replica_versions rv ON rv.version = rc.version
        WHERE rc.singleton = 1
        """
    ).fetchone()["n"]
    check(
        "current_replica_pointer_is_valid",
        int(current_pointer) == 1,
        {"matches": int(current_pointer)},
    )

    version_clock_regression = 0
    previous_created = -1
    for row in connection.execute(
        "SELECT version, created_clock_ms FROM replica_versions ORDER BY version"
    ).fetchall():
        created = int(row["created_clock_ms"])
        if created < previous_created:
            version_clock_regression += 1
        previous_created = created
    check(
        "replica_version_created_clocks_monotonic",
        version_clock_regression == 0,
        {"violations": version_clock_regression},
    )

    bad_states = connection.execute(
        """
        SELECT COUNT(*) AS n FROM requests
        WHERE state NOT IN ('pending', 'batched', 'executing', 'completed', 'expired', 'failed')
        """
    ).fetchone()["n"]
    check(
        "request_states_valid",
        int(bad_states) == 0,
        {"violations": int(bad_states)},
    )

    count_mismatches = connection.execute(
        """
        SELECT COUNT(*) AS n
        FROM requests r
        JOIN batch_requests br ON br.request_seq = r.request_seq
        JOIN batches b ON b.batch_key = br.batch_key
        WHERE b.state != 'committed' AND r.state != 'pending'
        """
    ).fetchone()["n"]
    check(
        "no_request_advanced_from_uncommitted_batch",
        int(count_mismatches) == 0,
        {"violations": int(count_mismatches)},
    )

    broken_request_links = connection.execute(
        """
        SELECT COUNT(*) AS n
        FROM requests
        WHERE (state IN ('batched', 'executing', 'completed', 'failed')
               AND (window_key IS NULL OR batch_key IS NULL))
           OR (state = 'expired' AND window_key IS NULL)
           OR (state = 'pending' AND window_key IS NOT NULL)
        """
    ).fetchone()["n"]
    check(
        "request_lifecycle_links_are_consistent",
        int(broken_request_links) == 0,
        {"violations": int(broken_request_links)},
    )

    ledger_state_mismatch = connection.execute(
        """
        SELECT COUNT(*) AS n
        FROM requests r
        WHERE EXISTS (SELECT 1 FROM ledger l WHERE l.request_seq = r.request_seq)
          AND (
              SELECT l.to_state
              FROM ledger l
              WHERE l.request_seq = r.request_seq AND l.to_state IS NOT NULL
              ORDER BY l.event_id DESC
              LIMIT 1
          ) != r.state
        """
    ).fetchone()["n"]
    check(
        "ledger_latest_state_matches_request",
        int(ledger_state_mismatch) == 0,
        {"violations": int(ledger_state_mismatch)},
    )

    ledger_failure_mismatch = connection.execute(
        """
        SELECT COUNT(*) AS n
        FROM requests r
        WHERE (
            SELECT l.failure_count_after
            FROM ledger l
            WHERE l.request_seq = r.request_seq
              AND l.failure_count_after IS NOT NULL
            ORDER BY l.event_id DESC
            LIMIT 1
        ) IS NOT NULL
          AND (
              SELECT l.failure_count_after
              FROM ledger l
              WHERE l.request_seq = r.request_seq
                AND l.failure_count_after IS NOT NULL
              ORDER BY l.event_id DESC
              LIMIT 1
          ) != r.failure_count
        """
    ).fetchone()["n"]
    check(
        "ledger_latest_failure_count_matches_request",
        int(ledger_failure_mismatch) == 0,
        {"violations": int(ledger_failure_mismatch)},
    )

    budget_exceeded = connection.execute(
        "SELECT COUNT(*) AS n FROM precision_records WHERE within_error_budget = 0"
    ).fetchone()["n"]
    checks.append(
        {
            "check": "precision_error_budget",
            "ok": True,
            "detail": {"records_over_budget": int(budget_exceeded)},
            "severity": "warning" if budget_exceeded else "info",
        }
    )
    return checks


def verify(state_dir: str) -> dict[str, Any]:
    with file_lock(state_dir, exclusive=False):
        connection = open_database(state_dir, create=False)
        try:
            connection.execute("BEGIN")
            try:
                checks = _verification_checks(connection)
                ok = all(check["ok"] for check in checks)
                return {
                    "ok": ok,
                    "checks": checks,
                    "summary": {
                        "checks": len(checks),
                        "failed": sum(1 for check in checks if not check["ok"]),
                        "warnings": sum(
                            1
                            for check in checks
                            if check.get("severity") == "warning"
                        ),
                    },
                }
            finally:
                connection.execute("COMMIT")
        finally:
            connection.close()

