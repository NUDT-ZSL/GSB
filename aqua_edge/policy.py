"""Policy cloning and deterministic adjustment."""

from __future__ import annotations

import copy
from typing import Iterable

from .errors import AquaError
from .model import Project


def apply_policy(
    project: Project,
    *,
    max_streams: int | None = None,
    priority_order: Iterable[str] | None = None,
    priorities: dict[str, int] | None = None,
    windows: dict[str, int] | None = None,
    window_default: int | None = None,
) -> Project:
    result = copy.deepcopy(project)
    stream_by_id = result.stream_map()

    if max_streams is not None:
        if isinstance(max_streams, bool) or not isinstance(max_streams, int) or max_streams <= 0:
            raise AquaError("E_ARGUMENT", "max_concurrent_streams 必须是正整数")
        result.network.max_concurrent_streams = max_streams

    if priority_order is not None:
        order = [item.strip() for item in priority_order if item and item.strip()]
        unknown = [item for item in order if item not in stream_by_id]
        if unknown:
            raise AquaError(
                "E_STREAM",
                "priority_order 引用了不存在的流",
                {"unknown_stream_ids": unknown},
            )
        existing_max = max(stream.priority for stream in result.streams)
        base = max(100, existing_max + len(order) + 1)
        for index, stream_id in enumerate(order):
            stream_by_id[stream_id].priority = base - index

    if priorities:
        for stream_id, priority in priorities.items():
            if stream_id not in stream_by_id:
                raise AquaError(
                    "E_STREAM",
                    "priority 调整引用了不存在的流",
                    {"stream_id": stream_id},
                )
            if isinstance(priority, bool) or not isinstance(priority, int):
                raise AquaError(
                    "E_ARGUMENT",
                    "priority 必须是整数",
                    {"stream_id": stream_id, "value": priority},
                )
            stream_by_id[stream_id].priority = priority

    if window_default is not None:
        if isinstance(window_default, bool) or not isinstance(window_default, int) or window_default <= 0:
            raise AquaError("E_ARGUMENT", "window_default 必须是正整数")
        for stream in result.streams:
            stream.window_bytes = window_default

    if windows:
        for stream_id, window in windows.items():
            if stream_id not in stream_by_id:
                raise AquaError(
                    "E_STREAM",
                    "window 调整引用了不存在的流",
                    {"stream_id": stream_id},
                )
            if isinstance(window, bool) or not isinstance(window, int) or window <= 0:
                raise AquaError(
                    "E_ARGUMENT",
                    "window_bytes 必须是正整数",
                    {"stream_id": stream_id, "value": window},
                )
            stream_by_id[stream_id].window_bytes = window

    return result


def parse_csv_list(value: str | None) -> list[str] | None:
    if value is None:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_assignments(value: str | None, *, integer: bool = True) -> dict[str, int] | None:
    if value is None:
        return None
    result: dict[str, int] = {}
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise AquaError(
                "E_ARGUMENT",
                "调整项必须使用 STREAM=VALUE 格式",
                {"item": item},
            )
        key, raw_value = item.split("=", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if not key or not raw_value:
            raise AquaError("E_ARGUMENT", "调整项缺少流标识或值", {"item": item})
        try:
            parsed = int(raw_value)
        except ValueError as exc:
            if integer:
                raise AquaError(
                    "E_ARGUMENT",
                    "调整值必须是整数",
                    {"item": item},
                ) from exc
            raise
        result[key] = parsed
    return result
