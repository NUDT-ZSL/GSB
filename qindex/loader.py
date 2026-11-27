"""Workload loading and validation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .model import IndexConfig, QualityEvent


def load_workload(path: str | Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        workload = json.load(handle)
    validate_workload(workload)
    return workload


def validate_workload(workload: dict[str, Any]) -> None:
    if not isinstance(workload, dict):
        raise ValueError("workload 根节点必须是对象")
    if workload.get("format") not in (None, "quality-index-workload/v1"):
        raise ValueError("不支持的 workload format")
    config = workload.get("config")
    if not isinstance(config, dict):
        raise ValueError("workload.config 必须是对象")
    IndexConfig.from_dict(config)
    timeline = workload.get("timeline")
    if not isinstance(timeline, list) or not timeline:
        raise ValueError("workload.timeline 必须是非空数组")

    seen_event_ids: set[str] = set()
    seen_query_ids: set[str] = set()
    seen_invalidation_ids: set[str] = set()
    previous_time: int | None = None
    for step_index, step in enumerate(timeline):
        if not isinstance(step, dict):
            raise ValueError(f"timeline[{step_index}] 必须是对象")
        if "time" not in step:
            raise ValueError(f"timeline[{step_index}] 缺少 time")
        time = int(step["time"])
        if previous_time is not None and time < previous_time:
            raise ValueError("timeline 的 time 必须非递减")
        previous_time = time

        writes = step.get("writes", [])
        if not isinstance(writes, list):
            raise ValueError(f"timeline[{step_index}].writes 必须是数组")
        for raw_event in writes:
            event = QualityEvent.from_dict(raw_event)
            if event.logical_time != time:
                raise ValueError(
                    f"事件 {event.event_id} 的 logical_time 与所在时刻不一致"
                )
            if event.event_id in seen_event_ids:
                raise ValueError(f"重复的 event_id: {event.event_id}")
            seen_event_ids.add(event.event_id)

        invalidations = step.get("invalidations", [])
        if not isinstance(invalidations, list):
            raise ValueError(
                f"timeline[{step_index}].invalidations 必须是数组"
            )
        for invalidation in invalidations:
            if not isinstance(invalidation, dict):
                raise ValueError("invalidation 必须是对象")
            invalidation_id = invalidation.get("invalidation_id")
            if invalidation_id is not None:
                invalidation_id = str(invalidation_id)
                if invalidation_id in seen_invalidation_ids:
                    raise ValueError(f"重复的 invalidation_id: {invalidation_id}")
                seen_invalidation_ids.add(invalidation_id)
            if not invalidation.get("station") and not invalidation.get(
                "batch_prefix"
            ):
                raise ValueError("invalidation 至少要指定 station 或 batch_prefix")

        queries = step.get("queries", [])
        if not isinstance(queries, list):
            raise ValueError(f"timeline[{step_index}].queries 必须是数组")
        for query in queries:
            if not isinstance(query, dict):
                raise ValueError("query 必须是对象")
            for field in ("query_id", "process", "shift"):
                if field not in query:
                    raise ValueError(f"query 缺少字段: {field}")
            query_id = str(query["query_id"])
            if query_id in seen_query_ids:
                raise ValueError(f"重复的 query_id: {query_id}")
            seen_query_ids.add(query_id)
            if "batch_prefix" not in query:
                raise ValueError("query 缺少字段: batch_prefix")


def normalized_timeline(workload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "time": int(step["time"]),
            "writes": list(step.get("writes", [])),
            "invalidations": list(step.get("invalidations", [])),
            "queries": list(step.get("queries", [])),
        }
        for step in workload["timeline"]
    ]
