from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

from .errors import BusinessError, StateMismatchError
from .estimator import compact_cumulative, estimate_batch
from .jsonutil import canonical_json, decimal_string
from .locking import FileLock
from .spec import METRICS, ModelSpec, PrecisionConfig, ReplicaSpec, RequestSpec, WorkspaceSpec
from .store import (
    connect,
    lock_path,
    meta_get,
    set_request_status,
    state_get,
    state_set,
    transaction,
    workspace_from_db,
)


FINAL_REQUEST_STATUSES = {"completed", "expired", "failed"}
OUTCOME_COMPLETED = "OK_COMPLETED"
OUTCOME_QUEUED = "OK_QUEUED"
OUTCOME_BATCHED = "OK_BATCHED"
OUTCOME_EXPIRED = "E_REQUEST_EXPIRED"
OUTCOME_FAILED = "E_EXECUTION_FAILED"


def _fault_requested(stage: str, step_index: int) -> bool:
    configured = os.environ.get("BATCHDIAG_FAULT", "")
    if configured != stage:
        return False
    raw_step = os.environ.get("BATCHDIAG_FAULT_STEP", "1")
    try:
        target_step = int(raw_step)
    except ValueError:
        target_step = 1
    return step_index == target_step


def _deterministically_sampled(batch_id: str, ratio: Decimal) -> bool:
    if ratio >= 1:
        return True
    value = int.from_bytes(hashlib.sha256(batch_id.encode("utf-8")).digest()[:8], "big")
    threshold = int(ratio * Decimal(2**64))
    return value < threshold


def _request_specs(
    ids: list[int], requests: dict[int, dict[str, Any]]
) -> list[RequestSpec]:
    result: list[RequestSpec] = []
    for request_id in ids:
        row = requests[request_id]
        if row["status"] != "queued":
            raise BusinessError(
                "E_DUPLICATE_REQUEST_ASSIGNMENT",
                "a queued batch candidate is not in queued state",
                {"request_id": request_id, "status": row["status"]},
            )
        result.append(
            RequestSpec(
                request_id=request_id,
                arrival_ms=row["arrival_ms"],
                service_class=row["service_class"],
                input_tokens=row["input_tokens"],
                output_tokens=row["output_tokens"],
                deadline_ms=row["deadline_ms"],
            )
        )
    return result


def _candidate_for_replica(
    *,
    queue: list[int],
    requests: dict[int, dict[str, Any]],
    model: ModelSpec,
    replica: ReplicaSpec,
    config: PrecisionConfig,
    previous: dict[str, Any],
    queue_before: int,
    batch_id: str,
) -> tuple[list[int], dict[str, dict[str, Any]] | None]:
    limit = min(replica.dispatch_quota, replica.max_batch_size)
    chosen: list[int] = []
    estimates: dict[str, dict[str, Any]] | None = None
    for request_id in queue[:limit]:
        row = requests[request_id]
        if row["status"] != "queued":
            break
        tentative_ids = chosen + [request_id]
        tentative_specs = _request_specs(tentative_ids, requests)
        if any(
            request.sequence_tokens > replica.max_sequence_length
            for request in tentative_specs
        ):
            break
        tentative_estimates = estimate_batch(
            batch_id=batch_id,
            requests=tentative_specs,
            model=model,
            replica=replica,
            config=config,
            queue_depth=queue_before,
            previous=previous,
        )
        compute = tentative_estimates["compute"]
        memory = tentative_estimates["memory"]
        if compute["value"] > replica.compute_units:
            break
        if memory["value"] > replica.memory_limit_mib:
            break
        chosen = tentative_ids
        estimates = tentative_estimates
    return chosen, estimates


def _select_replica_and_batch(
    *,
    queue: list[int],
    requests: dict[int, dict[str, Any]],
    model: ModelSpec,
    replicas: tuple[ReplicaSpec, ...],
    config: PrecisionConfig,
    cumulative: dict[str, Any],
    queue_before: int,
    batch_id: str,
) -> tuple[
    ReplicaSpec | None,
    list[int],
    dict[str, dict[str, Any]] | None,
]:
    best_replica: ReplicaSpec | None = None
    best_ids: list[int] = []
    best_estimates: dict[str, dict[str, Any]] | None = None
    best_key: tuple[Any, ...] | None = None
    for replica in sorted(replicas, key=lambda item: item.replica_id):
        chosen, estimates = _candidate_for_replica(
            queue=queue,
            requests=requests,
            model=model,
            replica=replica,
            config=config,
            previous=cumulative.get(replica.replica_id, {}),
            queue_before=queue_before,
            batch_id=batch_id,
        )
        if not chosen or estimates is None:
            continue
        float_utilization = estimates["compute"]["value"] / replica.compute_units
        fixed_utilization = estimates["compute"]["fixed_value"] / replica.compute_units
        key = (
            -len(chosen),
            float_utilization,
            fixed_utilization,
            replica.replica_id,
        )
        if best_key is None or key < best_key:
            best_key = key
            best_replica = replica
            best_ids = chosen
            best_estimates = estimates
    return best_replica, best_ids, best_estimates


