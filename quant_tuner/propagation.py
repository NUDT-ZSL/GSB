from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .canonical import content_digest
from .config_io import detect_and_load
from .ids import quant_digest
from .planner import ordered_points
from .workload import compute_quant_report


def diff_tasks(old_tasks: list[dict[str, Any]], new_tasks: list[dict[str, Any]]) -> dict[str, Any]:
    old_map = {task["tuning_id"]: task for task in old_tasks}
    new_map = {task["tuning_id"]: task for task in new_tasks}
    quant_changed: set[str] = set()
    schedule_changed: set[tuple[str, str]] = set()
    generation_bumps: list[dict[str, Any]] = []

    for tuning_id, new_task in new_map.items():
        old_task = old_map.get(tuning_id)
        if old_task is None:
            quant_changed.add(tuning_id)
            continue
        if content_digest(old_task["quant_config"]) != content_digest(new_task["quant_config"]):
            quant_changed.add(tuning_id)

        old_schedules = {
            schedule["schedule_id"]: schedule for schedule in old_task["candidate_schedules"]
        }
        for schedule in new_task["candidate_schedules"]:
            schedule_id = schedule["schedule_id"]
            old_schedule = old_schedules.get(schedule_id)
            if old_schedule is None or content_digest(old_schedule) != content_digest(schedule):
                schedule_changed.add((tuning_id, schedule_id))

        old_generation = old_task.get("config_generation")
        new_generation = new_task.get("config_generation")
        if old_generation is not None and new_generation is not None:
            if int(new_generation) > int(old_generation):
                generation_bumps.append(
                    {
                        "tuning_id": tuning_id,
                        "old_generation": int(old_generation),
                        "new_generation": int(new_generation),
                    }
                )

    return {
        "quant_changed": sorted(quant_changed),
        "schedule_changed": sorted([list(item) for item in schedule_changed]),
        "generation_bumps": generation_bumps,
    }


def diff_config_bundles(
    old_bundle: dict[str, Any], new_bundle: dict[str, Any]
) -> dict[str, Any]:
    old_records = {record["record_id"]: record for record in old_bundle["records"]}
    generation_bumps: list[dict[str, Any]] = []
    quant_changed: set[str] = set()
    schedule_changed: set[tuple[str, str]] = set()

    for new_record in new_bundle["records"]:
        old_record = old_records.get(new_record["record_id"])
        if old_record is None:
            quant_changed.add(new_record["tuning_id"])
            continue
        if int(new_record["generation"]) > int(old_record["generation"]):
            generation_bumps.append(
                {
                    "record_id": new_record["record_id"],
                    "tuning_id": new_record["tuning_id"],
                    "old_generation": int(old_record["generation"]),
                    "new_generation": int(new_record["generation"]),
                }
            )
        if content_digest(old_record["quant_config"]) != content_digest(
            new_record["quant_config"]
        ):
            quant_changed.add(new_record["tuning_id"])
        old_schedule_id = old_record["schedule"].get("schedule_id")
        if content_digest(old_record["schedule"]) != content_digest(new_record["schedule"]):
            schedule_changed.add(
                (new_record["tuning_id"], new_record["schedule"].get("schedule_id", old_schedule_id))
            )

    return {
        "generation_bumps": generation_bumps,
        "quant_changed": sorted(quant_changed),
        "schedule_changed": sorted([list(item) for item in schedule_changed]),
    }


