from __future__ import annotations

import json
import os
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from .errors import BusinessError, StateMismatchError
from .jsonutil import canonical_json, digest_json, read_json
from .locking import FileLock
from .spec import METRICS
from .store import (
    SCHEMA_VERSION,
    connect,
    lock_path,
    meta_get,
    request_rows,
    state_get,
    workspace_from_db,
)


def _pending_artifacts(connection: sqlite3.Connection) -> dict[str, Any]:
    noncommitted_steps = [
        int(row["step_index"])
        for row in connection.execute(
            "SELECT step_index FROM steps WHERE status != 'committed' ORDER BY step_index"
        )
    ]
    noncommitted_batches = [
        str(row["batch_id"])
        for row in connection.execute(
            "SELECT batch_id FROM batches WHERE status != 'committed' ORDER BY batch_seq"
        )
    ]
    incomplete_spectrum: list[str] = []
    batches = connection.execute(
        "SELECT batch_id, spectrum_payload_json FROM batches ORDER BY batch_seq"
    ).fetchall()
    for batch in batches:
        rows = connection.execute(
            "SELECT metric, status, payload_json FROM spectrum WHERE batch_id = ?",
            (batch["batch_id"],),
        ).fetchall()
        present = {str(row["metric"]): str(row["payload_json"]) for row in rows}
        committed = {
            str(row["metric"]): str(row["status"]) == "committed" for row in rows
        }
        expected = json.loads(batch["spectrum_payload_json"])
        for metric in METRICS:
            if (
                present.get(metric) != canonical_json(expected[metric])
                or not committed.get(metric, False)
            ):
                incomplete_spectrum.append(f"{batch['batch_id']}/{metric}")
    return {
        "noncommitted_steps": noncommitted_steps,
        "noncommitted_batches": noncommitted_batches,
        "incomplete_spectrum": sorted(incomplete_spectrum),
    }


