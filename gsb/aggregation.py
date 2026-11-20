from __future__ import annotations

import math
from collections.abc import Iterable, Iterator
from typing import Any

from .models import AggregateSpec, ColumnBatch, QuerySpec
from .util import (
    canonical_json,
    first_difference,
    group_key_sort_key,
    group_key_token,
    sha256_json,
    sort_key_token,
)


def empty_aggregate_state() -> dict[str, Any]:
    return {"n": 0, "sum": 0.0, "sumsq": 0.0}


def new_group_state(key: Any, query: QuerySpec) -> dict[str, Any]:
    return {
        "key": key,
        "sort_key": sort_key_token(key),
        "rows": 0,
        "aggs": {agg.name: empty_aggregate_state() for agg in query.aggregates},
    }


def _filter_matches(value: Any, query: QuerySpec) -> bool:
    filter_spec = query.filter
    if filter_spec is None:
        return True
    if filter_spec.op == "eq":
        return value == filter_spec.value
    if filter_spec.op == "ne":
        return value != filter_spec.value
    if filter_spec.op == "in":
        values = filter_spec.value if isinstance(filter_spec.value, list) else [filter_spec.value]
        return value in values
    return False


def _add_numeric(state: dict[str, Any], value: int | float) -> None:
    numeric = float(value)
    state["n"] += 1
    state["sum"] = state["sum"] + numeric
    state["sumsq"] = state["sumsq"] + numeric * numeric


def aggregate_rows(rows: Iterable[dict[str, Any]], query: QuerySpec) -> dict[str, Any]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        if query.filter is not None and not _filter_matches(row[query.filter.column], query):
            continue
        key = row[query.group_key]
        token = group_key_token(key)
        entry = grouped.get(token)
        if entry is None:
            entry = new_group_state(key, query)
            grouped[token] = entry
        entry["rows"] += 1
        for agg in query.aggregates:
            agg_state = entry["aggs"][agg.name]
            if agg.op == "count":
                if agg.column is None or row[agg.column] is not None:
                    agg_state["n"] += 1
                continue
            value = row[agg.column] if agg.column is not None else None
            if value is None:
                continue
            _add_numeric(agg_state, value)
    return grouped


def aggregate_batch(batch: ColumnBatch, query: QuerySpec) -> dict[str, Any]:
    def rows() -> Iterator[dict[str, Any]]:
        for index in range(batch.row_count):
            yield {name: values[index] for name, values in batch.columns.items()}

    return aggregate_rows(rows(), query)


def aggregate_batch_partial(batch: ColumnBatch, query: QuerySpec) -> dict[str, Any]:
    return aggregate_batch(batch, query)


def merge_into(base: dict[str, Any], delta: dict[str, Any], query: QuerySpec) -> None:
    base["rows"] += int(delta["rows"])
    for agg in query.aggregates:
        left = base["aggs"][agg.name]
        right = delta["aggs"][agg.name]
        left["n"] += int(right["n"])
        left["sum"] = left["sum"] + right["sum"]
        left["sumsq"] = left["sumsq"] + right["sumsq"]


def merge_partial(
    groups: dict[str, dict[str, Any]],
    partial: dict[str, dict[str, Any]],
    query: QuerySpec,
) -> None:
    for token in sorted(partial, key=lambda item: partial[item]["sort_key"]):
        delta = partial[token]
        base = groups.get(token)
        if base is None:
            base = new_group_state(delta["key"], query)
            groups[token] = base
        merge_into(base, delta, query)


def move_channels(
    normal: dict[str, dict[str, Any]],
    hot: dict[str, dict[str, Any]],
    hot_tokens: set[str],
) -> None:
    for token in sorted(hot_tokens):
        state = normal.pop(token, None)
        if state is not None:
            hot[token] = state


def route_partial(
    partial: dict[str, dict[str, Any]],
    normal: dict[str, dict[str, Any]],
    hot: dict[str, dict[str, Any]],
    hot_tokens: set[str],
    accelerated: bool,
    query: QuerySpec,
) -> None:
    if not accelerated:
        merge_partial(normal, partial, query)
        return
    hot_part: dict[str, dict[str, Any]] = {}
    normal_part: dict[str, dict[str, Any]] = {}
    for token, state in partial.items():
        if token in hot_tokens:
            hot_part[token] = state
        else:
            normal_part[token] = state
    merge_partial(normal, normal_part, query)
    merge_partial(hot, hot_part, query)


def merge_all_channels(
    normal: dict[str, dict[str, Any]],
    hot: dict[str, dict[str, Any]],
    query: QuerySpec,
) -> dict[str, dict[str, Any]]:
    merged = {token: state for token, state in normal.items()}
    for token in sorted(hot):
        delta = hot[token]
        if token not in merged:
            merged[token] = new_group_state(delta["key"], query)
        merge_into(merged[token], delta, query)
    return merged


def _mean(state: dict[str, Any]) -> float | None:
    count = int(state["n"])
    if count == 0:
        return None
    return state["sum"] / count


def _variance(state: dict[str, Any]) -> float | None:
    count = int(state["n"])
    if count == 0:
        return None
    if count == 1:
        return 0.0
    mean = state["sum"] / count
    value = state["sumsq"] / count - mean * mean
    if value < 0.0 and math.isfinite(value):
        return 0.0
    return value


