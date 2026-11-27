"""Workload schema, deterministic JSON helpers, and validation."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from typing import Any, Mapping

DIMENSIONS = ("cpu", "memory", "io")
CATEGORIES = ("章节渲染", "音视频转码", "索引重建", "预热预取")
CATEGORY_CODES = {
    "章节渲染": "chapter_render",
    "音视频转码": "av_transcode",
    "索引重建": "index_rebuild",
    "预热预取": "prefetch_warmup",
}
SCHEMA_VERSION = "1.0"


class ValidationError(ValueError):
    """Raised when a workload or persisted state is malformed."""


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def digest_value(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def workload_digest(workload: Mapping[str, Any]) -> str:
    return digest_value(workload)


def zero_vector() -> dict[str, int]:
    return {dimension: 0 for dimension in DIMENSIONS}


def vector_add(left: Mapping[str, int], right: Mapping[str, int]) -> dict[str, int]:
    return {dimension: left[dimension] + right[dimension] for dimension in DIMENSIONS}


def vector_sub(left: Mapping[str, int], right: Mapping[str, int]) -> dict[str, int]:
    return {dimension: left[dimension] - right[dimension] for dimension in DIMENSIONS}


def vector_leq(left: Mapping[str, int], right: Mapping[str, int]) -> bool:
    return all(left[dimension] <= right[dimension] for dimension in DIMENSIONS)


def vector_positive(value: Mapping[str, int]) -> bool:
    return any(value[dimension] > 0 for dimension in DIMENSIONS)


def vector_nonnegative(value: Mapping[str, int]) -> bool:
    return all(value[dimension] >= 0 for dimension in DIMENSIONS)


def vector_sum(value: Mapping[str, int]) -> int:
    return sum(value[dimension] for dimension in DIMENSIONS)


def _require_int(value: Any, label: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{label} 必须是整数")
    if minimum is not None and value < minimum:
        raise ValidationError(f"{label} 必须大于等于 {minimum}")
    return value


def _require_vector(
    value: Any,
    label: str,
    *,
    minimum: int = 0,
    allow_zero: bool = True,
) -> dict[str, int]:
    if not isinstance(value, dict):
        raise ValidationError(f"{label} 必须是三维资源对象")
    if set(value) != set(DIMENSIONS):
        raise ValidationError(f"{label} 必须且只能包含 {list(DIMENSIONS)}")
    result = {
        dimension: _require_int(
            value[dimension], f"{label}.{dimension}", minimum=minimum
        )
        for dimension in DIMENSIONS
    }
    if not allow_zero and not vector_positive(result):
        raise ValidationError(f"{label} 至少一个维度必须大于零")
    return result


def normalize_workload(workload: Mapping[str, Any]) -> dict[str, Any]:
    """Return a deep copy with stable ordering suitable for hashing."""

    normalized = copy.deepcopy(dict(workload))
    nodes = normalized.get("nodes")
    jobs = normalized.get("jobs")
    events = normalized.get("events")
    if isinstance(nodes, list):
        normalized["nodes"] = sorted(nodes, key=lambda item: item.get("node_id", ""))
    if isinstance(jobs, list):
        normalized["jobs"] = sorted(jobs, key=lambda item: item.get("job_id", ""))
    if isinstance(events, list):
        normalized["events"] = sorted(
            events, key=lambda item: (item.get("at", -1), item.get("sequence", -1))
        )
    return normalized


def validate_workload(workload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(workload, dict):
        raise ValidationError("workload 必须是 JSON 对象")
    if workload.get("schema_version") != SCHEMA_VERSION:
        raise ValidationError(
            f"workload.schema_version 必须为 {SCHEMA_VERSION!r}"
        )

    nodes = workload.get("nodes")
    jobs = workload.get("jobs")
    events = workload.get("events")
    if not isinstance(nodes, list) or not nodes:
        raise ValidationError("workload.nodes 必须是非空数组")
    if not isinstance(jobs, list) or not jobs:
        raise ValidationError("workload.jobs 必须是非空数组")
    if not isinstance(events, list):
        raise ValidationError("workload.events 必须是数组")

    node_ids: set[str] = set()
    for index, node in enumerate(nodes):
        prefix = f"nodes[{index}]"
        if not isinstance(node, dict):
            raise ValidationError(f"{prefix} 必须是对象")
        node_id = node.get("node_id")
        if not isinstance(node_id, str) or not node_id:
            raise ValidationError(f"{prefix}.node_id 必须是非空字符串")
        if node_id in node_ids:
            raise ValidationError(f"重复节点标识: {node_id}")
        node_ids.add(node_id)
        capacity = _require_vector(
            node.get("capacity"), f"{prefix}.capacity", allow_zero=False
        )
        if vector_sum(capacity) <= 0:
            raise ValidationError(f"{prefix}.capacity 必须为正")
        categories = node.get("categories")
        if not isinstance(categories, list) or not categories:
            raise ValidationError(f"{prefix}.categories 必须是非空数组")
        if len(set(categories)) != len(categories):
            raise ValidationError(f"{prefix}.categories 不得重复")
        unknown = sorted(set(categories) - set(CATEGORIES))
        if unknown:
            raise ValidationError(f"{prefix}.categories 含未知类别: {unknown}")

    job_ids: set[str] = set()
    submit_times: dict[str, int] = {}
    for index, job in enumerate(jobs):
        prefix = f"jobs[{index}]"
        if not isinstance(job, dict):
            raise ValidationError(f"{prefix} 必须是对象")
        job_id = job.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            raise ValidationError(f"{prefix}.job_id 必须是非空字符串")
        if job_id in job_ids:
            raise ValidationError(f"重复作业标识: {job_id}")
        job_ids.add(job_id)

        category = job.get("category")
        if category not in CATEGORIES:
            raise ValidationError(f"{prefix}.category 不是支持的类别: {category!r}")
        category_code = job.get("category_code")
        if category_code is not None and category_code != CATEGORY_CODES[category]:
            raise ValidationError(
                f"{prefix}.category_code 与 category 不一致"
            )
        priority = _require_int(job.get("priority"), f"{prefix}.priority", minimum=1)
        if priority > 5:
            raise ValidationError(f"{prefix}.priority 必须在 1-5 之间")
        if not isinstance(job.get("preemptible"), bool):
            raise ValidationError(f"{prefix}.preemptible 必须是布尔值")
        demand = _require_vector(
            job.get("demand"), f"{prefix}.demand", minimum=0, allow_zero=False
        )
        duration = _require_int(
            job.get("duration"), f"{prefix}.duration", minimum=1
        )
        deadline = _require_int(job.get("deadline"), f"{prefix}.deadline", minimum=0)
        submit_time = _require_int(
            job.get("submit_time"), f"{prefix}.submit_time", minimum=0
        )
        if deadline < submit_time:
            raise ValidationError(f"{prefix}.deadline 不得早于 submit_time")
        submit_times[job_id] = submit_time
        if duration <= 0:
            raise ValidationError(f"{prefix}.duration 必须大于零")

        mutex_group = job.get("mutex_group")
        if mutex_group is not None and (
            not isinstance(mutex_group, str) or not mutex_group
        ):
            raise ValidationError(f"{prefix}.mutex_group 必须是非空字符串或 null")
        if "downgrade_plan" not in job:
            raise ValidationError(f"{prefix} 缺少 downgrade_plan 字段")
        plan = job.get("downgrade_plan")
        if plan is not None:
            if not isinstance(plan, dict):
                raise ValidationError(f"{prefix}.downgrade_plan 必须是对象或 null")
            profile = plan.get("profile")
            if not isinstance(profile, str) or not profile:
                raise ValidationError(
                    f"{prefix}.downgrade_plan.profile 必须是非空字符串"
                )
            demoted_demand = _require_vector(
                plan.get("demand"),
                f"{prefix}.downgrade_plan.demand",
                minimum=0,
                allow_zero=False,
            )
            release = _require_vector(
                plan.get("release"),
                f"{prefix}.downgrade_plan.release",
                minimum=0,
                allow_zero=False,
            )
            if not vector_leq(demoted_demand, demand):
                raise ValidationError(
                    f"{prefix}.downgrade_plan.demand 不得超过原始需求"
                )
            expected_release = vector_sub(demand, demoted_demand)
            if release != expected_release:
                raise ValidationError(
                    f"{prefix}.downgrade_plan.release 必须等于原始需求减降级需求"
                )
            if not vector_positive(release):
                raise ValidationError(
                    f"{prefix}.downgrade_plan 必须至少释放一个资源刻度"
                )

    seen_events: set[str] = set()
    cancel_seen: set[str] = set()
    submit_event_seen: set[str] = set()
    previous_key: tuple[int, int] | None = None
    for index, event in enumerate(events):
        prefix = f"events[{index}]"
        if not isinstance(event, dict):
            raise ValidationError(f"{prefix} 必须是对象")
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            raise ValidationError(f"{prefix}.event_id 必须是非空字符串")
        if event_id in seen_events:
            raise ValidationError(f"重复事件标识: {event_id}")
        seen_events.add(event_id)
        at = _require_int(event.get("at"), f"{prefix}.at", minimum=0)
        sequence = _require_int(
            event.get("sequence"), f"{prefix}.sequence", minimum=0
        )
        key = (at, sequence)
        if previous_key is not None and key <= previous_key:
            raise ValidationError("events 必须按 (at, sequence) 严格有序")
        previous_key = key
        job_id = event.get("job_id")
        if job_id not in job_ids:
            raise ValidationError(f"{prefix}.job_id 引用了未知作业")
        event_type = event.get("type")
        if event_type == "submit":
            if at != submit_times[job_id]:
                raise ValidationError(
                    f"{prefix}.at 必须等于作业 {job_id} 的 submit_time"
                )
            if job_id in submit_event_seen:
                raise ValidationError(f"作业 {job_id} 存在重复提交事件")
            submit_event_seen.add(job_id)
        elif event_type == "cancel":
            if job_id in cancel_seen:
                raise ValidationError(f"作业 {job_id} 存在重复取消事件")
            if at < submit_times[job_id]:
                raise ValidationError(f"{prefix}.at 不得早于作业提交时刻")
            cancel_seen.add(job_id)
        else:
            raise ValidationError(f"{prefix}.type 必须为 submit 或 cancel")

    missing_submit = job_ids - submit_event_seen
    if missing_submit:
        raise ValidationError(f"作业缺少提交事件: {sorted(missing_submit)}")
    return normalize_workload(workload)


def load_workload(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return validate_workload(payload)


def dump_workload(workload: Mapping[str, Any], path: str) -> None:
    validated = validate_workload(workload)
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(validated, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def demand_ratio(
    allocated: Mapping[str, int], demand: Mapping[str, int]
) -> float:
    demand_total = vector_sum(demand)
    if demand_total <= 0:
        return 0.0
    return round(vector_sum(allocated) / demand_total, 6)