def _batch_report_rows(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    batches = connection.execute(
        "SELECT * FROM batches WHERE status = 'committed' ORDER BY step_index, batch_seq"
    ).fetchall()
    result: list[dict[str, Any]] = []
    for batch in batches:
        step = connection.execute(
            "SELECT payload_json FROM steps WHERE step_index = ?",
            (batch["step_index"],),
        ).fetchone()
        if step is None:
            raise StateMismatchError(
                "committed batch has no corresponding step",
                {"batch_id": batch["batch_id"], "step_index": batch["step_index"]},
            )
        step_payload = json.loads(step["payload_json"])
        batch_payload = step_payload.get("batch") or {}
        metric_rows = connection.execute(
            "SELECT metric, payload_json FROM spectrum "
            "WHERE batch_id = ? AND status = 'committed'",
            (batch["batch_id"],),
        ).fetchall()
        metrics = {
            str(row["metric"]): json.loads(row["payload_json"]) for row in metric_rows
        }
        missing = [metric for metric in METRICS if metric not in metrics]
        if missing:
            raise StateMismatchError(
                "committed batch is missing spectrum entries",
                {"batch_id": batch["batch_id"], "missing": missing},
            )
        diagnostics = json.loads(batch["diagnostics_json"])
        result.append(
            {
                "batch_id": str(batch["batch_id"]),
                "batch_seq": int(batch["batch_seq"]),
                "replica_id": str(batch["replica_id"]),
                "step_index": int(batch["step_index"]),
                "logical_time_ms": int(batch["clock_ms"]),
                "version": int(batch["version"]),
                "request_ids": json.loads(batch["request_ids_json"]),
                "input_tokens": batch_payload.get("input_tokens"),
                "output_tokens": batch_payload.get("output_tokens"),
                "max_sequence_tokens": batch_payload.get("max_sequence_tokens"),
                "sampled": batch_payload.get("sampled", True),
                "queue_before": int(batch["queue_before"]),
                "queue_after": int(batch["queue_after"]),
                "queue_watermark": int(batch["queue_before"]),
                "metrics": {metric: metrics[metric] for metric in METRICS},
                "precision_sensitivity": sorted(
                    diagnostics, key=lambda item: item["sensitivity_id"]
                ),
            }
        )
    return result


def _amplification_markers(
    connection: sqlite3.Connection, threshold: int
) -> list[dict[str, Any]]:
    markers: list[dict[str, Any]] = []
    batches = _batch_report_rows(connection)
    for metric in METRICS:
        first: dict[str, Any] | None = None
        for batch in batches:
            estimate = batch["metrics"][metric]
            watermark = int(
                estimate.get("propagation", {})
                .get("queue_watermark", {})
                .get("depth", 0)
            )
            bound = Decimal(estimate["cumulative_error_bound"])
            budget = Decimal(estimate["budget"])
            multiplicative = bool(
                estimate.get("propagation", {})
                .get("queue_watermark", {})
                .get("used_multiplicatively", False)
            )
            chain = list(estimate.get("accumulation_chain", []))
            if watermark <= threshold:
                continue
            if not multiplicative:
                continue
            if bound <= budget:
                continue
            first = {
                "marker_id": f"amplification:{metric}:{batch['batch_id']}",
                "metric": metric,
                "first_over_budget_step_index": batch["step_index"],
                "first_over_budget_time_ms": batch["logical_time_ms"],
                "first_over_budget_batch_id": batch["batch_id"],
                "replica_id": batch["replica_id"],
                "queue_threshold": threshold,
                "observed_queue_watermark": watermark,
                "queue_over_threshold_by": watermark - threshold,
                "error_bound": estimate["cumulative_error_bound"],
                "error_budget": estimate["budget"],
                "over_budget_multiple": str(bound / budget),
                "budget_basis": estimate["bound_basis"],
                "queue_participates_multiplicatively": multiplicative,
                "batch_chain": chain,
                "participating_request_ids": _chain_request_ids(batches, chain),
                "evidence": [
                    {
                        "batch_id": batch_id,
                        "spectrum_ref": f"{batch_id}/{metric}/fixed",
                        "float64_spectrum_ref": f"{batch_id}/{metric}/float64",
                    }
                    for batch_id in chain
                ],
                "derivation": (
                    "stepwise cumulative error bounds persisted in committed "
                    "spectrum entries; no retrospective fitting"
                ),
            }
            break
        if first is not None:
            markers.append(first)
    markers.sort(key=lambda item: (item["first_over_budget_step_index"], item["metric"]))
    return markers


def _chain_request_ids(
    batches: list[dict[str, Any]], chain: Iterable[str]
) -> list[int]:
    by_id = {batch["batch_id"]: batch for batch in batches}
    request_ids: set[int] = set()
    for batch_id in chain:
        batch = by_id.get(batch_id)
        if batch is not None:
            request_ids.update(int(value) for value in batch["request_ids"])
    return sorted(request_ids)


def _request_outcomes(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        {
            "request_id": row["request_id"],
            "arrival_ms": row["arrival_ms"],
            "deadline_ms": row["deadline_ms"],
            "service_class": row["service_class"],
            "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"],
            "status": row["status"],
            "outcome_code": row["outcome_code"],
            "batch_id": row["batch_id"],
            "assigned_step": row["assigned_step"],
            "status_clock_ms": row["status_clock_ms"],
        }
        for row in request_rows(connection).values()
    ]