def _operation_refs(
    batch_id: str, metric: str, estimate: dict[str, Any]
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for path in ("fixed", "float64"):
        for operation in estimate[path]["operations"]:
            if operation["operation"] not in {"add", "multiply"}:
                continue
            result.append(
                {
                    "spectrum_ref": f"{batch_id}/{metric}/{path}",
                    "operation_id": operation["operation_id"],
                    "path": path,
                    "operation": operation["operation"],
                    "label": operation["label"],
                    "added_error_bound": operation["added_error_bound"],
                    "cumulative_error_bound": operation["cumulative_error_bound"],
                }
            )
    return result


def _replica_sensitivity(
    *,
    batch_id: str,
    step_index: int,
    clock_ms: int,
    selected_replica: ReplicaSpec,
    selected_ids: list[int],
    request_specs: list[RequestSpec],
    requests: dict[int, dict[str, Any]],
    model: ModelSpec,
    replicas: tuple[ReplicaSpec, ...],
    config: PrecisionConfig,
    cumulative: dict[str, Any],
    queue_before: int,
    selected_estimates: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    tolerance = config.float_tolerance
    if len(replicas) < 2:
        return []
    candidates: list[dict[str, Any]] = []
    for replica in sorted(replicas, key=lambda item: item.replica_id):
        estimates = (
            selected_estimates
            if replica.replica_id == selected_replica.replica_id
            else estimate_batch(
                batch_id=batch_id,
                requests=request_specs,
                model=model,
                replica=replica,
                config=config,
                queue_depth=queue_before,
                previous=cumulative.get(replica.replica_id, {}),
            )
        )
        compute = estimates["compute"]
        memory = estimates["memory"]
        if compute["value"] > replica.compute_units:
            continue
        if memory["value"] > replica.memory_limit_mib:
            continue
        candidates.append(
            {
                "replica_id": replica.replica_id,
                "replica": replica,
                "estimates": estimates,
                "float_utilization": compute["value"] / replica.compute_units,
                "fixed_utilization": compute["fixed_value"] / replica.compute_units,
                "float_bound": Decimal(compute["float64"]["cumulative_error_bound"])
                / Decimal(str(replica.compute_units)),
                "fixed_bound": Decimal(compute["fixed"]["cumulative_error_bound"])
                / Decimal(str(replica.compute_units)),
            }
        )
    if len(candidates) < 2:
        return []
    selected = next(
        candidate
        for candidate in candidates
        if candidate["replica_id"] == selected_replica.replica_id
    )
    float_choice = min(
        candidates, key=lambda item: (item["float_utilization"], item["replica_id"])
    )
    fixed_choice = min(
        candidates, key=lambda item: (item["fixed_utilization"], item["replica_id"])
    )
    result: list[dict[str, Any]] = []
    for alternative in candidates:
        if alternative["replica_id"] == selected_replica.replica_id:
            continue
        difference = abs(
            selected["float_utilization"] - alternative["float_utilization"]
        )
        if Decimal(str(difference)) > tolerance:
            continue
        error_bound = max(selected["fixed_bound"], alternative["fixed_bound"])
        result.append(
            {
                "sensitivity_id": (
                    f"{batch_id}:replica:{selected_replica.replica_id}:"
                    f"{alternative['replica_id']}"
                ),
                "kind": "replica_choice",
                "batch_id": batch_id,
                "step_index": step_index,
                "logical_time_ms": clock_ms,
                "metric": "compute_utilization",
                "decision": "minimum estimated compute utilization",
                "request_ids": sorted(selected_ids),
                "selected": {
                    "replica_id": selected_replica.replica_id,
                    "float64_utilization": repr(selected["float_utilization"]),
                    "fixed_utilization": repr(selected["fixed_utilization"]),
                    "float64_choice": float_choice["replica_id"],
                    "fixed_choice": fixed_choice["replica_id"],
                },
                "alternative": {
                    "replica_id": alternative["replica_id"],
                    "float64_utilization": repr(alternative["float_utilization"]),
                    "fixed_utilization": repr(alternative["fixed_utilization"]),
                },
                "difference": repr(float(difference)),
                "tolerance": decimal_string(tolerance),
                "error_bound": decimal_string(error_bound),
                "flip_if_fixed": fixed_choice["replica_id"] != float_choice["replica_id"],
                "sampled": True,
                "conversion_steps": _operation_refs(
                    batch_id, "compute", selected["estimates"]["compute"]
                )
                + _operation_refs(
                    batch_id, "compute", alternative["estimates"]["compute"]
                ),
                "spectrum_refs": [
                    f"{batch_id}/compute/fixed",
                    f"{batch_id}/compute/float64",
                ],
            }
        )
    return result


def _boundary_sensitivity(
    *,
    batch_id: str,
    step_index: int,
    clock_ms: int,
    selected_ids: list[int],
    queue: list[int],
    replica: ReplicaSpec,
    requests: dict[int, dict[str, Any]],
    model: ModelSpec,
    config: PrecisionConfig,
    previous: dict[str, Any],
    queue_before: int,
    selected_estimates: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    limit = min(replica.dispatch_quota, replica.max_batch_size)
    if not selected_ids or len(queue) <= len(selected_ids):
        return []
    if len(selected_ids) < limit:
        return []
    next_id = queue[len(selected_ids)]
    replaced_id = selected_ids[-1]
    candidate_a_ids = list(selected_ids)
    candidate_b_ids = selected_ids[:-1] + [next_id]
    try:
        candidate_a_specs = _request_specs(candidate_a_ids, requests)
        candidate_b_specs = _request_specs(candidate_b_ids, requests)
    except BusinessError:
        return []
    if any(
        request.sequence_tokens > replica.max_sequence_length
        for request in candidate_b_specs
    ):
        return []
    candidate_b = estimate_batch(
        batch_id=batch_id,
        requests=candidate_b_specs,
        model=model,
        replica=replica,
        config=config,
        queue_depth=queue_before,
        previous=previous,
    )
    if candidate_b["compute"]["value"] > replica.compute_units:
        return []
    if candidate_b["memory"]["value"] > replica.memory_limit_mib:
        return []
    candidate_a = selected_estimates
    result: list[dict[str, Any]] = []
    tolerance = config.float_tolerance
    for metric in ("compute", "memory"):
        a = candidate_a[metric]
        b = candidate_b[metric]
        difference = abs(a["value"] - b["value"])
        if Decimal(str(difference)) > tolerance:
            continue
        float_choice = candidate_a_ids if a["value"] <= b["value"] else candidate_b_ids
        fixed_choice = (
            candidate_a_ids if a["fixed_value"] <= b["fixed_value"] else candidate_b_ids
        )
        result.append(
            {
                "sensitivity_id": f"{batch_id}:boundary:{metric}",
                "kind": "batch_boundary",
                "batch_id": batch_id,
                "step_index": step_index,
                "logical_time_ms": clock_ms,
                "metric": metric,
                "decision": "quota-limited admission of the final candidate",
                "request_ids": sorted([replaced_id, next_id]),
                "selected": {
                    "request_id": replaced_id,
                    "float64_candidate_value": repr(a["value"]),
                    "fixed_candidate_value": repr(a["fixed_value"]),
                },
                "alternative": {
                    "request_id": next_id,
                    "float64_candidate_value": repr(b["value"]),
                    "fixed_candidate_value": repr(b["fixed_value"]),
                },
                "difference": repr(float(difference)),
                "tolerance": decimal_string(tolerance),
                "error_bound": decimal_string(
                    max(
                        Decimal(a["cumulative_error_bound"]),
                        Decimal(b["cumulative_error_bound"]),
                    )
                ),
                "flip_if_fixed": fixed_choice != float_choice,
                "sampled": True,
                "conversion_steps": _operation_refs(batch_id, metric, a)
                + _operation_refs(batch_id, metric, b),
                "spectrum_refs": [
                    f"{batch_id}/{metric}/fixed",
                    f"{batch_id}/{metric}/float64",
                ],
            }
        )
    return result


def _capacity_sensitivity(
    *,
    batch_id: str,
    step_index: int,
    clock_ms: int,
    selected_ids: list[int],
    replica: ReplicaSpec,
    config: PrecisionConfig,
    estimates: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    limits = {
        "compute": Decimal(str(replica.compute_units)),
        "memory": Decimal(replica.memory_limit_mib),
    }
    for metric, limit in limits.items():
        estimate = estimates[metric]
        difference = abs(Decimal(estimate["float64"]["value"]) - limit)
        if difference > config.float_tolerance:
            continue
        fixed_fit = Decimal(estimate["fixed"]["value"]) <= limit
        float_fit = Decimal(estimate["float64"]["value"]) <= limit
        if fixed_fit == float_fit:
            continue
        result.append(
            {
                "sensitivity_id": f"{batch_id}:capacity:{metric}",
                "kind": "capacity_threshold",
                "batch_id": batch_id,
                "step_index": step_index,
                "logical_time_ms": clock_ms,
                "metric": metric,
                "decision": "batch capacity admission",
                "request_ids": sorted(selected_ids),
                "selected": {
                    "replica_id": replica.replica_id,
                    "fixed_value": estimate["fixed"]["value"],
                    "float64_value": estimate["float64"]["value"],
                    "fixed_fit": fixed_fit,
                },
                "alternative": {
                    "limit": decimal_string(limit),
                    "float64_fit": float_fit,
                },
                "difference": decimal_string(difference),
                "tolerance": decimal_string(config.float_tolerance),
                "error_bound": estimate["cumulative_error_bound"],
                "flip_if_fixed": fixed_fit != float_fit,
                "sampled": True,
                "conversion_steps": _operation_refs(batch_id, metric, estimate),
                "spectrum_refs": [
                    f"{batch_id}/{metric}/fixed",
                    f"{batch_id}/{metric}/float64",
                ],
            }
        )
    return result


def _request_update_for_batch(
    request_id: int, batch_id: str, clock_ms: int
) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "final_status": "completed",
        "outcome_code": OUTCOME_COMPLETED,
        "batch_id": batch_id,
        "events": [
            {
                "from_status": "queued",
                "to_status": "batched",
                "outcome_code": OUTCOME_BATCHED,
                "batch_id": batch_id,
            },
            {
                "from_status": "batched",
                "to_status": "completed",
                "outcome_code": OUTCOME_COMPLETED,
                "batch_id": batch_id,
            },
        ],
    }


def _simple_request_update(
    request_id: int, status: str, outcome_code: str
) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "final_status": status,
        "outcome_code": outcome_code,
        "batch_id": None,
        "events": [
            {
                "from_status": "queued",
                "to_status": status,
                "outcome_code": outcome_code,
                "batch_id": None,
            }
        ],
    }


def _apply_request_update(
    connection: sqlite3.Connection,
    update: dict[str, Any],
    *,
    step_index: int,
    version: int,
    clock_ms: int,
) -> None:
    request_id = int(update["request_id"])
    row = connection.execute(
        "SELECT status, batch_id FROM requests WHERE request_id = ?", (request_id,)
    ).fetchone()
    if row is None:
        raise BusinessError(
            "E_DB_SCHEMA", "request row is missing", {"request_id": request_id}
        )
    current = str(row["status"])
    final_status = str(update["final_status"])
    batch_id = update.get("batch_id")
    for event_no, event in enumerate(update["events"], start=1):
        from_status = str(event["from_status"])
        to_status = str(event["to_status"])
        if current == from_status:
            set_request_status(
                connection,
                request_id,
                from_status=from_status,
                to_status=to_status,
                outcome_code=str(event["outcome_code"]),
                step_index=step_index,
                clock_ms=clock_ms,
                batch_id=event.get("batch_id"),
                event_no=event_no,
            )
            current = to_status
        elif current == to_status:
            continue
        elif current == final_status:
            continue
        elif from_status == "queued" and current in {"batched", "completed", "expired", "failed"}:
            # Recovery can encounter a later durable status. The final status and
            # unique batch assignment remain authoritative.
            continue
        else:
            raise BusinessError(
                "E_STATE_CONFLICT",
                "request transition conflicts with persisted state",
                {
                    "request_id": request_id,
                    "persisted": current,
                    "from_status": from_status,
                    "to_status": to_status,
                },
            )
    if current != final_status:
        row = connection.execute(
            "SELECT status FROM requests WHERE request_id = ?", (request_id,)
        ).fetchone()
        if row is None or row["status"] != final_status:
            raise BusinessError(
                "E_STATE_CONFLICT",
                "request did not reach its final status",
                {"request_id": request_id, "expected": final_status, "actual": row["status"]},
            )
    if batch_id is not None:
        connection.execute(
            """
            INSERT INTO batch_requests(request_id, batch_id, step_index, version)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(request_id) DO NOTHING
            """,
            (request_id, batch_id, step_index, version),
        )
        assigned = connection.execute(
            "SELECT batch_id FROM batch_requests WHERE request_id = ?", (request_id,)
        ).fetchone()
        if assigned is None or assigned["batch_id"] != batch_id:
            raise BusinessError(
                "E_DUPLICATE_REQUEST_ASSIGNMENT",
                "request is already assigned to a different batch",
                {
                    "request_id": request_id,
                    "existing_batch_id": None if assigned is None else assigned["batch_id"],
                    "new_batch_id": batch_id,
                },
            )


def _batch_payload(
    *,
    batch_id: str,
    batch_seq: int,
    replica: ReplicaSpec,
    step_index: int,
    clock_ms: int,
    version: int,
    request_ids: list[int],
    requests: dict[int, dict[str, Any]],
    queue_before: int,
    queue_after: int,
    estimates: dict[str, dict[str, Any]],
    sensitivity: list[dict[str, Any]],
    sampled: bool,
) -> dict[str, Any]:
    specs = _request_specs(request_ids, requests)
    return {
        "batch_id": batch_id,
        "batch_seq": batch_seq,
        "replica_id": replica.replica_id,
        "step_index": step_index,
        "logical_time_ms": clock_ms,
        "version": version,
        "request_ids": request_ids,
        "input_tokens": sum(request.input_tokens for request in specs),
        "output_tokens": sum(request.output_tokens for request in specs),
        "max_sequence_tokens": max(request.sequence_tokens for request in specs),
        "queue_before": queue_before,
        "queue_after": queue_after,
        "queue_watermark": queue_before,
        "sampled": sampled,
        "metrics": estimates,
        "precision_sensitivity": sensitivity,
    }


def _insert_batch(
    connection: sqlite3.Connection,
    payload: dict[str, Any],
    *,
    status: str,
) -> None:
    existing = connection.execute(
        """
        SELECT batch_seq, step_index, request_ids_json, spectrum_payload_json,
               diagnostics_json, status
        FROM batches WHERE batch_id = ?
        """,
        (payload["batch_id"],),
    ).fetchone()
    encoded = canonical_json(payload["metrics"])
    if existing is not None:
        if (
            existing["spectrum_payload_json"] != encoded
            or existing["diagnostics_json"]
            != canonical_json(payload.get("precision_sensitivity", []))
            or existing["request_ids_json"] != canonical_json(payload["request_ids"])
            or int(existing["batch_seq"]) != int(payload["batch_seq"])
            or int(existing["step_index"]) != int(payload["step_index"])
        ):
            raise BusinessError(
                "E_STATE_CONFLICT",
                "batch payload conflicts with an existing batch",
                {"batch_id": payload["batch_id"]},
            )
        return
    connection.execute(
        """
        INSERT INTO batches(
            batch_id, batch_seq, replica_id, step_index, clock_ms, version,
            status, request_ids_json, queue_before, queue_after,
            spectrum_payload_json, diagnostics_json
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload["batch_id"],
            payload["batch_seq"],
            payload["replica_id"],
            payload["step_index"],
            payload["logical_time_ms"],
            payload["version"],
            status,
            canonical_json(payload["request_ids"]),
            payload["queue_before"],
            payload["queue_after"],
            encoded,
            canonical_json(payload.get("precision_sensitivity", [])),
        ),
    )


def _insert_spectrum(
    connection: sqlite3.Connection,
    batch_id: str,
    metrics: dict[str, dict[str, Any]],
    *,
    status: str,
    only_metric: str | None = None,
) -> None:
    for metric in METRICS:
        if only_metric is not None and metric != only_metric:
            continue
        encoded = canonical_json(metrics[metric])
        existing = connection.execute(
            "SELECT payload_json FROM spectrum WHERE batch_id = ? AND metric = ?",
            (batch_id, metric),
        ).fetchone()
        if existing is not None:
            if existing["payload_json"] != encoded:
                raise BusinessError(
                    "E_STATE_MISMATCH",
                    "existing spectrum entry conflicts with committed batch payload",
                    {"batch_id": batch_id, "metric": metric},
                    exit_code=3,
                )
            connection.execute(
                "UPDATE spectrum SET status = ? WHERE batch_id = ? AND metric = ?",
                (status, batch_id, metric),
            )
            continue
        connection.execute(
            """
            INSERT INTO spectrum(batch_id, metric, status, payload_json)
            VALUES(?, ?, ?, ?)
            """,
            (batch_id, metric, status, encoded),
        )


def _commit_step(
    connection: sqlite3.Connection,
    *,
    state_before: dict[str, Any],
    step_payload: dict[str, Any],
    status: str = "committed",
    torn_spectrum: bool = False,
) -> None:
    step_index = int(step_payload["step_index"])
    version = int(step_payload["version"])
    existing = connection.execute(
        "SELECT payload_json, status FROM steps WHERE step_index = ?", (step_index,)
    ).fetchone()
    if existing is not None and existing["status"] == "committed":
        if existing["payload_json"] != canonical_json(step_payload):
            raise BusinessError(
                "E_STATE_CONFLICT",
                "step was already committed with a different payload",
                {"step_index": step_index},
            )
        return
    batch = step_payload.get("batch")
    if batch is not None:
        _insert_batch(connection, batch, status=status)
        _insert_spectrum(
            connection,
            batch["batch_id"],
            batch["metrics"],
            status=status,
            only_metric="compute" if torn_spectrum else None,
        )
    for update in step_payload["request_updates"]:
        _apply_request_update(
            connection,
            update,
            step_index=step_index,
            version=version,
            clock_ms=int(step_payload["logical_time_ms"]),
        )
    encoded = canonical_json(step_payload)
    if existing is None:
        connection.execute(
            """
            INSERT INTO steps(
                step_index, clock_ms, version, status, queue_before,
                queue_after, arrivals_json, batch_id, payload_json
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                step_index,
                step_payload["logical_time_ms"],
                version,
                status,
                step_payload["queue_before"],
                step_payload["queue_after"],
                canonical_json(step_payload["arrivals"]),
                batch["batch_id"] if batch else None,
                encoded,
            ),
        )
    else:
        connection.execute(
            """
            UPDATE steps
            SET status = ?, payload_json = ?, batch_id = ?,
                queue_before = ?, queue_after = ?, arrivals_json = ?
            WHERE step_index = ?
            """,
            (
                status,
                encoded,
                batch["batch_id"] if batch else None,
                step_payload["queue_before"],
                step_payload["queue_after"],
                canonical_json(step_payload["arrivals"]),
                step_index,
            ),
        )
    state_after = step_payload["state_after"]
    state_set(
        connection,
        clock_ms=int(state_after["clock_ms"]),
        step_index=int(state_after["step_index"]),
        version=int(state_after["version"]),
        queue=list(state_after["queue"]),
        high_watermark=int(state_after["high_watermark"]),
        cumulative=state_after["cumulative"],
    )


def _build_step(
    *,
    connection: sqlite3.Connection,
    workspace: WorkspaceSpec,
    state: dict[str, Any],
    clock_ms: int,
) -> dict[str, Any]:
    config = workspace.precision
    requests = {
        request.request_id: {
            "request_id": request.request_id,
            "arrival_ms": request.arrival_ms,
            "deadline_ms": request.deadline_ms,
            "service_class": request.service_class,
            "input_tokens": request.input_tokens,
            "output_tokens": request.output_tokens,
            "status": "queued",
        }
        for request in workspace.requests
    }
    rows = connection.execute(
        "SELECT request_id, status FROM requests WHERE status = 'queued'"
    ).fetchall()
    queued_now = {int(row["request_id"]) for row in rows}
    for request_id, row in requests.items():
        row["status"] = "queued" if request_id in queued_now else "inactive"

    queue = sorted(
        {
            request_id
            for request_id in state["queue"]
            if requests[request_id]["status"] == "queued"
        }
        | {
            request.request_id
            for request in workspace.requests
            if requests[request.request_id]["status"] == "queued"
            and request.arrival_ms <= clock_ms
        },
        key=lambda request_id: (
            requests[request_id]["arrival_ms"],
            request_id,
        ),
    )
    arrivals = sorted(
        request_id
        for request_id in queue
        if request_id not in set(state["queue"])
    )

    expired = [
        request_id
        for request_id in queue
        if requests[request_id]["deadline_ms"] < clock_ms
    ]
    expired_set = set(expired)
    queue = [request_id for request_id in queue if request_id not in expired_set]
    queue_before = len(queue)
    next_version = int(state["version"]) + 1
    next_step = int(state["step_index"]) + 1
    batch_count = int(
        connection.execute("SELECT COUNT(*) AS n FROM batches").fetchone()["n"]
    )
    batch_seq = batch_count + 1
    batch_id = f"b{batch_seq:06d}"
    replica, selected_ids, estimates = _select_replica_and_batch(
        queue=queue,
        requests=requests,
        model=workspace.model,
        replicas=workspace.replicas,
        config=config,
        cumulative=state["cumulative"],
        queue_before=queue_before,
        batch_id=batch_id,
    )

    failed: list[int] = []
    sensitivity: list[dict[str, Any]] = []
    batch_payload: dict[str, Any] | None = None
    if queue and (replica is None or not selected_ids or estimates is None):
        failed = [queue[0]]
        queue = queue[1:]
    elif replica is not None and selected_ids and estimates is not None:
        queue = [request_id for request_id in queue if request_id not in set(selected_ids)]
        queue_after = len(queue)
        sampled = _deterministically_sampled(batch_id, config.sample_ratio)
        if sampled:
            specs = _request_specs(selected_ids, requests)
            sensitivity.extend(
                _replica_sensitivity(
                    batch_id=batch_id,
                    step_index=next_step,
                    clock_ms=clock_ms,
                    selected_replica=replica,
                    selected_ids=selected_ids,
                    request_specs=specs,
                    requests=requests,
                    model=workspace.model,
                    replicas=workspace.replicas,
                    config=config,
                    cumulative=state["cumulative"],
                    queue_before=queue_before,
                    selected_estimates=estimates,
                )
            )
            sensitivity.extend(
                _boundary_sensitivity(
                    batch_id=batch_id,
                    step_index=next_step,
                    clock_ms=clock_ms,
                    selected_ids=selected_ids,
                    queue=queue_before_ids(queue, selected_ids),
                    replica=replica,
                    requests=requests,
                    model=workspace.model,
                    config=config,
                    previous=state["cumulative"].get(replica.replica_id, {}),
                    queue_before=queue_before,
                    selected_estimates=estimates,
                )
            )
            sensitivity.extend(
                _capacity_sensitivity(
                    batch_id=batch_id,
                    step_index=next_step,
                    clock_ms=clock_ms,
                    selected_ids=selected_ids,
                    replica=replica,
                    config=config,
                    estimates=estimates,
                )
            )
        sensitivity.sort(key=lambda item: item["sensitivity_id"])
        batch_payload = _batch_payload(
            batch_id=batch_id,
            batch_seq=batch_seq,
            replica=replica,
            step_index=next_step,
            clock_ms=clock_ms,
            version=next_version,
            request_ids=selected_ids,
            requests=requests,
            queue_before=queue_before,
            queue_after=queue_after,
            estimates=estimates,
            sensitivity=sensitivity,
            sampled=sampled,
        )
    queue_after = len(queue)
    cumulative_after = copy.deepcopy(state["cumulative"])
    if batch_payload is not None and replica is not None and estimates is not None:
        cumulative_after[replica.replica_id] = compact_cumulative(
            estimates, batch_id
        )

    request_updates: list[dict[str, Any]] = []
    if batch_payload is not None:
        for request_id in selected_ids:
            request_updates.append(
                _request_update_for_batch(request_id, batch_id, clock_ms)
            )
    for request_id in expired:
        request_updates.append(
            _simple_request_update(request_id, "expired", OUTCOME_EXPIRED)
        )
    for request_id in failed:
        request_updates.append(
            _simple_request_update(request_id, "failed", OUTCOME_FAILED)
        )
    request_updates.sort(key=lambda item: item["request_id"])

    state_after = {
        "clock_ms": clock_ms + config.clock_step_ms,
        "step_index": next_step,
        "version": next_version,
        "queue": queue,
        "high_watermark": max(int(state["high_watermark"]), queue_before),
        "cumulative": cumulative_after,
    }
    return {
        "step_index": next_step,
        "logical_time_ms": clock_ms,
        "version": next_version,
        "arrivals": arrivals,
        "expired_ids": expired,
        "failed_ids": failed,
        "queue_before": queue_before,
        "queue_after": queue_after,
        "queue_delta": queue_after - queue_before,
        "batch": batch_payload,
        "request_updates": request_updates,
        "state_after": state_after,
    }


def queue_before_ids(queue_after: list[int], selected_ids: list[int]) -> list[int]:
    """Reconstruct the pre-dequeue queue for a quota-boundary comparison."""
    # The selected IDs are a prefix of the original deterministic queue in normal
    # operation. Appending any residual candidates after the selected prefix is
    # sufficient because _boundary_sensitivity only inspects the next candidate.
    return list(selected_ids) + list(queue_after)


def _advance_one_locked(
    connection: sqlite3.Connection,
    workspace: WorkspaceSpec,
    *,
    clock_ms: int,
    torn: bool = False,
) -> dict[str, Any]:
    state = state_get(connection)
    if state["clock_ms"] != clock_ms:
        raise BusinessError(
            "E_STATE_CONFLICT",
            "logical clock changed while holding the workbench lock",
            {"expected": state["clock_ms"], "actual": clock_ms},
        )
    step = _build_step(
        connection=connection,
        workspace=workspace,
        state=state,
        clock_ms=clock_ms,
    )
    fault_before = _fault_requested("kill_before_commit", step["step_index"])
    with transaction(connection):
        _commit_step(
            connection,
            state_before=state,
            step_payload=step,
            status="torn" if torn else "committed",
            torn_spectrum=torn,
        )
        if fault_before:
            os._exit(137)
    if _fault_requested("kill_after_commit", step["step_index"]):
        os._exit(137)
    return step


def run_recovery(connection: sqlite3.Connection, *, apply: bool = True) -> dict[str, Any]:
    """Materialize missing atomic artifacts from already committed payloads."""
    repaired_steps: list[int] = []
    repaired_batches: list[str] = []
    inserted_entries: list[str] = []
    recommitted_entries: list[str] = []
    seen_batches: set[str] = set()

    batch_rows = connection.execute(
        "SELECT * FROM batches ORDER BY batch_seq"
    ).fetchall()
    step_rows = connection.execute("SELECT * FROM steps ORDER BY step_index").fetchall()
    step_payload_by_batch: dict[str, dict[str, Any]] = {}
    for row in step_rows:
        payload = json.loads(row["payload_json"])
        batch = payload.get("batch")
        if batch is not None:
            step_payload_by_batch[batch["batch_id"]] = payload

    for row in batch_rows:
        batch_id = str(row["batch_id"])
        seen_batches.add(batch_id)
        spectrum_rows = connection.execute(
            "SELECT metric, status, payload_json FROM spectrum WHERE batch_id = ?",
            (batch_id,),
        ).fetchall()
        present = {str(item["metric"]): str(item["payload_json"]) for item in spectrum_rows}
        complete_status = {
            str(item["metric"]): str(item["status"]) == "committed"
            for item in spectrum_rows
        }
        metrics = json.loads(row["spectrum_payload_json"])
        missing = [
            metric
            for metric in METRICS
            if metric not in present
            or present[metric] != canonical_json(metrics[metric])
            or not complete_status.get(metric, False)
        ]
        if missing and not apply:
            raise StateMismatchError(
                "committed batch has incomplete or conflicting spectrum entries",
                {"batch_id": batch_id, "metrics": missing},
            )
        if missing:
            with transaction(connection):
                for metric in missing:
                    existing_payload = present.get(metric)
                    entry_key = f"{batch_id}/{metric}"
                    if existing_payload is None or existing_payload != canonical_json(
                        metrics[metric]
                    ):
                        inserted_entries.append(entry_key)
                    else:
                        recommitted_entries.append(entry_key)
                    connection.execute(
                        """
                        INSERT INTO spectrum(batch_id, metric, status, payload_json)
                        VALUES(?, ?, 'committed', ?)
                        ON CONFLICT(batch_id, metric) DO UPDATE SET
                            status = 'committed',
                            payload_json = excluded.payload_json
                        """,
                        (batch_id, metric, canonical_json(metrics[metric])),
                    )
                connection.execute(
                    "UPDATE batches SET status = 'committed' WHERE batch_id = ?",
                    (batch_id,),
                )
            repaired_batches.append(batch_id)
        elif row["status"] != "committed":
            if apply:
                connection.execute(
                    "UPDATE batches SET status = 'committed' WHERE batch_id = ?",
                    (batch_id,),
                )
                repaired_batches.append(batch_id)

    for row in step_rows:
        if row["status"] == "committed":
            continue
        step_index = int(row["step_index"])
        payload = json.loads(row["payload_json"])
        batch = payload.get("batch")
        if batch is not None and batch["batch_id"] not in seen_batches:
            raise StateMismatchError(
                "step refers to a missing batch", {"step_index": step_index}
            )
        if not apply:
            raise StateMismatchError(
                "step is not committed", {"step_index": step_index, "status": row["status"]}
            )
        with transaction(connection):
            if batch is not None:
                _insert_spectrum(
                    connection,
                    batch["batch_id"],
                    batch["metrics"],
                    status="committed",
                )
                connection.execute(
                    "UPDATE batches SET status = 'committed' WHERE batch_id = ?",
                    (batch["batch_id"],),
                )
            for update in payload["request_updates"]:
                _apply_request_update(
                    connection,
                    update,
                    step_index=step_index,
                    version=int(payload["version"]),
                    clock_ms=int(payload["logical_time_ms"]),
                )
            connection.execute(
                "UPDATE steps SET status = 'committed' WHERE step_index = ?",
                (step_index,),
            )
            state_after = payload["state_after"]
            current = state_get(connection)
            if current["version"] < int(state_after["version"]):
                state_set(
                    connection,
                    clock_ms=int(state_after["clock_ms"]),
                    step_index=int(state_after["step_index"]),
                    version=int(state_after["version"]),
                    queue=list(state_after["queue"]),
                    high_watermark=int(state_after["high_watermark"]),
                    cumulative=state_after["cumulative"],
                )
            elif current["version"] == int(state_after["version"]):
                if canonical_json(current["cumulative"]) != canonical_json(
                    state_after["cumulative"]
                ):
                    raise StateMismatchError(
                        "persisted cumulative error state differs from committed payload",
                        {"step_index": step_index},
                    )
            else:
                raise StateMismatchError(
                    "persisted state is ahead of an unfinished step",
                    {
                        "step_index": step_index,
                        "state_version": current["version"],
                        "step_version": state_after["version"],
                    },
                )
        repaired_steps.append(step_index)

    return {
        "repaired_steps": sorted(repaired_steps),
        "repaired_batches": sorted(set(repaired_batches)),
        "inserted_spectrum_entries": sorted(inserted_entries),
        "recommitted_spectrum_entries": sorted(set(recommitted_entries)),
        "pending_steps": len(repaired_steps),
    }


def advance(
    db_path: str | os.PathLike[str],
    *,
    steps: int = 1,
    to_time: int | None = None,
    recover_first: bool = True,
    until_complete: bool = False,
) -> dict[str, Any]:
    if steps < 0:
        raise BusinessError("E_USAGE", "steps must be non-negative", {"steps": steps})
    path = Path(db_path)
    if not path.exists():
        raise BusinessError("E_DB_NOT_FOUND", "state database does not exist", {"db": str(path)})
    with FileLock(lock_path(path)):
        connection = connect(path)
        try:
            workspace = workspace_from_db(connection)
            pre_recovery_state = state_get(connection)
            if to_time is not None:
                tolerance = workspace.precision.clock_rollback_tolerance_ms
                if to_time < pre_recovery_state["clock_ms"] - tolerance:
                    raise BusinessError(
                        "E_CLOCK_ROLLBACK",
                        "logical clock rollback exceeds the configured tolerance",
                        {
                            "target_ms": to_time,
                            "next_clock_ms": pre_recovery_state["clock_ms"],
                            "tolerance_ms": tolerance,
                        },
                    )
            recovery: dict[str, Any] = {
                "repaired_steps": [],
                "repaired_batches": [],
                "inserted_spectrum_entries": [],
                "recommitted_spectrum_entries": [],
                "pending_steps": 0,
            }
            if recover_first:
                recovery = run_recovery(connection)
            state = state_get(connection)
            if to_time is not None:
                step_clock = workspace.precision.clock_step_ms
                requested_steps = max(
                    1, (to_time - state["clock_ms"]) // step_clock + 1
                )
                if steps != 1:
                    raise BusinessError(
                        "E_USAGE", "use either --steps or --to, not both"
                    )
                steps = int(requested_steps)
            if until_complete:
                if to_time is not None:
                    raise BusinessError(
                        "E_USAGE",
                        "--to cannot be combined with automatic resume completion",
                    )
                final_count = int(
                    connection.execute(
                        "SELECT COUNT(*) AS n FROM requests "
                        "WHERE status NOT IN ('completed','expired','failed')"
                    ).fetchone()["n"]
                )
                if final_count == 0:
                    steps = 0
                else:
                    horizon = max(
                        max(
                            request.arrival_ms,
                            request.deadline_ms,
                        )
                        for request in workspace.requests
                    )
                    steps = max(
                        1,
                        (max(0, horizon - state["clock_ms"])
                         + workspace.precision.clock_step_ms
                         - 1)
                        // workspace.precision.clock_step_ms
                        + len(workspace.requests)
                        + 2,
                    )
            executed: list[dict[str, Any]] = []
            for _ in range(steps):
                current = state_get(connection)
                clock_ms = current["clock_ms"]
                torn = _fault_requested("torn_spectrum", current["step_index"] + 1)
                step = _advance_one_locked(
                    connection, workspace, clock_ms=clock_ms, torn=torn
                )
                executed.append(
                    {
                        "step_index": step["step_index"],
                        "logical_time_ms": step["logical_time_ms"],
                        "queue_before": step["queue_before"],
                        "queue_after": step["queue_after"],
                        "batch_id": (
                            step["batch"]["batch_id"] if step["batch"] is not None else None
                        ),
                        "batch_request_ids": (
                            step["batch"]["request_ids"]
                            if step["batch"] is not None
                            else []
                        ),
                        "expired_ids": step["expired_ids"],
                        "failed_ids": step["failed_ids"],
                    }
                )
                if until_complete:
                    remaining = int(
                        connection.execute(
                            "SELECT COUNT(*) AS n FROM requests "
                            "WHERE status NOT IN ('completed','expired','failed')"
                        ).fetchone()["n"]
                    )
                    if remaining == 0:
                        break
                if torn:
                    # A normal process cannot continue after the simulated torn write.
                    os._exit(138)
            if until_complete:
                remaining = int(
                    connection.execute(
                        "SELECT COUNT(*) AS n FROM requests "
                        "WHERE status NOT IN ('completed','expired','failed')"
                    ).fetchone()["n"]
                )
                if remaining:
                    raise BusinessError(
                        "E_STATE_CONFLICT",
                        "automatic resume could not reach final outcomes",
                        {"remaining_requests": remaining},
                    )
            return {
                "recovery": recovery,
                "executed": executed,
                "state": state_get(connection),
            }
        finally:
            connection.close()


def resume(
    db_path: str | os.PathLike[str],
    *,
    steps: int | None = None,
    to_time: int | None = None,
) -> dict[str, Any]:
    if steps is None and to_time is None:
        return advance(db_path, steps=1, recover_first=True, until_complete=True)
    return advance(
        db_path,
        steps=1 if steps is None else steps,
        to_time=to_time,
        recover_first=True,
    )
