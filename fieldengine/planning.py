from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from .compute import apply_filter, group_rows, partition_rows
from .model import Batch, Experiment, canonical_json


BASE_SHARD_OVERHEAD_BYTES = 1024 * 1024
EXACT_ACCUMULATOR_BYTES = 48
GROUP_STATE_BYTES = 96


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def split_ranges(row_count: int, shard_count: int) -> list[tuple[int, int]]:
    if row_count <= 0:
        return []
    effective = max(1, min(row_count, shard_count))
    return [(row_count * i // effective, row_count * (i + 1) // effective) for i in range(effective)]


@dataclass(frozen=True)
class ShardPlan:
    shard_id: str
    ordinal: int
    batch_index: int
    shard_index: int
    shard_count: int
    row_start: int
    row_end: int
    row_count: int
    shard_digest: str
    content_digest: str
    filtered_count: int
    group_cardinality: int
    partition_counts: dict[str, int]
    partition_offsets: dict[str, int]
    partition_totals: dict[str, int]
    estimated_bytes: int
    estimate: dict[str, Any]

    @property
    def batch_id(self) -> str:
        return f"b{self.batch_index:06d}"


@dataclass(frozen=True)
class WindowDefinition:
    ordinal: int
    window_id: str
    partition_key: str
    partition_display: dict[str, Any]
    start: int
    end: int
    expected_shards: tuple[str, ...]
    batch_indices: tuple[int, ...]


@dataclass(frozen=True)
class RunPlan:
    mode: str
    mode_decision: dict[str, Any]
    shard_plans: tuple[ShardPlan, ...]
    windows: tuple[WindowDefinition, ...]
    window_base_ordinals: dict[str, int]
    partition_totals: dict[str, int]
    partition_displays: dict[str, dict[str, Any]]
    plan_digest: str

    @property
    def total_shards(self) -> int:
        return len(self.shard_plans)


def _estimate_shard(
    batch: Batch,
    experiment: Experiment,
    row_start: int,
    row_end: int,
    filtered_count: int,
    shard_group_cardinality: int,
    shard_partition_cardinality: int,
) -> tuple[int, dict[str, Any]]:
    rows = row_end - row_start
    referenced = set(experiment.query.group_keys)
    referenced.update(spec.column for spec in experiment.query.aggregates)
    referenced.update(spec.column for spec in (experiment.query.window.aggregates if experiment.query.window else ()))
    referenced.update(experiment.query.window.partition_by if experiment.query.window else ())
    referenced.update(filt.column for filt in experiment.query.filters)
    column_bytes = 0
    type_bytes = {"int": 8, "float": 8, "bool": 1}
    for name in referenced:
        column_bytes += type_bytes[batch.columns[name].type] * rows
    mask_bytes = rows
    projection_bytes = 8 * filtered_count * max(
        1, len({spec.column for spec in experiment.query.aggregates})
    )
    group_metric_count = max(1, len(experiment.query.aggregates))
    group_bytes = (
        shard_group_cardinality
        * (GROUP_STATE_BYTES + 24 * len(experiment.query.group_keys))
        * group_metric_count
    )
    exact_workspace_bytes = (
        filtered_count * 176 * max(1, len(experiment.query.aggregates))
    )
    fixed_workspace_bytes = filtered_count * 16 * (
        len(experiment.query.aggregates)
        + (len(experiment.query.window.aggregates) if experiment.query.window else 0)
    )
    window_bytes = 0
    window_workspace_bytes = 0
    window_depth = 1
    if experiment.query.window is not None:
        window = experiment.query.window
        window_depth = max(1, math.ceil(window.length / window.step))
        window_bytes = (
            filtered_count
            * window_depth
            * max(1, len(window.aggregates))
            * EXACT_ACCUMULATOR_BYTES
        )
        window_workspace_bytes = (
            filtered_count
            * window_depth
            * max(1, len(window.aggregates))
            * 176
        )
        window_bytes += (
            shard_partition_cardinality
            * GROUP_STATE_BYTES
            * max(1, len(window.aggregates))
        )
    partial_serialization_bytes = (
        max(1, shard_group_cardinality) * 256
        + max(1, filtered_count) * window_depth * 8
    )
    total = (
        BASE_SHARD_OVERHEAD_BYTES
        + column_bytes
        + mask_bytes
        + projection_bytes
        + group_bytes
        + exact_workspace_bytes
        + fixed_workspace_bytes
        + window_bytes
        + window_workspace_bytes
        + partial_serialization_bytes
    )
    estimate = {
        "formula": "base + referenced_column_bytes + filter_mask + projection + group_state(cardinality) + exact_accumulator_workspace + fixed_workspace + window_buffer(rows*depth*metrics) + window_accumulator_workspace + partial_serialization",
        "observations": {
            "row_count": rows,
            "filtered_count": filtered_count,
            "column_types": {name: batch.columns[name].type for name in sorted(referenced)},
            "group_cardinality": shard_group_cardinality,
            "window_depth": window_depth,
            "window_partition_cardinality": shard_partition_cardinality,
        },
        "breakdown": {
            "base": BASE_SHARD_OVERHEAD_BYTES,
            "columns": column_bytes,
            "filter_mask": mask_bytes,
            "projection": projection_bytes,
            "group_state": group_bytes,
            "exact_accumulator_workspace": exact_workspace_bytes,
            "fixed_workspace": fixed_workspace_bytes,
            "window_buffer": window_bytes,
            "window_accumulator_workspace": window_workspace_bytes,
            "partial_serialization": partial_serialization_bytes,
        },
        "total_bytes": total,
        "total_mib": total / (1024 * 1024),
    }
    return total, estimate


def build_plan(experiment: Experiment) -> RunPlan:
    query = experiment.query
    config = experiment.config
    cardinalities: dict[int, int] = {}
    triggers: list[dict[str, Any]] = []
    for batch in experiment.batches:
        if query.group_keys:
            selected = np.arange(batch.row_count, dtype=np.int64)
            _ids, _keys, _displays, cardinality = group_rows(
                batch, query.group_keys, 0, batch.row_count, selected
            )
        else:
            cardinality = 1 if batch.row_count else 0
        cardinalities[batch.index] = cardinality
        row_trigger = batch.row_count > config.adaptive.row_threshold
        cardinality_trigger = cardinality > config.adaptive.group_cardinality_threshold
        if row_trigger or cardinality_trigger:
            triggers.append(
                {
                    "batch_index": batch.index,
                    "row_count": batch.row_count,
                    "group_cardinality": cardinality,
                    "row_threshold_exceeded": row_trigger,
                    "group_cardinality_threshold_exceeded": cardinality_trigger,
                }
            )
    mode = "coarse" if triggers else "fine"
    fine_count = config.shards_per_batch
    coarse_count = max(1, math.ceil(fine_count / config.adaptive.coarse_divisor))
    requested_count = fine_count if mode == "fine" else coarse_count
    decision = {
        "mode": mode,
        "fine_shards_per_batch": fine_count,
        "coarse_shards_per_batch": coarse_count,
        "selected_shards_per_batch": requested_count,
        "rule": "coarse if any batch row_count > row_threshold OR group cardinality > group_cardinality_threshold; otherwise fine",
        "triggers": triggers,
        "rejected_mode": "coarse" if mode == "fine" else "fine",
        "rejected_reason": (
            "no threshold exceeded; fine shards preserve interaction latency"
            if mode == "fine"
            else "row or group cardinality threshold exceeded; merged shards reduce per-task overhead"
        ),
        "batch_group_cardinality": [
            {"batch_index": index, "cardinality": cardinalities[index]}
            for index in sorted(cardinalities)
        ],
    }
    raw_shards: list[dict[str, Any]] = []
    ordinal = 0
    for batch in experiment.batches:
        ranges = split_ranges(batch.row_count, requested_count)
        for shard_index, (row_start, row_end) in enumerate(ranges):
            shard_id = f"b{batch.index:06d}-s{shard_index:04d}"
            descriptor = {
                "shard_id": shard_id,
                "batch_index": batch.index,
                "row_count": batch.row_count,
                "shard_index": shard_index,
                "shard_count": len(ranges),
                "row_start": row_start,
                "row_end": row_end,
                "selected_shards_per_batch": requested_count,
            }
            shard_digest = _sha(canonical_json(descriptor))
            mask = apply_filter(batch, query.filters, row_start, row_end)
            selected = np.flatnonzero(mask)
            filtered_count = int(selected.size)
            if query.group_keys and filtered_count:
                _gids, _keys, _displays, group_cardinality = group_rows(
                    batch, query.group_keys, row_start, row_end, selected
                )
            else:
                group_cardinality = 0
            partition_counts: dict[str, int] = {}
            partition_cardinality = 0
            if query.window is not None:
                pids, pkeys, _pdisplays, partition_cardinality = partition_rows(
                    batch,
                    query.window.partition_by,
                    row_start,
                    row_end,
                    selected,
                )
                if filtered_count:
                    counts = np.bincount(pids, minlength=partition_cardinality)
                    partition_counts = {
                        pkeys[i]: int(counts[i]) for i in range(partition_cardinality)
                    }
            estimated, estimate = _estimate_shard(
                batch,
                experiment,
                row_start,
                row_end,
                filtered_count,
                group_cardinality,
                partition_cardinality,
            )
            content_digest = _sha(
                canonical_json(
                    {
                        "batch_content_digest": batch.digest,
                        "shard_digest": shard_digest,
                        "filtered_count": filtered_count,
                    }
                )
            )
            raw_shards.append(
                {
                    "shard_id": shard_id,
                    "ordinal": ordinal,
                    "batch": batch,
                    "batch_index": batch.index,
                    "shard_index": shard_index,
                    "shard_count": len(ranges),
                    "row_start": row_start,
                    "row_end": row_end,
                    "shard_digest": shard_digest,
                    "content_digest": content_digest,
                    "filtered_count": filtered_count,
                    "group_cardinality": group_cardinality,
                    "partition_counts": partition_counts,
                    "estimated_bytes": estimated,
                    "estimate": estimate,
                }
            )
            ordinal += 1
    partition_totals: dict[str, int] = {}
    partition_displays: dict[str, dict[str, Any]] = {}
    if query.window is not None:
        for item in raw_shards:
            for key, count in item["partition_counts"].items():
                partition_totals[key] = partition_totals.get(key, 0) + int(count)
        for batch in experiment.batches:
            selected = np.arange(batch.row_count, dtype=np.int64)
            if not selected.size:
                continue
            _pids, pkeys, pdisplays, _n = partition_rows(
                batch, query.window.partition_by, 0, batch.row_count, selected
            )
            for key, display in zip(pkeys, pdisplays, strict=True):
                partition_displays.setdefault(key, display)
    offsets_so_far: dict[str, int] = {key: 0 for key in partition_totals}
    shard_plans: list[ShardPlan] = []
    for item in raw_shards:
        offsets = {
            key: offsets_so_far.get(key, 0) for key in item["partition_counts"]
        }
        for key, count in item["partition_counts"].items():
            offsets_so_far[key] = offsets_so_far.get(key, 0) + int(count)
        shard_plans.append(
            ShardPlan(
                shard_id=item["shard_id"],
                ordinal=item["ordinal"],
                batch_index=item["batch_index"],
                shard_index=item["shard_index"],
                shard_count=item["shard_count"],
                row_start=item["row_start"],
                row_end=item["row_end"],
                row_count=item["row_end"] - item["row_start"],
                shard_digest=item["shard_digest"],
                content_digest=item["content_digest"],
                filtered_count=item["filtered_count"],
                group_cardinality=item["group_cardinality"],
                partition_counts=item["partition_counts"],
                partition_offsets=offsets,
                partition_totals={key: partition_totals[key] for key in item["partition_counts"]},
                estimated_bytes=item["estimated_bytes"],
                estimate=item["estimate"],
            )
        )
    windows: list[WindowDefinition] = []
    window_base_ordinals: dict[str, int] = {}
    if query.window is not None:
        window = query.window
        next_ordinal = 0
        for key in sorted(partition_totals):
            total = partition_totals[key]
            max_k = (total - window.length) // window.step
            if max_k < 0:
                continue
            window_base_ordinals[key] = next_ordinal
            for k in range(max_k + 1):
                start = k * window.step
                end = start + window.length
                expected: list[str] = []
                batches: set[int] = set()
                for shard in shard_plans:
                    count = int(shard.partition_counts.get(key, 0))
                    if count <= 0:
                        continue
                    shard_start = int(shard.partition_offsets[key])
                    shard_end = shard_start + count
                    if shard_start < end and shard_end > start:
                        expected.append(shard.shard_id)
                        batches.add(shard.batch_index)
                windows.append(
                    WindowDefinition(
                        ordinal=next_ordinal,
                        window_id=f"w{next_ordinal:06d}",
                        partition_key=key,
                        partition_display=partition_displays.get(key, {}),
                        start=start,
                        end=end,
                        expected_shards=tuple(expected),
                        batch_indices=tuple(sorted(batches)),
                    )
                )
                next_ordinal += 1
    plan_payload = {
        "mode_decision": decision,
        "shards": [
            {
                "shard_id": shard.shard_id,
                "shard_digest": shard.shard_digest,
                "content_digest": shard.content_digest,
                "row_start": shard.row_start,
                "row_end": shard.row_end,
                "estimated_bytes": shard.estimated_bytes,
            }
            for shard in shard_plans
        ],
        "windows": [
            {
                "window_id": window.window_id,
                "partition_key": window.partition_key,
                "start": window.start,
                "end": window.end,
                "expected_shards": list(window.expected_shards),
            }
            for window in windows
        ],
    }
    return RunPlan(
        mode=mode,
        mode_decision=decision,
        shard_plans=tuple(shard_plans),
        windows=tuple(windows),
        window_base_ordinals=window_base_ordinals,
        partition_totals=partition_totals,
        partition_displays=partition_displays,
        plan_digest=_sha(canonical_json(plan_payload)),
    )