def build_report(connection: sqlite3.Connection) -> dict[str, Any]:
    workspace = workspace_from_db(connection)
    state = state_get(connection)
    batches = _batch_report_rows(connection)
    amplification = _amplification_markers(
        connection, workspace.precision.queue_threshold
    )
    sensitivity: list[dict[str, Any]] = []
    for batch in batches:
        sensitivity.extend(batch["precision_sensitivity"])
    sensitivity.sort(
        key=lambda item: (
            item["step_index"],
            item["batch_id"],
            item["sensitivity_id"],
        )
    )
    outcomes = _request_outcomes(connection)
    status_counts: dict[str, int] = {}
    for outcome in outcomes:
        status_counts[outcome["status"]] = (
            status_counts.get(outcome["status"], 0) + 1
        )
    pending = _pending_artifacts(connection)
    core = {
        "schema_version": SCHEMA_VERSION,
        "sample_sha256": workspace.sample_hash,
        "model": workspace.model.as_dict(),
        "replicas": [replica.as_dict() for replica in workspace.replicas],
        "precision_policy": workspace.precision.as_dict(),
        "state": {
            "clock_ms": state["clock_ms"],
            "step_index": state["step_index"],
            "version": state["version"],
            "queue_count": state["queue_count"],
            "queue": state["queue"],
            "high_watermark": state["high_watermark"],
        },
        "batch_spectrum": batches,
        "amplification": amplification,
        "precision_sensitivity": sensitivity,
        "request_outcomes": outcomes,
        "summary": {
            "batch_count": len(batches),
            "amplification_marker_count": len(amplification),
            "precision_sensitivity_marker_count": len(sensitivity),
            "request_status_counts": dict(sorted(status_counts.items())),
            "pending_artifacts": pending,
        },
    }
    core["report_digest"] = digest_json(core)
    return core


def report_from_db(db_path: str | os.PathLike[str]) -> dict[str, Any]:
    path = Path(db_path)
    if not path.exists():
        raise BusinessError(
            "E_DB_NOT_FOUND", "state database does not exist", {"db": str(path)}
        )
    with FileLock(lock_path(path)):
        connection = connect(path)
        try:
            return build_report(connection)
        finally:
            connection.close()


def status_from_db(db_path: str | os.PathLike[str]) -> dict[str, Any]:
    path = Path(db_path)
    if not path.exists():
        raise BusinessError(
            "E_DB_NOT_FOUND", "state database does not exist", {"db": str(path)}
        )
    with FileLock(lock_path(path)):
        connection = connect(path)
        try:
            workspace = workspace_from_db(connection)
            state = state_get(connection)
            rows = request_rows(connection)
            counts: dict[str, int] = {}
            for row in rows.values():
                counts[row["status"]] = counts.get(row["status"], 0) + 1
            batches = connection.execute("SELECT COUNT(*) AS n FROM batches").fetchone()
            steps = connection.execute("SELECT COUNT(*) AS n FROM steps").fetchone()
            return {
                "sample_sha256": workspace.sample_hash,
                "clock_ms": state["clock_ms"],
                "step_index": state["step_index"],
                "version": state["version"],
                "queue_count": state["queue_count"],
                "queue": state["queue"],
                "high_watermark": state["high_watermark"],
                "request_status_counts": dict(sorted(counts.items())),
                "batch_count": int(batches["n"]),
                "step_count": int(steps["n"]),
                "pending_artifacts": _pending_artifacts(connection),
                "cumulative": state["cumulative"],
            }
        finally:
            connection.close()


