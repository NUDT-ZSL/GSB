"""Deterministic local demonstration and report construction."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from .errors import (
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    STAGE_BLOCK_STATS,
    STAGE_INPUT,
    STAGE_NORMALIZATION,
    STAGE_WRITEBACK,
    STAGES,
)
from .models import (
    ExecutionOptions,
    FaultParameters,
    OperatorResult,
    OperatorTask,
    ResourceLimits,
    ToleranceConfig,
)
from .operators import execute_job
from .snapshot import Snapshot, SnapshotStore
from .util import (
    array_bytes,
    atomic_write_bytes,
    canonical_json_bytes,
    file_sha256,
    npy_bytes,
    pretty_json_bytes,
    sha256_bytes,
    sha256_json,
)


@dataclass(frozen=True)
class DemoCase:
    case_id: str
    task: OperatorTask
    tensor: np.ndarray
    options: ExecutionOptions
    role: str
    expected_stage: str | None = None


@dataclass
class CaseExecution:
    case: DemoCase
    result: OperatorResult
    npy_data: bytes | None


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value)
    return cleaned.strip("._") or "job"


def load_tasks(data_dir: str | Path) -> list[OperatorTask]:
    tasks_path = Path(data_dir) / "tasks.jsonl"
    tasks: list[OperatorTask] = []
    with open(tasks_path, "r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            import json

            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"tasks.jsonl line {line_number} is not an object")
            tasks.append(OperatorTask.from_mapping(value))
    return tasks


def load_tensor(data_dir: str | Path, task: OperatorTask) -> np.ndarray:
    path = Path(data_dir) / "tensors" / f"{task.job_id}.npy"
    return np.load(path, allow_pickle=False)


def _clone_task(task: OperatorTask, suffix: str, **updates: Any) -> OperatorTask:
    raw = copy.deepcopy(dict(task.raw))
    raw.update(updates)
    raw["job_id"] = f"{task.job_id}::{suffix}"
    return OperatorTask.from_mapping(raw)


def build_demo_cases(
    data_dir: str | Path,
    options: ExecutionOptions,
) -> tuple[list[DemoCase], list[dict[str, Any]]]:
    tasks = load_tasks(data_dir)
    if not tasks:
        raise ValueError("sample task list is empty")
    tensors = {task.job_id: load_tensor(data_dir, task) for task in tasks}
    cases: list[DemoCase] = [
        DemoCase(
            case_id=task.job_id,
            task=task,
            tensor=tensors[task.job_id],
            options=options,
            role="task",
        )
        for task in tasks
    ]
    task_manifest = [
        {
            "job_id": task.job_id,
            "op_kind": task.op_kind,
            "tensor_sha256": sha256_bytes(array_bytes(tensors[task.job_id])),
        }
        for task in tasks
    ]

    softmax_task = next(task for task in tasks if task.op_kind == "softmax")
    layernorm_task = next(task for task in tasks if task.op_kind == "layernorm")
    softmax_tensor = tensors[softmax_task.job_id]
    layernorm_tensor = tensors[layernorm_task.job_id]

    cases.extend(
        [
            DemoCase(
                case_id="inject::input_validation",
                task=_clone_task(layernorm_task, "inj_input_validation", eps=-1.0),
                tensor=layernorm_tensor,
                options=options,
                role="injection",
                expected_stage=STAGE_INPUT,
            ),
            DemoCase(
                case_id="inject::block_statistics",
                task=_clone_task(softmax_task, "inj_block_statistics", block_size=0),
                tensor=softmax_tensor,
                options=options,
                role="injection",
                expected_stage=STAGE_BLOCK_STATS,
            ),
            DemoCase(
                case_id="inject::normalization_accumulation",
                task=_clone_task(layernorm_task, "inj_normalization_accumulation"),
                tensor=layernorm_tensor,
                options=ExecutionOptions(
                    tolerance=options.tolerance,
                    limits=options.limits,
                    eps_floor=options.eps_floor,
                    faults=FaultParameters(
                        stat_overflow_scale=1e308,
                        write_index_offset=options.faults.write_index_offset,
                    ),
                ),
                role="injection",
                expected_stage=STAGE_NORMALIZATION,
            ),
            DemoCase(
                case_id="inject::output_writeback",
                task=_clone_task(softmax_task, "inj_output_writeback"),
                tensor=softmax_tensor,
                options=ExecutionOptions(
                    tolerance=options.tolerance,
                    limits=options.limits,
                    eps_floor=options.eps_floor,
                    faults=FaultParameters(
                        stat_overflow_scale=options.faults.stat_overflow_scale,
                        write_index_offset=1_000_000_000,
                    ),
                ),
                role="injection",
                expected_stage=STAGE_WRITEBACK,
            ),
        ]
    )

    cases.extend(
        [
            DemoCase(
                case_id="probe::op_unknown",
                task=_clone_task(softmax_task, "probe_op_unknown", op_kind="mystery_op"),
                tensor=softmax_tensor,
                options=options,
                role="validation_probe",
            ),
            DemoCase(
                case_id="probe::axis_invalid",
                task=_clone_task(softmax_task, "probe_axis_invalid", axis=99),
                tensor=softmax_tensor,
                options=options,
                role="validation_probe",
            ),
            DemoCase(
                case_id="probe::shape_invalid",
                task=_clone_task(
                    softmax_task,
                    "probe_shape_invalid",
                    input_shape=[999, int(softmax_tensor.shape[1])],
                ),
                tensor=softmax_tensor,
                options=options,
                role="validation_probe",
            ),
            DemoCase(
                case_id="probe::dtype_unsupported",
                task=_clone_task(
                    softmax_task, "probe_dtype_unsupported", dtype="float16"
                ),
                tensor=softmax_tensor,
                options=options,
                role="validation_probe",
            ),
        ]
    )

    shrink_limits = ResourceLimits(
        max_blocks=options.limits.max_blocks,
        max_block_elements=3,
        max_stat_bytes=options.limits.max_stat_bytes,
        allow_block_shrink=True,
        allow_float32_stats=options.limits.allow_float32_stats,
    )
    hard_limit = ResourceLimits(
        max_blocks=1,
        max_block_elements=options.limits.max_block_elements,
        max_stat_bytes=options.limits.max_stat_bytes,
        allow_block_shrink=options.limits.allow_block_shrink,
        allow_float32_stats=options.limits.allow_float32_stats,
    )
    float32_stats = ResourceLimits(
        max_blocks=options.limits.max_blocks,
        max_block_elements=options.limits.max_block_elements,
        max_stat_bytes=400,
        allow_block_shrink=options.limits.allow_block_shrink,
        allow_float32_stats=True,
    )
    cases.extend(
        [
            DemoCase(
                case_id="resource::block_shrink",
                task=_clone_task(softmax_task, "resource_block_shrink"),
                tensor=softmax_tensor,
                options=ExecutionOptions(
                    tolerance=options.tolerance,
                    limits=shrink_limits,
                    eps_floor=options.eps_floor,
                    faults=FaultParameters(),
                ),
                role="resource_check",
            ),
            DemoCase(
                case_id="resource::max_blocks",
                task=_clone_task(softmax_task, "resource_max_blocks"),
                tensor=softmax_tensor,
                options=ExecutionOptions(
                    tolerance=options.tolerance,
                    limits=hard_limit,
                    eps_floor=options.eps_floor,
                    faults=FaultParameters(),
                ),
                role="resource_check",
            ),
            DemoCase(
                case_id="resource::float32_stats",
                task=_clone_task(layernorm_task, "resource_float32_stats"),
                tensor=layernorm_tensor,
                options=ExecutionOptions(
                    tolerance=options.tolerance,
                    limits=float32_stats,
                    eps_floor=options.eps_floor,
                    faults=FaultParameters(),
                ),
                role="resource_check",
            ),
        ]
    )
    return cases, task_manifest


def run_cases(cases: Iterable[DemoCase]) -> list[CaseExecution]:
    executions: list[CaseExecution] = []
    for case in cases:
        result = execute_job(case.task, case.tensor, case.options)
        npy_data = npy_bytes(result.output) if result.output is not None else None
        executions.append(
            CaseExecution(case=case, result=result, npy_data=npy_data)
        )
    return executions


def _block_statistics_snapshot(result: OperatorResult) -> dict[str, Any]:
    return {
        "job_id": result.job_id,
        "op_kind": result.op_kind,
        "status": result.status,
        "plan_sha256": sha256_json(result.plan),
        "blocks": [
            {
                "block_id": block.block_id,
                "axis_start": block.axis_start,
                "axis_end": block.axis_end,
                "statistics": block.statistics,
            }
            for block in result.blocks
        ],
    }


def result_to_report(
    execution: CaseExecution,
    snapshot_query: dict[str, Any] | None,
) -> dict[str, Any]:
    result = execution.result
    output = result.output
    report: dict[str, Any] = {
        "case_id": execution.case.case_id,
        "role": execution.case.role,
        "job_id": result.job_id,
        "op_kind": result.op_kind,
        "status": result.status,
        "business_codes": result.business_codes(),
        "task": copy.deepcopy(dict(result.task.raw)),
        "compute_dtype": result.compute_dtype,
        "plan": result.plan,
        "block_statistics": [block.to_dict() for block in result.blocks],
        "deviation_spectrum": result.tolerance_report,
        "diagnostics": [item.to_dict() for item in result.diagnostics],
        "degradations": copy.deepcopy(result.degradations),
        "unattributed_errors": copy.deepcopy(result.unattributed_errors),
        "self_check": result.self_check,
        "snapshot": snapshot_query,
    }
    if output is not None:
        report["output"] = {
            "shape": list(output.shape),
            "dtype": output.dtype.name,
            "array_sha256": sha256_bytes(array_bytes(output)),
            "npy_sha256": sha256_bytes(execution.npy_data) if execution.npy_data else None,
            "reference_sha256": (
                sha256_bytes(array_bytes(result.reference))
                if result.reference is not None
                else None
            ),
        }
    else:
        report["output"] = None
    return report


def _snapshot_behavior_checks(options: ExecutionOptions) -> dict[str, Any]:
    store = SnapshotStore()
    content_v1 = {
        "abs_tol": options.tolerance.abs_tol,
        "rel_tol": options.tolerance.rel_tol,
        "previous_only_key": "must_disappear_after_whole_replace",
    }
    operation_v1 = store.publish("tolerance_config", content_v1)
    content_v2 = {
        "abs_tol": options.tolerance.abs_tol,
        "rel_tol": options.tolerance.rel_tol,
        "replacement": "whole-object",
    }
    operation_v2 = store.publish("tolerance_config", content_v2)
    old_snapshot = operation_v1.snapshot
    rollback = (
        store.commit(old_snapshot)
        if old_snapshot is not None
        else None
    )
    corrupt_snapshot = Snapshot.create(
        "corruption_probe", 1, {"value": 1}, checksum="0" * 64
    )
    corrupt = store.commit(corrupt_snapshot)
    query_v2 = store.query("tolerance_config")
    return {
        "whole_replacement": {
            "v1": operation_v1.to_dict(include_content=True),
            "v2": operation_v2.to_dict(include_content=True),
            "v2_query": query_v2,
            "old_key_removed": (
                query_v2.get("ok", False)
                and "previous_only_key" not in query_v2.get("content", {})
            ),
        },
        "rollback_attempt": (
            rollback.to_dict(include_content=False) if rollback is not None else None
        ),
        "corruption_attempt": corrupt.to_dict(include_content=False),
        "expected_codes": {
            "rollback": E_SNAPSHOT_ROLLBACK,
            "corruption": E_SNAPSHOT_CORRUPT,
        },
    }


def build_snapshot_report(
    executions: list[CaseExecution],
    options: ExecutionOptions,
) -> tuple[SnapshotStore, dict[str, Any], dict[str, dict[str, Any]]]:
    store = SnapshotStore()
    initial_plans = {
        "phase": "requested",
        "jobs": [
            {
                "job_id": execution.result.job_id,
                "op_kind": execution.result.op_kind,
                "requested_block_size": execution.result.task.block_size,
                "axis": execution.result.task.axis,
            }
            for execution in executions
        ],
    }
    plan_v1 = store.publish("block_plan", initial_plans)
    tolerance_v1 = store.publish("tolerance_config", options.tolerance.to_dict())
    if not plan_v1.ok or not tolerance_v1.ok:
        raise RuntimeError("failed to initialize deterministic snapshots")

    actual_plans = {
        "phase": "executed",
        "jobs": [
            {
                "job_id": execution.result.job_id,
                "op_kind": execution.result.op_kind,
                "plan": execution.result.plan,
            }
            for execution in executions
        ],
    }
    plan_v2 = store.publish("block_plan", actual_plans)
    tolerance_v2 = store.publish("tolerance_config", options.tolerance.to_dict())
    if not plan_v2.ok or not tolerance_v2.ok:
        raise RuntimeError("failed to replace final snapshots")

    queries: dict[str, dict[str, Any]] = {}
    for execution in executions:
        category = f"job_statistics:{execution.result.job_id}"
        operation = store.publish(
            category, _block_statistics_snapshot(execution.result)
        )
        if not operation.ok:
            raise RuntimeError(f"failed to publish statistics snapshot: {category}")
        queries[execution.result.job_id] = store.query(category)

    report = {
        "store": store.to_dict(),
        "final_queries": {
            "block_plan": store.query("block_plan"),
            "tolerance_config": store.query("tolerance_config"),
        },
        "replacement_checks": _snapshot_behavior_checks(options),
    }
    return store, report, queries


def _case_hashes(executions: list[CaseExecution]) -> str:
    payload = []
    for execution in executions:
        result = execution.result
        payload.append(
            {
                "case_id": execution.case.case_id,
                "status": result.status,
                "business_codes": result.business_codes(),
                "plan": result.plan,
                "blocks": [block.to_dict() for block in result.blocks],
                "spectrum": result.tolerance_report,
                "diagnostics": [item.to_dict() for item in result.diagnostics],
                "degradations": result.degradations,
                "self_check": result.self_check,
                "unattributed_errors": result.unattributed_errors,
                "output_sha256": (
                    sha256_bytes(array_bytes(result.output))
                    if result.output is not None
                    else None
                ),
                "reference_sha256": (
                    sha256_bytes(array_bytes(result.reference))
                    if result.reference is not None
                    else None
                ),
            }
        )
    return sha256_json(payload)


def _exact_rerun_match(
    first: list[CaseExecution], second: list[CaseExecution]
) -> bool:
    if len(first) != len(second):
        return False
    for left, right in zip(first, second):
        if _case_hashes([left]) != _case_hashes([right]):
            return False
        if left.npy_data != right.npy_data:
            return False
        if left.result.output is None and right.result.output is None:
            continue
        if left.result.output is None or right.result.output is None:
            return False
        if array_bytes(left.result.output) != array_bytes(right.result.output):
            return False
        if left.result.reference is None or right.result.reference is None:
            return False
        if array_bytes(left.result.reference) != array_bytes(right.result.reference):
            return False
    return True


def build_demo_report(
    data_dir: str | Path,
    output_dir: str | Path,
    options: ExecutionOptions | None = None,
) -> dict[str, Any]:
    options = options if options is not None else ExecutionOptions()
    data_path = Path(data_dir)
    output_path = Path(output_dir)
    cases, task_manifest = build_demo_cases(data_path, options)
    first = run_cases(cases)
    second = run_cases(cases)

    store, snapshot_report, snapshot_queries = build_snapshot_report(first, options)
    job_reports = [
        result_to_report(
            execution,
            snapshot_queries.get(execution.result.job_id),
        )
        for execution in first
    ]

    injection_records: list[dict[str, Any]] = []
    injection_cases: list[dict[str, Any]] = []
    for execution in first:
        if execution.case.role != "injection":
            continue
        targeted = [
            item.to_dict()
            for item in execution.result.diagnostics
            if item.stage == execution.case.expected_stage
        ]
        injection_records.extend(targeted)
        injection_cases.append(
            {
                "case_id": execution.case.case_id,
                "job_id": execution.result.job_id,
                "expected_stage": execution.case.expected_stage,
                "stages_seen": sorted(
                    {item.stage for item in execution.result.diagnostics}
                ),
                "status": execution.result.status,
                "business_codes": execution.result.business_codes(),
                "degradations": copy.deepcopy(
                    execution.result.degradations
                ),
                "self_check": execution.result.self_check,
                "snapshot": snapshot_queries.get(execution.result.job_id),
            }
        )

    validation_probes = [
        result_to_report(
            execution,
            snapshot_queries.get(execution.result.job_id),
        )
        for execution in first
        if execution.case.role == "validation_probe"
    ]
    resource_checks = [
        result_to_report(
            execution,
            snapshot_queries.get(execution.result.job_id),
        )
        for execution in first
        if execution.case.role == "resource_check"
    ]

    first_hash = _case_hashes(first)
    second_hash = _case_hashes(second)
    rerun_match = _exact_rerun_match(first, second)
    four_stages = sorted(
        {record["stage"] for record in injection_records}
    )
    all_unattributed = [
        error
        for execution in first
        for error in execution.result.unattributed_errors
    ]
    output_selfchecks = [
        execution.result.self_check.get("status")
        for execution in first
        if execution.result.output is not None
    ]
    snapshot_checks = snapshot_report["replacement_checks"]
    snapshot_expected_ok = (
        snapshot_checks["whole_replacement"]["old_key_removed"]
        and snapshot_checks["rollback_attempt"]["code"] == E_SNAPSHOT_ROLLBACK
        and snapshot_checks["corruption_attempt"]["code"] == E_SNAPSHOT_CORRUPT
    )

    normal_jobs_ok = all(
        execution.result.status == "ok"
        for execution in first
        if execution.case.role == "task"
    )
    validation_probe_codes = {
        item["business_codes"][0]
        for item in validation_probes
        if item["business_codes"]
    }
    expected_validation_codes = {
        "E_OP_UNKNOWN",
        "E_AXIS_INVALID",
        "E_SHAPE_INVALID",
        "E_DTYPE_UNSUPPORTED",
    }
    verification = {
        "normal_jobs_all_ok": normal_jobs_ok,
        "four_injection_stages_seen": four_stages,
        "four_injection_stages_complete": set(four_stages) == set(STAGES),
        "validation_codes_seen": sorted(validation_probe_codes),
        "validation_codes_complete": validation_probe_codes
        == expected_validation_codes,
        "all_output_selfchecks_passed": bool(output_selfchecks)
        and all(status == "passed" for status in output_selfchecks),
        "unattributed_error_count": len(all_unattributed),
        "no_unattributed_errors": not all_unattributed,
        "rerun_byte_exact": rerun_match,
        "snapshot_behaviors_ok": snapshot_expected_ok,
    }
    verification["passed"] = all(
        [
            verification["normal_jobs_all_ok"],
            verification["four_injection_stages_complete"],
            verification["validation_codes_complete"],
            verification["all_output_selfchecks_passed"],
            verification["no_unattributed_errors"],
            verification["rerun_byte_exact"],
            verification["snapshot_behaviors_ok"],
        ]
    )

    tasks_sha = file_sha256(data_path / "tasks.jsonl")
    report_core: dict[str, Any] = {
        "schema_version": "1.0",
        "config": options.to_dict(),
        "samples": {
            "tasks_sha256": tasks_sha,
            "task_count": len(task_manifest),
            "tensors": task_manifest,
        },
        "jobs": job_reports,
        "injections": {
            "records": injection_records,
            "cases": injection_cases,
        },
        "validation_probes": validation_probes,
        "resource_checks": resource_checks,
        "snapshots": snapshot_report,
        "rerun_check": {
            "first_run_sha256": first_hash,
            "second_run_sha256": second_hash,
            "byte_exact_match": rerun_match,
        },
        "verification": verification,
    }
    report_core["report_core_sha256"] = sha256_json(report_core)

    output_path.mkdir(parents=True, exist_ok=True)
    outputs_path = output_path / "outputs"
    outputs_path.mkdir(parents=True, exist_ok=True)
    for execution in first:
        if execution.npy_data is None:
            continue
        filename = f"{safe_filename(execution.result.job_id)}.npy"
        atomic_write_bytes(outputs_path / filename, execution.npy_data)
    store.write(output_path / "snapshots.json")
    report_bytes = pretty_json_bytes(report_core)
    atomic_write_bytes(output_path / "demo_report.json", report_bytes)
    atomic_write_bytes(
        output_path / "demo_report.sha256",
        (sha256_bytes(report_bytes) + "\n").encode("ascii"),
    )
    return report_core