def aggregate_value(agg: AggregateSpec, group: dict[str, Any]) -> Any:
    state = group["aggs"][agg.name]
    count = int(state["n"])
    if agg.op == "count":
        return group["rows"] if agg.column is None else count
    if count == 0:
        return None
    if agg.op == "sum":
        return state["sum"]
    if agg.op == "sumsq":
        return state["sumsq"]
    if agg.op == "avg":
        return _mean(state)
    if agg.op == "var":
        return _variance(state)
    return None


def materialize_groups(
    groups: dict[str, dict[str, Any]],
    query: QuerySpec,
) -> list[dict[str, Any]]:
    ordered = sorted(groups.values(), key=lambda state: group_key_sort_key(state["key"]))
    output: list[dict[str, Any]] = []
    for group in ordered:
        aggregates: dict[str, Any] = {}
        for agg in query.aggregates:
            state = group["aggs"][agg.name]
            aggregates[agg.name] = {
                "op": agg.op,
                "column": agg.column,
                "value": aggregate_value(agg, group),
                "count": int(state["n"]),
                "sum": state["sum"],
                "sumsq": state["sumsq"],
                "mean": _mean(state),
                "variance": _variance(state),
            }
        output.append(
            {
                "key": group["key"],
                "rows": int(group["rows"]),
                "aggregates": aggregates,
            }
        )
    return output


def state_digest(groups: dict[str, dict[str, Any]]) -> str:
    return sha256_json(
        [
            {
                "token": token,
                "state": groups[token],
            }
            for token in sorted(groups)
        ]
    )


def partial_ledger_digest(partial: dict[str, dict[str, Any]]) -> str:
    return sha256_json(
        [
            {"token": token, "state": partial[token]}
            for token in sorted(partial, key=lambda item: partial[item]["sort_key"])
        ]
    )


def state_bytes(groups: dict[str, dict[str, Any]], channel: str) -> int:
    total = 0
    for token, state in groups.items():
        total += len(token.encode("utf-8"))
        total += len(channel.encode("utf-8"))
        total += len(canonical_json(state).encode("utf-8"))
        total += 96
    return total


def compare_group_states(
    left: dict[str, dict[str, Any]],
    right: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    left_tokens = set(left)
    right_tokens = set(right)
    if left_tokens != right_tokens:
        missing_left = sorted(right_tokens - left_tokens)
        missing_right = sorted(left_tokens - right_tokens)
        token = (missing_left or missing_right)[0]
        return {
            "group_key": (right.get(token) or left.get(token) or {}).get("key", token),
            "path": "$.groups",
            "left": sorted(left_tokens),
            "right": sorted(right_tokens),
            "missing_in_left": missing_left,
            "missing_in_right": missing_right,
        }
    for token in sorted(left, key=lambda item: left[item]["sort_key"]):
        diff = first_difference(left[token], right[token], f"$.groups[{token}]")
        if diff is not None:
            diff["group_key"] = left[token]["key"]
            diff["exact_intermediate"] = left[token]
            diff["accelerated_intermediate"] = right[token]
            return diff
    return None


def replay_exact(
    batches: Iterable[ColumnBatch],
    query: QuerySpec,
) -> dict[str, dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for batch in batches:
        merge_partial(groups, aggregate_batch_partial(batch, query), query)
    return groups


def row_stream(
    batches: Iterable[ColumnBatch],
    start: int,
    end: int,
) -> Iterator[dict[str, Any]]:
    offset = 0
    for batch in batches:
        batch_start = offset
        batch_end = offset + batch.row_count
        offset = batch_end
        if batch_end <= start:
            continue
        if batch_start >= end:
            break
        local_start = max(0, start - batch_start)
        local_end = min(batch.row_count, end - batch_start)
        for index in range(local_start, local_end):
            yield {name: values[index] for name, values in batch.columns.items()}


def total_rows(batches: Iterable[ColumnBatch]) -> int:
    return sum(batch.row_count for batch in batches)


def build_window_groups(
    batches: tuple[ColumnBatch, ...],
    query: QuerySpec,
) -> tuple[list[dict[str, Any]], int]:
    if query.window is None:
        return [], total_rows(batches)
    rows_total = total_rows(batches)
    length = query.window.length
    step = query.window.step
    windows: list[dict[str, Any]] = []
    start = 0
    index = 0
    while start + length <= rows_total:
        groups = aggregate_rows(row_stream(batches, start, start + length), query)
        windows.append(
            {
                "window_index": index,
                "start_row": start,
                "end_row": start + length,
                "groups": materialize_groups(groups, query),
            }
        )
        start += step
        index += 1
    return windows, rows_total


def build_result_payload(
    query: QuerySpec,
    groups: dict[str, dict[str, Any]],
    batches: tuple[ColumnBatch, ...],
    *,
    actual_mode: str,
    mode_history: list[str],
    consistency: dict[str, Any],
    result_digest: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "query_id": query.query_id,
        "group_key": query.group_key,
        "aggregates": [query.to_dict()["aggregates"][index] for index in range(len(query.aggregates))],
        "actual_mode": actual_mode,
        "mode_history": mode_history,
        "consistency": consistency,
    }
    if query.window is None:
        payload["groups"] = materialize_groups(groups, query)
    else:
        windows, rows_total = build_window_groups(batches, query)
        payload["window"] = query.window.to_dict()
        payload["windows"] = windows
        payload["total_rows"] = rows_total
        payload["incomplete_tail_rows"] = rows_total - (
            windows[-1]["end_row"] if windows else 0
        )
    if result_digest is not None:
        payload["result_digest"] = result_digest
    return payload