def _check_invariants(
    connection: sqlite3.Connection, report: dict[str, Any]
) -> dict[str, Any]:
    state = state_get(connection)
    workspace = workspace_from_db(connection)
    pending = _pending_artifacts(connection)
    if any(pending.values()):
        raise StateMismatchError(
            "unfinished commits remain in persistent state", {"pending": pending}
        )
    if state["queue_count"] != len(state["queue"]):
        raise StateMismatchError(
            "persisted queue_count differs from the queue contents",
            {
                "queue_count": state["queue_count"],
                "queue_length": len(state["queue"]),
            },
        )
    if len(set(state["queue"])) != len(state["queue"]):
        raise StateMismatchError("persisted queue contains duplicate request IDs")

    procedure_rows = connection.execute(
        "SELECT request_id, status FROM requests WHERE request_id IN (%s)"
        % ",".join("?" for _ in state["queue"]),
        tuple(state["queue"]),
    ).fetchall() if state["queue"] else []
    for row in procedure_rows:
        if row["status"] != "queued":
            raise StateMismatchError(
                "a persisted queue member is not in queued state",
                {"request_id": int(row["request_id"]), "status": row["status"]},
            )

    step_count = int(
        connection.execute("SELECT COUNT(*) AS n FROM steps").fetchone()["n"]
    )
    if step_count != state["step_index"] or state["version"] != state["step_index"]:
        raise StateMismatchError(
            "version, step index, and committed step count disagree",
            {
                "step_count": step_count,
                "state_step_index": state["step_index"],
                "state_version": state["version"],
            },
        )
    if step_count:
        last_step = connection.execute(
            "SELECT payload_json FROM steps ORDER BY step_index DESC LIMIT 1"
        ).fetchone()
        last_payload = json.loads(last_step["payload_json"])
        state_after = last_payload["state_after"]
        expected_state = {
            "clock_ms": state["clock_ms"],
            "step_index": state["step_index"],
            "version": state["version"],
            "queue": state["queue"],
            "high_watermark": state["high_watermark"],
            "cumulative": state["cumulative"],
        }
        if canonical_json(state_after) != canonical_json(expected_state):
            raise StateMismatchError(
                "persisted state does not match the last committed step payload"
            )

    batch_rows = connection.execute(
        "SELECT * FROM batches ORDER BY batch_seq"
    ).fetchall()
    if [int(row["batch_seq"]) for row in batch_rows] != list(
        range(1, len(batch_rows) + 1)
    ):
        raise StateMismatchError("batch sequence numbers are not contiguous")

    assignments: dict[int, str] = {}
    assignment_rows = connection.execute(
        "SELECT request_id, batch_id FROM batch_requests"
    ).fetchall()
    for row in assignment_rows:
        request_id = int(row["request_id"])
        if request_id in assignments:
            raise StateMismatchError(
                "request is assigned to more than one batch", {"request_id": request_id}
            )
        assignments[request_id] = str(row["batch_id"])
    completed_rows = connection.execute(
        "SELECT request_id, batch_id FROM requests WHERE status = 'completed'"
    ).fetchall()
    for row in completed_rows:
        request_id = int(row["request_id"])
        if assignments.get(request_id) != row["batch_id"]:
            raise StateMismatchError(
                "completed request assignment differs from batch membership",
                {
                    "request_id": request_id,
                    "request_batch_id": row["batch_id"],
                    "assignment_batch_id": assignments.get(request_id),
                },
            )

    prior_by_replica: dict[str, str] = {}
    last_cumulative: dict[str, dict[str, Any]] = {}
    for row in batch_rows:
        batch_id = str(row["batch_id"])
        request_ids = [int(value) for value in json.loads(row["request_ids_json"])]
        if len(request_ids) != len(set(request_ids)):
            raise StateMismatchError(
                "batch contains duplicate request IDs", {"batch_id": batch_id}
            )
        for request_id in request_ids:
            if assignments.get(request_id) != batch_id:
                raise StateMismatchError(
                    "batch membership table disagrees with spectrum batch",
                    {
                        "batch_id": batch_id,
                        "request_id": request_id,
                        "assignment": assignments.get(request_id),
                    },
                )
        step = connection.execute(
            "SELECT status, batch_id FROM steps WHERE step_index = ?",
            (row["step_index"],),
        ).fetchone()
        if step is None or step["status"] != "committed":
            raise StateMismatchError(
                "batch refers to a missing or unfinished step",
                {"batch_id": batch_id, "step_index": row["step_index"]},
            )
        if step["batch_id"] != batch_id:
            raise StateMismatchError(
                "step and batch references disagree", {"batch_id": batch_id}
            )
        spectrum_rows = connection.execute(
            "SELECT metric, payload_json FROM spectrum WHERE batch_id = ?",
            (batch_id,),
        ).fetchall()
        metrics = {str(item["metric"]): json.loads(item["payload_json"]) for item in spectrum_rows}
        if sorted(metrics) != sorted(METRICS):
            raise StateMismatchError(
                "batch does not have exactly one entry for each metric",
                {"batch_id": batch_id, "metrics": sorted(metrics)},
            )
        replica_id = str(row["replica_id"])
        for metric, estimate in metrics.items():
            previous_id = estimate.get("previous_batch_id")
            if previous_id != prior_by_replica.get(replica_id):
                raise StateMismatchError(
                    "cross-batch propagation source is discontinuous",
                    {
                        "batch_id": batch_id,
                        "metric": metric,
                        "expected_previous": prior_by_replica.get(replica_id),
                        "actual_previous": previous_id,
                    },
                )
            chain = estimate.get("accumulation_chain", [])
            if not chain or chain[-1] != batch_id:
                raise StateMismatchError(
                    "accumulation chain does not terminate at the batch",
                    {"batch_id": batch_id, "metric": metric, "chain": chain},
                )
        prior_by_replica[replica_id] = batch_id
        last_cumulative[replica_id] = {
            metric: {
                "batch_id": batch_id,
                "fixed_scaled": estimate["fixed"]["scaled_string"],
                "fixed_value": estimate["fixed"]["value"],
                "fixed_bound": estimate["fixed"]["cumulative_error_bound"],
                "float64": estimate["float64"]["value"],
                "float_bound": estimate["float64"]["cumulative_error_bound"],
                "pre_conversion_raw": estimate["fixed"]["pre_conversion_raw"],
                "cumulative_error_bound": estimate["cumulative_error_bound"],
                "accumulation_chain": estimate["accumulation_chain"],
            }
            for metric, estimate in metrics.items()
        }

    for replica in workspace.replicas:
        expected = last_cumulative.get(replica.replica_id, {})
        actual = state["cumulative"].get(replica.replica_id, {})
        if canonical_json(expected) != canonical_json(actual):
            raise StateMismatchError(
                "persisted cumulative error state is not the last batch spectrum state",
                {"replica_id": replica.replica_id},
            )

    for outcome in report["request_outcomes"]:
        if outcome["status"] == "expired" and outcome["outcome_code"] != "E_REQUEST_EXPIRED":
            raise StateMismatchError(
                "expired request uses the wrong outcome code",
                {"request_id": outcome["request_id"]},
            )
        if outcome["status"] == "failed" and outcome["outcome_code"] != "E_EXECUTION_FAILED":
            raise StateMismatchError(
                "failed request uses the wrong outcome code",
                {"request_id": outcome["request_id"]},
            )
    return {
        "verified_steps": step_count,
        "verified_batches": len(batch_rows),
        "verified_requests": len(report["request_outcomes"]),
        "verified_spectrum_entries": len(batch_rows) * len(METRICS),
        "report_digest": report["report_digest"],
    }


