"""Independent snapshot, plan, trace and result verifier."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Mapping

from .catalog import INPUT_FORMAT, load_pipeline_dict
from .codes import E_DATA, E_DIVERGE, E_INTERNAL, E_PARSE, E_PLAN, E_VERSION
from .errors import PipeError
from .executor import (
    ExecutionResult,
    compare_results,
    execute,
    serialize_result,
)
from .plan import build_plans, walk_plan
from .snapshot import (
    SNAPSHOT_FORMAT,
    SNAPSHOT_FORMAT_VERSION,
    load_snapshot,
)
from .stats import build_statistics, statistics_core_sha256
from .util import canonical_json, sha256_bytes, sha256_json, write_json


class CheckLog:
    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []

    def add(self, name: str, passed: bool, details: Any | None = None) -> bool:
        item: dict[str, Any] = {"check": name, "status": "PASS" if passed else "FAIL"}
        if details is not None:
            item["details"] = details
        self.checks.append(item)
        return passed

    @property
    def ok(self) -> bool:
        return all(item["status"] == "PASS" for item in self.checks)


def _request_from_snapshot(snapshot: Mapping[str, Any]):
    dataset = snapshot.get("dataset")
    if not isinstance(dataset, dict):
        raise PipeError(E_VERSION, "snapshot dataset is missing", "$.dataset")
    return load_pipeline_dict(
        {
            "format": snapshot.get("input_format"),
            "session_id": dataset.get("session_id"),
            "data_version": dataset.get("data_version"),
            "tables": dataset.get("tables"),
            "query": snapshot.get("query"),
            "settings": snapshot.get("settings"),
        },
        source_path="$.snapshot",
    )


def _expected_tasks(split_plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for alias in split_plan.get("scans", {}):
        tasks.extend(split_plan["scans"][alias]["tasks"])
    order = split_plan.get("task_order", [])
    mapping = {task["task_id"]: task for task in tasks}
    return [mapping[task_id] for task_id in order]


def _verify_trace(
    log: CheckLog,
    mode: str,
    execution_payload: Mapping[str, Any],
    split_plan: Mapping[str, Any],
    settings: Mapping[str, Any],
) -> ExecutionResult | None:
    trace = execution_payload.get("trace", [])
    stats = execution_payload.get("stats", {})
    prefix = f"{mode}."
    if not isinstance(trace, list) or not trace:
        log.add(prefix + "trace_present", False, "trace is empty or not an array")
        return None
    expected = _expected_tasks(split_plan)
    expected_ids = [task["task_id"] for task in expected]
    actual_ids = [str(task.get("task_id")) for task in trace]
    log.add(prefix + "trace_task_order", actual_ids == expected_ids, {"expected": expected_ids, "actual": actual_ids})
    expected_map = {task["task_id"]: task for task in expected}
    ranges_ok = True
    rows_ok = True
    clock_ok = True
    admission_ok = True
    for task in trace:
        task_id = str(task.get("task_id"))
        reference = expected_map.get(task_id)
        if reference is None:
            ranges_ok = False
            continue
        start, end = task.get("input_range", [-1, -1])
        if [int(start), int(end)] != [int(item) for item in reference["input_range"]]:
            ranges_ok = False
        if int(task.get("input_rows", -1)) != int(reference["input_rows"]):
            rows_ok = False
        if int(task.get("completed_rows", -1)) != int(task.get("output_rows", -2)):
            rows_ok = False
        start_ms = task.get("start_ms")
        end_ms = task.get("end_ms")
        if not isinstance(start_ms, (int, float)) or not isinstance(end_ms, (int, float)):
            clock_ok = False
        elif start_ms < 0 or end_ms < start_ms:
            clock_ok = False
        queue_wait = task.get("budget_queue_wait_ms")
        admitted = task.get("admitted_monotonic_ms")
        if (
            not isinstance(queue_wait, (int, float))
            or queue_wait < 0
            or not isinstance(admitted, (int, float))
            or admitted > float(start_ms) + 0.001
        ):
            admission_ok = False
    log.add(prefix + "trace_ranges_match_split", ranges_ok)
    log.add(prefix + "trace_completed_rows_match", rows_ok)
    log.add(prefix + "trace_monotonic_milliseconds", clock_ok)
    log.add(prefix + "trace_admission_order", admission_ok)

    budget = int(settings.get("memory_budget_bytes", 0))
    parallelism = int(settings.get("parallelism", 0))
    memory_ok = True
    max_memory = 0
    task_memory_ok = True
    events: list[tuple[float, int, int, str]] = []
    for task in trace:
        memory = int(task.get("peak_memory_bytes", -1))
        if memory < 0 or memory > budget:
            task_memory_ok = False
        max_memory = max(max_memory, memory)
        start = float(task.get("start_ms", 0))
        end = float(task.get("end_ms", 0))
        events.append((start, 1, memory, str(task.get("task_id"))))
        events.append((end, 0, -memory, str(task.get("task_id"))))
    events.sort(key=lambda item: (item[0], item[1]))
    active_memory = 0
    active_tasks = 0
    max_active_tasks = 0
    peak_memory = 0
    for _time, kind, delta, _task_id in events:
        active_memory += delta
        active_tasks += 1 if kind == 1 else -1
        peak_memory = max(peak_memory, active_memory)
        max_active_tasks = max(max_active_tasks, active_tasks)
        if active_memory > budget or active_tasks > parallelism:
            memory_ok = False
    log.add(prefix + "task_memory_within_budget", task_memory_ok, {"peak_task_memory_bytes": max_memory})
    log.add(
        prefix + "sweep_memory_and_parallelism",
        memory_ok,
        {
            "peak_active_memory_bytes": peak_memory,
            "budget_bytes": budget,
            "max_active_tasks": max_active_tasks,
            "parallelism_limit": parallelism,
        },
    )
    reserved_ok = all(int(task.get("scheduler_peak_reserved_memory_bytes", 0)) <= budget for task in trace)
    log.add(prefix + "scheduler_reservation_within_budget", reserved_ok)

    trace_input = sum(int(task.get("input_rows", 0)) for task in trace)
    trace_completed = sum(int(task.get("completed_rows", 0)) for task in trace)
    log.add(
        prefix + "collection_stats_match_trace",
        int(stats.get("scan_tasks", -1)) == len(trace)
        and int(stats.get("scanned_rows", -2)) == trace_input
        and int(stats.get("scan_output_rows", -3)) == trace_completed
        and int(stats.get("task_trace_rows", -4)) == trace_input
        and int(stats.get("task_completed_rows", -5)) == trace_completed,
        {
            "tasks": len(trace),
            "trace_input_rows": trace_input,
            "trace_completed_rows": trace_completed,
        },
    )
    log.add(prefix + "trace_checksum", str(stats.get("trace_sha256")) == sha256_json(trace))
    return None


def _execution_bytes(execution_payload: Mapping[str, Any]) -> bytes:
    result = execution_payload.get("result", {})
    encoded = result.get("canonical_bytes_base64")
    if not isinstance(encoded, str):
        raise PipeError(E_DIVERGE, "snapshot execution is missing canonical result bytes", "$.executions")
    return base64.b64decode(encoded.encode("ascii"), validate=True)


def verify_snapshot(
    snapshot_path: str | Path,
    *,
    output_path: str | Path | None = None,
    artifacts_dir: str | Path | None = None,
) -> dict[str, Any]:
    log = CheckLog()
    failure_code: str | None = None
    snapshot: dict[str, Any] = {}
    recomputed: dict[str, ExecutionResult] = {}
    try:
        snapshot = load_snapshot(snapshot_path)
        log.add("snapshot_checksum", True)
        log.add("snapshot_format", snapshot.get("format") == SNAPSHOT_FORMAT)
        log.add(
            "snapshot_format_version",
            snapshot.get("format_version") == SNAPSHOT_FORMAT_VERSION,
            {"actual": snapshot.get("format_version")},
        )
        request = _request_from_snapshot(snapshot)
        log.add("embedded_dataset_reconstructs", True)
        log.add(
            "embedded_data_digest",
            request.dataset.data_digest == snapshot.get("data_digest"),
        )
        log.add(
            "embedded_schema_fingerprint",
            request.dataset.schema_fingerprint == snapshot.get("schema_fingerprint"),
        )
        rebuilt_statistics = build_statistics(request.dataset)
        stored_statistics = snapshot.get("statistics", {})
        statistics_ok = (
            isinstance(stored_statistics, dict)
            and stored_statistics.get("statistics_sha256") == statistics_core_sha256(rebuilt_statistics)
        )
        log.add("statistics_reproducible", statistics_ok)
        bundle = build_plans(request)
        plans = snapshot.get("plans", {})
        log.add(
            "query_summary_stable",
            snapshot.get("query_summary_sha256") == request.query.summary_sha256,
        )
        optimized_plan_ok = (
            isinstance(plans, dict)
            and plans.get("optimized", {}).get("plan") == bundle.optimized_plan
            and plans.get("optimized", {}).get("summary_sha256") == bundle.optimized_summary_sha256
        )
        baseline_plan_ok = (
            isinstance(plans, dict)
            and plans.get("baseline", {}).get("plan") == bundle.baseline_plan
            and plans.get("baseline", {}).get("summary_sha256") == bundle.baseline_summary_sha256
        )
        log.add("optimized_plan_reproducible", optimized_plan_ok)
        log.add("baseline_plan_reproducible", baseline_plan_ok)
        log.add(
            "pushdown_pruning_reproducible",
            plans.get("pushdown") == bundle.pushdown and plans.get("pruning") == bundle.pruning,
        )
        log.add(
            "split_plan_reproducible",
            plans.get("split_plan") == bundle.split_plan
            and plans.get("baseline_split_plan") == bundle.baseline_split_plan,
        )
        index_ok = True
        for index, item in enumerate(snapshot.get("index_set", [])):
            try:
                table = request.dataset.table(str(item.get("table")))
                for column in item.get("columns", []):
                    table.column(str(column))
            except (KeyError, AttributeError):
                index_ok = False
                break
        log.add("index_set_rebuildable", index_ok)

        executions = snapshot.get("executions", {})
        if not isinstance(executions, dict) or set(executions) != {"optimized", "baseline"}:
            log.add("execution_records_present", False)
            raise PipeError(E_VERSION, "snapshot must contain optimized and baseline executions", "$.executions")
        log.add("execution_records_present", True)
        for mode, split_key in (("optimized", "split_plan"), ("baseline", "baseline_split_plan")):
            payload = executions[mode]
            embedded_bytes = _execution_bytes(payload)
            embedded_result = payload.get("result", {})
            embedded_hash = sha256_bytes(embedded_bytes)
            log.add(
                f"{mode}.embedded_bytes_hash",
                embedded_hash == embedded_result.get("canonical_sha256"),
                {"sha256": embedded_hash},
            )
            recalculated_bytes = serialize_result(
                list(embedded_result.get("columns", [])),
                [list(row) for row in embedded_result.get("rows", [])],
            )
            log.add(f"{mode}.embedded_rows_match_bytes", recalculated_bytes == embedded_bytes)
            _verify_trace(log, mode, payload, plans.get(split_key, {}), request.settings.to_dict())
            recomputed[mode] = execute(bundle, mode=mode)
            log.add(
                f"{mode}.reexecution_bytes_equal_snapshot",
                recomputed[mode].canonical_bytes == embedded_bytes,
                {
                    "snapshot_sha256": embedded_hash,
                    "recomputed_sha256": recomputed[mode].canonical_sha256,
                },
            )
        comparison = compare_results(recomputed["optimized"], recomputed["baseline"])
        log.add(
            "optimized_baseline_byte_equal",
            comparison.get("byte_equal") is True,
            {
                "optimized_sha256": recomputed["optimized"].canonical_sha256,
                "baseline_sha256": recomputed["baseline"].canonical_sha256,
            },
        )
        log.add("statistics_consistent", comparison.get("statistics_consistent", {}).get("ok") is True)
        log.add(
            "snapshot_comparison_consistent",
            snapshot.get("comparison", {}).get("byte_equal") is True,
        )
        history = snapshot.get("plan_history", [])
        history_ok = isinstance(history, list) and len(history) >= 2
        if history_ok:
            by_mode = {item.get("mode"): item for item in history if isinstance(item, dict)}
            history_ok = by_mode.get("optimized", {}).get("result_sha256") == recomputed[
                "optimized"
            ].canonical_sha256 and by_mode.get("baseline", {}).get("result_sha256") == recomputed[
                "baseline"
            ].canonical_sha256
        log.add("plan_history_consistent", history_ok)

        if artifacts_dir is not None:
            root = Path(artifacts_dir)
            checks: list[dict[str, Any]] = []
            for mode in ("optimized", "baseline"):
                for filename in (f"{mode}_result.json", f"{mode}_trace.json", f"{mode}.bin"):
                    checks.append({"mode": mode, "file": filename, "exists": (root / filename).exists()})
            # Files are optional for verification; when result files are present,
            # their embedded hash must agree with the snapshot.
            result_files_ok = True
            binary_files_ok = True
            trace_files_ok = True
            for mode in ("optimized", "baseline"):
                path = root / f"{mode}_result.json"
                if path.exists():
                    import json

                    try:
                        artifact = json.loads(path.read_text(encoding="utf-8"))
                        result = artifact.get("result", artifact)
                        if result.get("canonical_sha256") != recomputed[mode].canonical_sha256:
                            result_files_ok = False
                    except Exception:
                        result_files_ok = False
                binary_path = root / f"{mode}.bin"
                if binary_path.exists():
                    try:
                        if binary_path.read_bytes() != recomputed[mode].canonical_bytes:
                            binary_files_ok = False
                    except OSError:
                        binary_files_ok = False
                trace_path = root / f"{mode}_trace.json"
                if trace_path.exists():
                    import json

                    try:
                        trace_artifact = json.loads(trace_path.read_text(encoding="utf-8"))
                        if trace_artifact.get("trace") != executions[mode].get("trace"):
                            trace_files_ok = False
                    except Exception:
                        trace_files_ok = False
            log.add("artifact_result_files_consistent", result_files_ok, checks)
            log.add("artifact_binary_files_consistent", binary_files_ok)
            log.add("artifact_trace_files_consistent", trace_files_ok)
    except PipeError as exc:
        failure_code = exc.code
        log.add("verification_exception", False, exc.to_dict())
    except Exception as exc:  # verifier must always emit a structured verdict
        failure_code = E_INTERNAL
        log.add("verification_exception", False, {"code": E_INTERNAL, "message": str(exc)})

    status = "PASS" if log.ok else "FAIL"
    if failure_code is None and not log.ok:
        failure_code = E_DIVERGE
    result = {
        "status": status,
        "code": "OK" if status == "PASS" else failure_code,
        "snapshot_path": str(snapshot_path),
        "snapshot_id": snapshot.get("snapshot_id"),
        "checks": log.checks,
        "summary": {
            "session_id": snapshot.get("session_id"),
            "data_version": snapshot.get("data_version"),
            "optimized_sha256": recomputed.get("optimized").canonical_sha256
            if "optimized" in recomputed
            else None,
            "baseline_sha256": recomputed.get("baseline").canonical_sha256
            if "baseline" in recomputed
            else None,
            "checks_passed": sum(1 for item in log.checks if item["status"] == "PASS"),
            "checks_failed": sum(1 for item in log.checks if item["status"] == "FAIL"),
        },
    }
    if output_path is not None:
        write_json(output_path, result)
    return result
