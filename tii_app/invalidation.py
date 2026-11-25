"""Dependency-targeted invalidation for config and data evolution."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable, Iterable, Mapping

from .configs import ResolvedConfig
from .keys import stage_keys
from .models import Task
from .util import sha256_obj


STAGE_ORDER = (
    "model",
    "sample",
    "quantize",
    "forward",
    "train_backward",
    "inference",
)

STAGE_DEPENDENCY = {
    "model": ["config"],
    "sample": ["task_data"],
    "quantize": ["sample", "task_data"],
    "forward": ["model", "quantize"],
    "train_backward": ["forward", "sample"],
    "inference": ["forward"],
}


def _classify(old_task: Task, new_task: Task, old_config: ResolvedConfig, new_config: ResolvedConfig) -> str:
    data_changed = old_task.content_hash != new_task.content_hash
    config_changed = (
        old_config.generation != new_config.generation
        or old_config.config_hash != new_config.config_hash
    )
    if data_changed and config_changed:
        return "both"
    if config_changed:
        return "config_only"
    if data_changed:
        return "data_only"
    return "unchanged"


def invalidation_plan(
    previous_tasks: Mapping[str, Task],
    current_tasks: Iterable[Task],
    previous_config_for: Callable[[Task], ResolvedConfig],
    current_config_for: Callable[[Task], ResolvedConfig],
    *,
    global_seed: int,
    committed_cache_index: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute reusable upstream stages and the affected recompute suffix."""

    committed_cache_index = committed_cache_index or {}
    current_tasks = list(current_tasks)
    records: list[dict[str, Any]] = []
    recompute_tasks: list[str] = []
    reuse_tasks: list[str] = []
    for current in sorted(current_tasks, key=lambda item: (item.logical_clock_ms, item.case_id)):
        previous = previous_tasks.get(current.case_id, current)
        old_config = previous_config_for(previous)
        new_config = current_config_for(current)
        classification = _classify(previous, current, old_config, new_config)
        old_keys = stage_keys(previous, old_config, global_seed)
        new_keys = stage_keys(current, new_config, global_seed)
        reused: list[str] = []
        recomputed: list[str] = []
        cache_hits: list[str] = []
        for stage in STAGE_ORDER:
            if old_keys[stage] == new_keys[stage]:
                reused.append(stage)
                if old_keys[stage] in committed_cache_index:
                    cache_hits.append(stage)
            else:
                recomputed.append(stage)
        first_recompute = recomputed[0] if recomputed else None
        entry = {
            "case_id": current.case_id,
            "classification": classification,
            "old_task_content_hash": previous.content_hash,
            "new_task_content_hash": current.content_hash,
            "old_config_generation": old_config.generation,
            "new_config_generation": new_config.generation,
            "reused_stages": reused,
            "recomputed_stages": recomputed,
            "committed_cache_hits": cache_hits,
            "recompute_suffix_start": first_recompute,
            "old_keys": {stage: old_keys[stage] for stage in STAGE_ORDER},
            "new_keys": {stage: new_keys[stage] for stage in STAGE_ORDER},
        }
        records.append(entry)
        if recomputed:
            recompute_tasks.append(current.case_id)
        else:
            reuse_tasks.append(current.case_id)
    by_classification: dict[str, list[str]] = {}
    for record in records:
        by_classification.setdefault(record["classification"], []).append(record["case_id"])
    coverage = {
        "recompute_tasks": recompute_tasks,
        "reuse_tasks": reuse_tasks,
        "intersection": sorted(set(recompute_tasks) & set(reuse_tasks)),
        "union": sorted(set(recompute_tasks) | set(reuse_tasks)),
        "all_current_tasks": sorted(task.case_id for task in current_tasks),
        "disjoint": not (set(recompute_tasks) & set(reuse_tasks)),
        "covers_all": set(recompute_tasks) | set(reuse_tasks)
        == {task.case_id for task in current_tasks},
    }
    report = {
        "schema_version": 1,
        "global_seed": global_seed,
        "stage_order": list(STAGE_ORDER),
        "stage_dependency": STAGE_DEPENDENCY,
        "by_classification": by_classification,
        "coverage": coverage,
        "tasks": records,
    }
    report["report_checksum"] = sha256_obj(report)
    return report


def build_config_only_scenario(
    tasks: list[Task],
    *,
    v1_config: ResolvedConfig,
    current_config_for: Callable[[Task], ResolvedConfig],
    global_seed: int,
    committed_cache_index: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    previous = {task.case_id: task for task in tasks}
    return invalidation_plan(
        previous,
        tasks,
        lambda _task: v1_config,
        current_config_for,
        global_seed=global_seed,
        committed_cache_index=committed_cache_index,
    )


def build_data_only_scenario(
    tasks: list[Task],
    *,
    revision_bumps: Iterable[str],
    size_changes: Mapping[str, int],
    config_for: Callable[[Task], ResolvedConfig],
    global_seed: int,
    committed_cache_index: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    previous = {task.case_id: task for task in tasks}
    bumped: list[Task] = []
    for task in tasks:
        if task.case_id in revision_bumps:
            task = replace(task, revision=task.revision + 1)
        if task.case_id in size_changes:
            task = replace(task, data_size=int(size_changes[task.case_id]))
        bumped.append(task)
    return invalidation_plan(
        previous,
        bumped,
        lambda task: config_for(previous[task.case_id]),
        config_for,
        global_seed=global_seed,
        committed_cache_index=committed_cache_index,
    )


def build_both_scenario(
    tasks: list[Task],
    *,
    revision_bumps: Iterable[str],
    size_changes: Mapping[str, int],
    v1_config: ResolvedConfig,
    current_config_for: Callable[[Task], ResolvedConfig],
    global_seed: int,
    committed_cache_index: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    previous = {task.case_id: task for task in tasks}
    changed: list[Task] = []
    for task in tasks:
        if task.case_id in revision_bumps:
            task = replace(task, revision=task.revision + 1)
        if task.case_id in size_changes:
            task = replace(task, data_size=int(size_changes[task.case_id]))
        changed.append(task)
    return invalidation_plan(
        previous,
        changed,
        lambda _task: v1_config,
        current_config_for,
        global_seed=global_seed,
        committed_cache_index=committed_cache_index,
    )