def verify_from_db(
    db_path: str | os.PathLike[str],
    expected_report_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    path = Path(db_path)
    if not path.exists():
        raise BusinessError(
            "E_DB_NOT_FOUND", "state database does not exist", {"db": str(path)}
        )
    expected: dict[str, Any] | None = None
    if expected_report_path is not None:
        raw = read_json(expected_report_path)
        if not isinstance(raw, dict):
            raise BusinessError(
                "E_REPORT_SCHEMA", "diagnostic report must be a JSON object"
            )
        expected = raw
    with FileLock(lock_path(path)):
        connection = connect(path)
        try:
            schema_version = meta_get(connection, "schema_version")
            if schema_version != str(SCHEMA_VERSION):
                raise StateMismatchError(
                    "unsupported on-disk schema version",
                    {"expected": SCHEMA_VERSION, "actual": schema_version},
                )
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise StateMismatchError(
                    "SQLite integrity_check failed", {"result": integrity}
                )
            foreign_key_errors = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
            if foreign_key_errors:
                raise StateMismatchError(
                    "SQLite foreign_key_check failed",
                    {"rows": [dict(row) for row in foreign_key_errors[:20]]},
                )
            report = build_report(connection)
            verified = _check_invariants(connection, report)
            if expected is not None:
                if canonical_json(report) != canonical_json(expected):
                    raise StateMismatchError(
                        "persisted spectrum and supplied diagnostic report differ",
                        {
                            "actual_report_digest": report.get("report_digest"),
                            "expected_report_digest": expected.get("report_digest"),
                            "actual_sample_sha256": report.get("sample_sha256"),
                            "expected_sample_sha256": expected.get("sample_sha256"),
                        },
                    )
            return verified
        finally:
            connection.close()
