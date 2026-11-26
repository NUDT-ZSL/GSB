from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .canonical import content_digest
from .errors import E_BUDGET_EXCEEDED, E_FORMAT_CORRUPT, E_PRECISION_TRADE, TuningError
from .estimator import batch_interference, batch_memory_mb, estimate_candidate
from .ids import point_id, quant_digest
from .workload import compute_quant_report, normalize_shape


REQUIRED_TASK_FIELDS = (
    "tuning_id",
    "op_kind",
    "workload_shape",
    "quant_config",
    "candidate_schedules",
    "target_latency_us",
    "priority",
    "logical_clock_ms",
)

DEFAULT_POLICY: dict[str, Any] = {
    "worker_count": 2,
    "max_concurrency": 2,
    "logical_clock_budget_ms": 10_000.0,
    "memory_budget_mb": 128.0,
    "max_interference_factor": 1.35,
    "max_dequant_ops": 200_000_000,
    "max_relative_rmse": 0.08,
    "precision_upgrade_allowed": True,
    "high_precision_bits": 16,
    "timing_repeats": 5,
    "warmup_repeats": 1,
    "evaluation_overhead_ms": 0.05,
    "enforce_target_latency": False,
    "priority_order": ["progress", "memory", "precision"],
}


def load_policy(path: str | Path | None) -> dict[str, Any]:
    policy = dict(DEFAULT_POLICY)
    if path is None:
        return policy
    path = Path(path)
    if not path.exists():
        raise TuningError(E_FORMAT_CORRUPT, f"policy/config not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        user_policy = json.load(handle)
    if "arbitration" in user_policy and isinstance(user_policy["arbitration"], dict):
        user_policy = {**user_policy, **user_policy["arbitration"]}
    policy.update(user_policy)
    order = policy.get("priority_order")
    if sorted(order) != ["memory", "precision", "progress"] or len(order) != 3:
        raise TuningError(
            E_FORMAT_CORRUPT,
            "priority_order must be a permutation of progress, memory, precision",
        )
    policy["worker_count"] = max(1, int(policy["worker_count"]))
    policy["max_concurrency"] = max(1, int(policy["max_concurrency"]))
    policy["timing_repeats"] = max(1, int(policy["timing_repeats"]))
    return policy


def load_tasks(path: str | Path) -> list[dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        raise TuningError(E_FORMAT_CORRUPT, f"task JSONL not found: {path}")
    tasks: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                task = json.loads(line)
            except json.JSONDecodeError as exc:
                raise TuningError(
                    E_FORMAT_CORRUPT, f"invalid JSONL at line {line_number}: {exc}"
                ) from exc
            missing = [field for field in REQUIRED_TASK_FIELDS if field not in task]
            if missing:
                raise TuningError(
                    E_FORMAT_CORRUPT,
                    f"task line {line_number} missing fields: {','.join(missing)}",
                )
            if task["op_kind"] not in ("matmul", "conv2d"):
                raise TuningError(E_FORMAT_CORRUPT, f"unsupported op_kind in {task['tuning_id']}")
            normalize_shape(task["op_kind"], task["workload_shape"])
            if not isinstance(task["candidate_schedules"], list) or not task["candidate_schedules"]:
                raise TuningError(E_FORMAT_CORRUPT, "candidate_schedules must be a non-empty list")
            for schedule in task["candidate_schedules"]:
                if "schedule_id" not in schedule:
                    raise TuningError(E_FORMAT_CORRUPT, "schedule missing schedule_id")
                schedule.setdefault("schedule_revision", 1)
            tasks.append(task)
    return tasks


def ordered_points(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sorted_tasks = sorted(
        tasks,
        key=lambda task: (
            int(task["priority"]),
            int(task["logical_clock_ms"]),
            str(task["tuning_id"]),
        ),
    )
    points: list[dict[str, Any]] = []
    sequence = 0
    for task in sorted_tasks:
        q_digest = quant_digest(task["quant_config"])
        schedules = sorted(
            task["candidate_schedules"],
            key=lambda schedule: (
                str(schedule["schedule_id"]),
                int(schedule.get("schedule_revision", 1)),
                content_digest(schedule),
            ),
        )
        for schedule in schedules:
            sequence += 1
            points.append(
                {
                    "point_id": point_id(task["tuning_id"], schedule, q_digest),
                    "sequence_no": sequence,
                    "tuning_id": task["tuning_id"],
                    "op_kind": task["op_kind"],
                    "workload_shape": task["workload_shape"],
                    "quant_config": task["quant_config"],
                    "quant_digest": q_digest,
                    "schedule": schedule,
                    "target_latency_us": float(task["target_latency_us"]),
                    "priority": int(task["priority"]),
                    "logical_clock_ms": int(task["logical_clock_ms"]),
                    "task": task,
                }
            )
    return points


def _precision_choices(
    point: dict[str, Any], platform: dict[str, Any], policy: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    base_bits = int(point["quant_config"].get("bits", 8))
    high_bits = int(policy.get("high_precision_bits", 16))
    bits_options = [base_bits]
    if policy.get("precision_upgrade_allowed", True) and high_bits != base_bits:
        bits_options.append(high_bits)

    choices: list[dict[str, Any]] = []
    hard_error: dict[str, Any] | None = None
    for bits in bits_options:
        report = compute_quant_report(
            point["tuning_id"],
            point["op_kind"],
            point["workload_shape"],
            point["quant_config"],
            bits,
        )
        estimate = estimate_candidate(
            point["op_kind"], point["workload_shape"], point["schedule"], platform, bits
        )
        meets_precision = (
            report["metrics"]["relative_rmse"] <= float(policy["max_relative_rmse"])
        )
        budget_ok = (
            estimate["resident_memory_mb"] <= float(policy["memory_budget_mb"])
            and estimate["dequant_ops"] <= int(policy["max_dequant_ops"])
            and estimate["interference_factor"] <= float(policy["max_interference_factor"])
        )
        target_ok = True
        if policy.get("enforce_target_latency", False):
            target_ok = estimate["expected_latency_us"] <= point["target_latency_us"]
        choices.append(
            {
                "bits": bits,
                "report": report,
                "estimate": estimate,
                "meets_precision": meets_precision,
                "budget_ok": budget_ok,
                "target_ok": target_ok,
            }
        )

    meeting = [choice for choice in choices if choice["meets_precision"]]
    if not meeting:
        hard_error = {
            "point_id": point["point_id"],
            "tuning_id": point["tuning_id"],
            "schedule_id": point["schedule"]["schedule_id"],
            "code": E_PRECISION_TRADE,
            "message": "fixed-point precision SLA cannot be met",
            "relative_rmse": choices[-1]["report"]["metrics"]["relative_rmse"],
        }
        return choices, hard_error

    affordable_meeting = [choice for choice in meeting if choice["budget_ok"] and choice["target_ok"]]
    if not affordable_meeting:
        failed = meeting[0]
        hard_error = {
            "point_id": point["point_id"],
            "tuning_id": point["tuning_id"],
            "schedule_id": point["schedule"]["schedule_id"],
            "code": E_BUDGET_EXCEEDED,
            "message": "precision-compliant representation exceeds compute/memory/target budget",
            "bits": failed["bits"],
            "resident_memory_mb": failed["estimate"]["resident_memory_mb"],
            "dequant_ops": failed["estimate"]["dequant_ops"],
        }
    return choices, hard_error


def _choose_for_mode(choices: list[dict[str, Any]], mode: str) -> dict[str, Any] | None:
    acceptable = [item for item in choices if item["meets_precision"] and item["budget_ok"] and item["target_ok"]]
    if not acceptable:
        return None
    if mode == "max":
        return max(acceptable, key=lambda item: item["bits"])
    return min(acceptable, key=lambda item: item["bits"])


def plan_search(
    tasks: list[dict[str, Any]],
    platform: dict[str, Any],
    policy: dict[str, Any],
) -> dict[str, Any]:
    points = ordered_points(tasks)
    rejections: list[dict[str, Any]] = []
    selected_by_point: dict[str, dict[str, Any]] = {}
    choices_by_point: dict[str, list[dict[str, Any]]] = {}

    for point in points:
        choices, hard_error = _precision_choices(point, platform, policy)
        choices_by_point[point["point_id"]] = choices
        if hard_error is not None:
            rejections.append(hard_error)
            continue
        selected_by_point[point["point_id"]] = choices[0]

    valid_points = [
        point
        for point in points
        if not any(item["point_id"] == point["point_id"] for item in rejections)
    ]

    option_records: list[dict[str, Any]] = []
    best: dict[str, Any] | None = None
    concurrency_cap = min(
        int(policy["max_concurrency"]), int(policy["worker_count"])
    )

    for concurrency in range(1, concurrency_cap + 1):
        for precision_mode in ("sla", "max"):
            effective: dict[str, dict[str, Any]] = {}
            mode_rejections: list[dict[str, Any]] = []
            mode_feasible = True
            for point in valid_points:
                chosen = _choose_for_mode(choices_by_point[point["point_id"]], precision_mode)
                if chosen is None:
                    mode_feasible = False
                    break
                effective[point["point_id"]] = chosen
            if not mode_feasible:
                continue

            batches = [
                valid_points[start : start + concurrency]
                for start in range(0, len(valid_points), concurrency)
            ]
            planned_batches: list[dict[str, Any]] = []
            peak_memory = 0.0
            peak_interference = 1.0
            total_logical_ms = 0.0
            admitted_ids: list[str] = []
            budget_rejections: list[dict[str, Any]] = []
            memory_feasible = True

            for batch_index, batch_points in enumerate(batches, 1):
                schedules = [point["schedule"] for point in batch_points]
                interference = batch_interference(schedules, platform)
                estimates: list[dict[str, Any]] = []
                for point in batch_points:
                    chosen = effective[point["point_id"]]
                    estimate = estimate_candidate(
                        point["op_kind"],
                        point["workload_shape"],
                        point["schedule"],
                        platform,
                        chosen["bits"],
                        active_threads_in_batch=sum(
                            max(1, int(schedule.get("threads", 1))) for schedule in schedules
                        ),
                    )
                    estimates.append(estimate)
                memory_mb = batch_memory_mb(estimates, platform["thread_occupancy"])
                peak_memory = max(peak_memory, memory_mb)
                peak_interference = max(peak_interference, interference)
                batch_kernel_us = max(item["expected_latency_us"] for item in estimates)
                repeats = int(policy["timing_repeats"]) + int(policy["warmup_repeats"])
                batch_cost_ms = (
                    batch_kernel_us * repeats / 1000.0
                    + float(policy["evaluation_overhead_ms"]) * len(batch_points)
                )
                memory_ok = memory_mb <= float(policy["memory_budget_mb"])
                interference_ok = interference <= float(policy["max_interference_factor"])
                if not memory_ok or not interference_ok:
                    memory_feasible = False
                    break
                if total_logical_ms + batch_cost_ms <= float(policy["logical_clock_budget_ms"]):
                    total_logical_ms += batch_cost_ms
                    admitted_ids.extend(point["point_id"] for point in batch_points)
                    planned_batches.append(
                        {
                            "batch_no": batch_index,
                            "point_ids": [point["point_id"] for point in batch_points],
                            "memory_mb": memory_mb,
                            "interference_factor": interference,
                            "logical_cost_ms": batch_cost_ms,
                        }
                    )
                else:
                    for point in batch_points:
                        budget_rejections.append(
                            {
                                "point_id": point["point_id"],
                                "tuning_id": point["tuning_id"],
                                "schedule_id": point["schedule"]["schedule_id"],
                                "code": E_BUDGET_EXCEEDED,
                                "message": "logical clock budget exhausted at batch boundary",
                                "required_batch_ms": batch_cost_ms,
                                "remaining_ms": float(policy["logical_clock_budget_ms"])
                                - total_logical_ms,
                            }
                        )

            if not memory_feasible:
                continue

            admitted_set = set(admitted_ids)
            admitted_effective = [effective[point_id] for point_id in admitted_ids]
            avg_bits = (
                sum(item["bits"] for item in admitted_effective) / len(admitted_effective)
                if admitted_effective
                else 0
            )
            errors = [
                item["report"]["metrics"]["relative_rmse"] for item in admitted_effective
            ]
            avg_error = sum(errors) / len(errors) if errors else 0.0
            makespan = total_logical_ms
            peak_memory_mb = peak_memory
            option = {
                "concurrency": concurrency,
                "precision_mode": precision_mode,
                "batches": planned_batches,
                "admitted_ids": admitted_ids,
                "budget_rejections": budget_rejections,
                "peak_memory_mb": peak_memory_mb,
                "peak_interference_factor": peak_interference,
                "estimated_logical_ms": makespan,
                "avg_bits": avg_bits,
                "avg_relative_rmse": avg_error,
                "effective_bits": {
                    point_id: effective[point_id]["bits"] for point_id in admitted_ids
                },
                "rank_metrics": {
                    "progress": (len(admitted_ids), concurrency, -makespan),
                    "memory": (-peak_memory_mb, -peak_interference),
                    "precision": (avg_bits, -avg_error),
                },
            }
            rank_tuple = tuple(
                value
                for dimension in policy["priority_order"]
                for value in option["rank_metrics"][dimension]
            )
            option["rank_tuple"] = rank_tuple
            option_records.append(option)
            if best is None or rank_tuple > best["rank_tuple"]:
                best = option

    if best is None:
        # This normally means C=1 violates a hard memory/interference budget.
        for point in valid_points:
            if not any(item["point_id"] == point["point_id"] for item in rejections):
                choices = choices_by_point[point["point_id"]]
                cheapest = min(choices, key=lambda item: item["estimate"]["resident_memory_mb"])
                rejections.append(
                    {
                        "point_id": point["point_id"],
                        "tuning_id": point["tuning_id"],
                        "schedule_id": point["schedule"]["schedule_id"],
                        "code": E_BUDGET_EXCEEDED,
                        "message": "no feasible concurrency under memory/interference budget",
                        "resident_memory_mb": cheapest["estimate"]["resident_memory_mb"],
                    }
                )
        best = {
            "concurrency": 1,
            "precision_mode": "sla",
            "batches": [],
            "admitted_ids": [],
            "budget_rejections": [],
            "effective_bits": {},
            "peak_memory_mb": 0.0,
            "peak_interference_factor": 1.0,
            "estimated_logical_ms": 0.0,
            "rank_tuple": (),
        }

    rejections.extend(best.get("budget_rejections", []))
    best["effective_concurrency_cap"] = concurrency_cap
    best["configured_max_concurrency"] = int(policy["max_concurrency"])
    best["worker_count"] = int(policy["worker_count"])
    admitted_set = set(best["admitted_ids"])
    serialized_points = []
    for point in points:
        if point["point_id"] not in admitted_set:
            continue
        chosen = _choose_for_mode(
            choices_by_point[point["point_id"]], best["precision_mode"]
        )
        if chosen is None:
            continue
        serialized = dict(point)
        serialized["effective_bits"] = chosen["bits"]
        serialized["quant_report"] = chosen["report"]
        serialized["estimate"] = chosen["estimate"]
        serialized.pop("task", None)
        serialized_points.append(serialized)

    return {
        "points": serialized_points,
        "point_index": {point["point_id"]: point for point in serialized_points},
        "batches": best["batches"],
        "rejections": rejections,
        "selected": {
            key: value
            for key, value in best.items()
            if key not in {"budget_rejections", "rank_metrics", "rank_tuple"}
        },
        "options_considered": [
            {
                "concurrency": option["concurrency"],
                "precision_mode": option["precision_mode"],
                "admitted": len(option["admitted_ids"]),
                "peak_memory_mb": option["peak_memory_mb"],
                "peak_interference_factor": option["peak_interference_factor"],
                "estimated_logical_ms": option["estimated_logical_ms"],
                "avg_bits": option["avg_bits"],
                "rank_tuple": list(option["rank_tuple"]),
            }
            for option in option_records
        ],
        "priority_order": policy["priority_order"],
    }