def plan_propagation(
    old_tasks: list[dict[str, Any]],
    new_tasks: list[dict[str, Any]],
    config_diff: dict[str, Any] | None = None,
) -> dict[str, Any]:
    task_diff = diff_tasks(old_tasks, new_tasks)
    quant_changed = set(task_diff["quant_changed"])
    schedule_changed = {tuple(item) for item in task_diff["schedule_changed"]}
    generation_bumps = list(task_diff["generation_bumps"])

    if config_diff:
        quant_changed.update(config_diff.get("quant_changed", []))
        schedule_changed.update(
            tuple(item) for item in config_diff.get("schedule_changed", [])
        )
        generation_bumps.extend(config_diff.get("generation_bumps", []))

    points = ordered_points(new_tasks)
    recompute_points: list[str] = []
    reuse_points: list[str] = []
    recompute_nodes: list[str] = []
    reuse_nodes: list[str] = []
    affected_set: set[str] = set()

    current_quant_nodes: dict[str, str] = {}
    for task in new_tasks:
        tuning_id = task["tuning_id"]
        q_digest = quant_digest(task["quant_config"])
        q_node = f"quant:{tuning_id}:{q_digest}"
        current_quant_nodes[tuning_id] = q_node

    for point in points:
        tuning_id = point["tuning_id"]
        p_node = f"point:{point['point_id']}"
        quant_change = tuning_id in quant_changed
        schedule_change = (tuning_id, point["schedule"]["schedule_id"]) in schedule_changed
        if quant_change or schedule_change:
            recompute_points.append(point["point_id"])
            recompute_nodes.append(p_node)
            affected_set.add(p_node)
        else:
            reuse_points.append(point["point_id"])

    recompute_summaries: list[str] = []
    reuse_summaries: list[str] = []
    for tuning_id, q_node in sorted(current_quant_nodes.items()):
        if tuning_id in quant_changed:
            recompute_summaries.append(q_node)
            recompute_nodes.append(q_node)
            affected_set.add(q_node)
        else:
            reuse_summaries.append(q_node)
            # A schedule-only revision touches this upstream dependency, but the
            # quant summary itself is reused.
            if any(tuning_id == item[0] for item in schedule_changed):
                reuse_nodes.append(q_node)
                affected_set.add(q_node)

    full_universe = [f"point:{point['point_id']}" for point in points] + sorted(
        current_quant_nodes.values()
    )
    full_recompute = list(recompute_nodes)
    full_reuse = [f"point:{pid}" for pid in reuse_points] + reuse_summaries

    return {
        "generation_bumps": generation_bumps,
        "quant_changed": sorted(quant_changed),
        "schedule_changed": [list(item) for item in sorted(schedule_changed)],
        "recompute_points": sorted(recompute_points),
        "reuse_points": sorted(reuse_points),
        "recompute_quant_summaries": sorted(recompute_summaries),
        "reuse_quant_summaries": sorted(reuse_summaries),
        "recompute_set": sorted(set(recompute_nodes)),
        "reuse_set": sorted(set(reuse_nodes) - set(recompute_nodes)),
        "affected_set": sorted(affected_set),
        "coverage_universe": sorted(full_universe),
        "full_recompute_set": sorted(set(full_recompute)),
        "full_reuse_set": sorted(set(full_reuse) - set(full_recompute)),
    }


def execute_quant_cache(
    tasks: list[dict[str, Any]],
    propagation: dict[str, Any],
    cache_path: str | Path,
) -> dict[str, Any]:
    cache_path = Path(cache_path)
    cache: dict[str, Any] = {}
    if cache_path.exists():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))

    recomputed: list[str] = []
    reused: list[str] = []
    for task in tasks:
        q_digest = quant_digest(task["quant_config"])
        node = f"quant:{task['tuning_id']}:{q_digest}"
        if node in propagation["recompute_quant_summaries"] or q_digest not in cache:
            report = compute_quant_report(
                task["tuning_id"],
                task["op_kind"],
                task["workload_shape"],
                task["quant_config"],
                int(task["quant_config"].get("bits", 8)),
            )
            cache[q_digest] = report
            recomputed.append(node)
        else:
            reused.append(node)

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {"cache_path": str(cache_path), "recomputed": sorted(recomputed), "reused": sorted(reused)}


def plan_v2_config_propagation(
    old_config_dir: str | Path,
    new_config_dir: str | Path,
    old_tasks: list[dict[str, Any]],
    new_tasks: list[dict[str, Any]],
) -> dict[str, Any]:
    old_bundle = detect_and_load(old_config_dir)
    new_bundle = detect_and_load(new_config_dir)
    config_diff = diff_config_bundles(old_bundle, new_bundle)
    return plan_propagation(old_tasks, new_tasks, config_diff)


def seed_quant_cache(tasks: list[dict[str, Any]], cache_path: str | Path) -> dict[str, Any]:
    cache_path = Path(cache_path)
    cache: dict[str, Any] = {}
    if cache_path.exists():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))

    seeded: list[str] = []
    reused: list[str] = []
    for task in tasks:
        q_digest = quant_digest(task["quant_config"])
        node = f"quant:{task['tuning_id']}:{q_digest}"
        if q_digest in cache:
            reused.append(node)
            continue
        cache[q_digest] = compute_quant_report(
            task["tuning_id"],
            task["op_kind"],
            task["workload_shape"],
            task["quant_config"],
            int(task["quant_config"].get("bits", 8)),
        )
        seeded.append(node)

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "cache_path": str(cache_path),
        "seeded": sorted(seeded),
        "reused": sorted(reused),
    }
