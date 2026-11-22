from __future__ import annotations

import hashlib
from typing import Any, Iterable

import numpy as np

from .compute import (
    annotate_precision,
    apply_filter,
    finalize_metric,
    group_rows,
    make_metric_state,
    merge_metric_states,
    partition_rows,
    window_pairs_for_shard,
)
from .errors import ErrorCode, fail
from .exact import bins_from_pairs
from .model import AggregateSpec, Batch, Column, Experiment, canonical_json
from .planning import RunPlan, ShardPlan, WindowDefinition, build_plan


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _select_metric_state(state: dict[str, Any], index: int) -> dict[str, Any]:
    return {
        "counts": [int(state["counts"][index])],
        "sum_bins": [state["sum_bins"][index]],
        "square_bins": [state["square_bins"][index]],
        "anomalies": {
            key: [int(value[index])] for key, value in state["anomalies"].items()
        },
        "max_abs": [float(state["max_abs"][index])],
        "fixed": {
            "sum_units": [int(state["fixed"]["sum_units"][index])],
            "square_sum_units": [int(state["fixed"]["square_sum_units"][index])],
            "bad_counts": [int(state["fixed"]["bad_counts"][index])],
        },
        "column_type": state["column_type"],
        "partial_mean_bits": [state["partial_mean_bits"][index]],
        "aggregation_order": state["aggregation_order"],
    }


def _select_final_state(state: dict[str, Any], index: int) -> dict[str, Any]:
    selected = _select_metric_state(state, index)
    selected["sum_bins"] = [bins_from_pairs(selected["sum_bins"][0])]
    selected["square_bins"] = [bins_from_pairs(selected["square_bins"][0])]
    return selected


def _build_group_partial(
    batch: Batch,
    shard: ShardPlan,
    experiment: Experiment,
) -> dict[str, Any]:
    query = experiment.query
    mask = apply_filter(batch, query.filters, shard.row_start, shard.row_end)
    selected = np.flatnonzero(mask)
    if selected.size == 0:
        return {
            "kind": "group",
            "shard_id": shard.shard_id,
            "batch_index": shard.batch_index,
            "shards": [shard.shard_id],
            "batches": [shard.batch_index],
            "rows": 0,
            "groups": [],
        }
    group_ids, keys, displays, n_groups = group_rows(
        batch, query.group_keys, shard.row_start, shard.row_end, selected
    )
    group_counts = np.bincount(group_ids, minlength=n_groups)
    metric_states: dict[str, dict[str, Any]] = {}
    for spec in query.aggregates:
        column = batch.columns[spec.column]
        values = column.values[shard.row_start:shard.row_end][selected]
        metric_states[spec.name] = make_metric_state(
            values, column, group_ids, n_groups, experiment.config.fixed_scale
        )
    groups: list[dict[str, Any]] = []
    for group_index in range(n_groups):
        groups.append(
            {
                "key": keys[group_index],
                "display": displays[group_index],
                "rows": int(group_counts[group_index]),
                "metrics": {
                    name: _select_metric_state(state, group_index)
                    for name, state in metric_states.items()
                },
            }
        )
    groups.sort(key=lambda item: item["key"])
    return {
        "kind": "group",
        "shard_id": shard.shard_id,
        "batch_index": shard.batch_index,
        "shards": [shard.shard_id],
        "batches": [shard.batch_index],
        "rows": int(selected.size),
        "groups": groups,
    }


def _build_window_partial(
    batch: Batch,
    shard: ShardPlan,
    experiment: Experiment,
    plan: RunPlan,
) -> dict[str, Any]:
    query = experiment.query
    if query.window is None:
        return {
            "kind": "window",
            "shard_id": shard.shard_id,
            "batch_index": shard.batch_index,
            "shards": [shard.shard_id],
            "batches": [shard.batch_index],
            "windows": [],
        }
    mask = apply_filter(batch, query.filters, shard.row_start, shard.row_end)
    selected = np.flatnonzero(mask)
    if selected.size == 0:
        return {
            "kind": "window",
            "shard_id": shard.shard_id,
            "batch_index": shard.batch_index,
            "shards": [shard.shard_id],
            "batches": [shard.batch_index],
            "windows": [],
        }
    row_pairs, window_ordinals = window_pairs_for_shard(
        batch,
        query,
        shard.row_start,
        shard.row_end,
        selected,
        shard.partition_offsets,
        shard.partition_totals,
        plan.window_base_ordinals,
    )
    if row_pairs.size == 0:
        return {
            "kind": "window",
            "shard_id": shard.shard_id,
            "batch_index": shard.batch_index,
            "shards": [shard.shard_id],
            "batches": [shard.batch_index],
            "windows": [],
        }
    metric_states: dict[str, dict[str, Any]] = {}
    for spec in query.window.aggregates:
        column = batch.columns[spec.column]
        base_values = column.values[shard.row_start:shard.row_end][selected]
        repeated = base_values[row_pairs]
        metric_states[spec.name] = make_metric_state(
            repeated,
            column,
            window_ordinals,
            len(plan.windows),
            experiment.config.fixed_scale,
        )
    first_state = next(iter(metric_states.values()))
    row_counts = first_state["counts"]
    window_by_ordinal = {window.ordinal: window for window in plan.windows}
    covered = sorted({int(x) for x in window_ordinals.tolist()})
    windows: list[dict[str, Any]] = []
    for ordinal in covered:
        definition = window_by_ordinal[ordinal]
        windows.append(
            {
                "window_id": definition.window_id,
                "ordinal": ordinal,
                "partition": definition.partition_display,
                "start": definition.start,
                "end": definition.end,
                "rows": int(row_counts[ordinal]),
                "metrics": {
                    name: _select_metric_state(state, ordinal)
                    for name, state in metric_states.items()
                },
            }
        )
    return {
        "kind": "window",
        "shard_id": shard.shard_id,
        "batch_index": shard.batch_index,
        "shards": [shard.shard_id],
        "batches": [shard.batch_index],
        "windows": windows,
    }


def execute_shard(
    experiment: Experiment,
    plan: RunPlan,
    shard: ShardPlan,
) -> dict[str, Any]:
    batch = experiment.batches[shard.batch_index]
    group_partial = _build_group_partial(batch, shard, experiment)
    window_partial = _build_window_partial(batch, shard, experiment, plan)
    partial = {
        "schema_version": 1,
        "shard_id": shard.shard_id,
        "batch_index": shard.batch_index,
        "shard_digest": shard.shard_digest,
        "content_digest": shard.content_digest,
        "group": group_partial,
        "window": window_partial,
        "logical_rows": {
            "batch_rows": shard.row_count,
            "filtered_rows": group_partial["rows"],
        },
    }
    return partial


def _normalize_group_outputs(
    partials: Iterable[dict[str, Any]],
    experiment: Experiment,
) -> list[dict[str, Any]]:
    query = experiment.query
    grouped: dict[str, dict[str, Any]] = {}
    for partial in partials:
        group_partial = partial["group"]
        for group in group_partial["groups"]:
            key = group["key"]
            state = grouped.setdefault(
                key,
                {
                    "key": key,
                    "display": group["display"],
                    "rows": 0,
                    "metric_states": {spec.name: [] for spec in query.aggregates},
                    "shards": set(),
                    "batches": set(),
                },
            )
            state["rows"] += int(group["rows"])
            state["shards"].update(group_partial["shards"])
            state["batches"].update(group_partial["batches"])
            for spec in query.aggregates:
                state["metric_states"][spec.name].append(group["metrics"][spec.name])
    outputs: list[dict[str, Any]] = []
    for key in sorted(grouped):
        item = grouped[key]
        metrics: dict[str, Any] = {}
        for spec in query.aggregates:
            merged = merge_metric_states(item["metric_states"][spec.name])
            if merged is None:
                raise fail(
                    ErrorCode.RUN_INCOMPLETE,
                    "group metric has no shard state",
                    group_key=key,
                    metric=spec.name,
                )
            metrics[spec.name] = finalize_metric(
                merged,
                spec,
                0,
                int(item["rows"]),
                experiment.config.fixed_scale,
            )
        outputs.append(
            {
                "kind": "group",
                "key": key,
                "group": item["display"],
                "rows": int(item["rows"]),
                "metrics": metrics,
                "sources": {
                    "shards": sorted(item["shards"]),
                    "batches": sorted(item["batches"]),
                },
            }
        )
    annotate_precision(outputs, query.aggregates, experiment.config.tolerance, "group")
    return outputs


def _normalize_window_outputs(
    partials: Iterable[dict[str, Any]],
    experiment: Experiment,
    plan: RunPlan,
) -> list[dict[str, Any]]:
    query = experiment.query
    if query.window is None:
        return []
    window_map = {window.window_id: window for window in plan.windows}
    grouped: dict[str, dict[str, Any]] = {}
    for partial in partials:
        window_partial = partial["window"]
        for window in window_partial["windows"]:
            window_id = window["window_id"]
            state = grouped.setdefault(
                window_id,
                {
                    "window_id": window_id,
                    "ordinal": int(window["ordinal"]),
                    "partition": window["partition"],
                    "start": int(window["start"]),
                    "end": int(window["end"]),
                    "rows": 0,
                    "metric_states": {spec.name: [] for spec in query.window.aggregates},
                    "shards": set(),
                    "batches": set(),
                },
            )
            state["rows"] += int(window["rows"])
            state["shards"].update(window_partial["shards"])
            state["batches"].update(window_partial["batches"])
            for spec in query.window.aggregates:
                state["metric_states"][spec.name].append(window["metrics"][spec.name])
    outputs: list[dict[str, Any]] = []
    for window_id in sorted(grouped, key=lambda item: grouped[item]["ordinal"]):
        item = grouped[window_id]
        definition = window_map[window_id]
        if item["rows"] != query.window.length:
            raise fail(
                ErrorCode.RUN_INCOMPLETE,
                "window does not have its full row coverage",
                window_id=window_id,
                expected_rows=query.window.length,
                actual_rows=item["rows"],
                expected_shards=list(definition.expected_shards),
                observed_shards=sorted(item["shards"]),
            )
        metrics: dict[str, Any] = {}
        for spec in query.window.aggregates:
            merged = merge_metric_states(item["metric_states"][spec.name])
            if merged is None:
                raise fail(
                    ErrorCode.RUN_INCOMPLETE,
                    "window metric has no shard state",
                    window_id=window_id,
                    metric=spec.name,
                )
            metrics[spec.name] = finalize_metric(
                merged,
                spec,
                0,
                int(item["rows"]),
                experiment.config.fixed_scale,
            )
        outputs.append(
            {
                "kind": "window",
                "window_id": window_id,
                "ordinal": item["ordinal"],
                "partition": item["partition"],
                "start": item["start"],
                "end": item["end"],
                "count": int(item["rows"]),
                "metrics": metrics,
                "sources": {
                    "shards": sorted(item["shards"]),
                    "batches": sorted(item["batches"]),
                    "expected_shards": list(definition.expected_shards),
                },
            }
        )
    annotate_precision(outputs, query.window.aggregates, experiment.config.tolerance, "window")
    return outputs


def finalize_results(
    partials: Iterable[dict[str, Any]],
    experiment: Experiment,
    plan: RunPlan,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ordered = sorted(partials, key=lambda partial: partial["shard_id"])
    groups = _normalize_group_outputs(ordered, experiment)
    windows = _normalize_window_outputs(ordered, experiment, plan)
    return groups, windows


def _combined_batch(experiment: Experiment) -> Batch:
    columns: dict[str, Column] = {}
    for name, typ in experiment.columns.items():
        arrays = [batch.columns[name].values for batch in experiment.batches]
        values = np.concatenate(arrays) if arrays else np.asarray([], dtype=np.float64)
        columns[name] = Column(name=name, type=typ, values=values)
    total = sum(batch.row_count for batch in experiment.batches)
    digest = _sha(canonical_json({"batch_indices": [batch.index for batch in experiment.batches], "rows": total}))
    return Batch(index=-1, row_count=total, columns=columns, digest=digest, source="<single-stream-truth>")


def _truth_sources(experiment: Experiment, plan: RunPlan, group_key: str) -> dict[str, list[Any]]:
    shards: set[str] = set()
    batches: set[int] = set()
    for shard in plan.shard_plans:
        batch = experiment.batches[shard.batch_index]
        mask = apply_filter(batch, experiment.query.filters, shard.row_start, shard.row_end)
        selected = np.flatnonzero(mask)
        if selected.size == 0:
            continue
        _ids, keys, _displays, _n = group_rows(
            batch, experiment.query.group_keys, shard.row_start, shard.row_end, selected
        )
        if group_key in keys:
            shards.add(shard.shard_id)
            batches.add(shard.batch_index)
    return {"shards": sorted(shards), "batches": sorted(batches)}


def compute_truth(
    experiment: Experiment,
    plan: RunPlan | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if plan is None:
        plan = build_plan(experiment)
    combined = _combined_batch(experiment)
    query = experiment.query
    mask = apply_filter(combined, query.filters, 0, combined.row_count)
    selected = np.flatnonzero(mask)
    outputs: list[dict[str, Any]] = []
    if selected.size:
        group_ids, keys, displays, n_groups = group_rows(
            combined, query.group_keys, 0, combined.row_count, selected
        )
        counts = np.bincount(group_ids, minlength=n_groups)
        truth_metrics: list[dict[str, Any]] = []
        for spec in query.aggregates:
            column = combined.columns[spec.column]
            values = column.values[selected]
            tone = make_metric_state(
                values, column, group_ids, n_groups, experiment.config.fixed_scale
            )
            truth_metrics.append(tone)
        for group_index in range(n_groups):
            metrics: dict[str, Any] = {}
            for spec, state in zip(query.aggregates, truth_metrics, strict=True):
                metrics[spec.name] = finalize_metric(
                    _select_final_state(state, group_index),
                    spec,
                    0,
                    int(counts[group_index]),
                    experiment.config.fixed_scale,
                )
            sources = _truth_sources(experiment, plan, keys[group_index])
            outputs.append(
                {
                    "kind": "group",
                    "key": keys[group_index],
                    "group": displays[group_index],
                    "rows": int(counts[group_index]),
                    "metrics": metrics,
                    "sources": sources,
                }
            )
        outputs.sort(key=lambda item: item["key"])
    annotate_precision(outputs, query.aggregates, experiment.config.tolerance, "group")
    windows: list[dict[str, Any]] = []
    if query.window is not None and selected.size:
        partitions = sorted(plan.partition_totals)
        base_ordinals = dict(plan.window_base_ordinals)
        offsets = {key: 0 for key in partitions}
        totals = dict(plan.partition_totals)
        row_pairs, ordinals = window_pairs_for_shard(
            combined,
            query,
            0,
            combined.row_count,
            selected,
            offsets,
            totals,
            base_ordinals,
        )
        if row_pairs.size:
            metric_states: dict[str, dict[str, Any]] = {}
            for spec in query.window.aggregates:
                column = combined.columns[spec.column]
                values = column.values[selected][row_pairs]
                metric_states[spec.name] = make_metric_state(
                    values,
                    column,
                    ordinals,
                    len(plan.windows),
                    experiment.config.fixed_scale,
                )
            first_state = next(iter(metric_states.values()))
            row_counts = first_state["counts"]
            by_ordinal = {window.ordinal: window for window in plan.windows}
            for ordinal in sorted({int(x) for x in ordinals.tolist()}):
                definition = by_ordinal[ordinal]
                metrics: dict[str, Any] = {}
                for spec, state in zip(
                    query.window.aggregates, metric_states.values(), strict=True
                ):
                    metrics[spec.name] = finalize_metric(
                        _select_final_state(state, ordinal),
                        spec,
                        0,
                        int(row_counts[ordinal]),
                        experiment.config.fixed_scale,
                    )
                windows.append(
                    {
                        "kind": "window",
                        "window_id": definition.window_id,
                        "ordinal": ordinal,
                        "partition": definition.partition_display,
                        "start": definition.start,
                        "end": definition.end,
                        "count": int(row_counts[ordinal]),
                        "metrics": metrics,
                        "sources": {
                            "shards": list(definition.expected_shards),
                            "batches": list(definition.batch_indices),
                            "expected_shards": list(definition.expected_shards),
                        },
                    }
                )
            windows.sort(key=lambda item: item["ordinal"])
    annotate_precision(windows, query.window.aggregates if query.window else (), experiment.config.tolerance, "window")
    return outputs, windows


def comparison_payload(kind: str, output: dict[str, Any]) -> dict[str, Any]:
    metrics = {
        name: {
            "value": metric["value"],
            "value_bits": metric["value_bits"],
            "count": metric["count"],
            "error_spectrum": metric["error_spectrum"],
            "fixed_value_bits": metric["fixed"]["value_bits"],
            "fixed_available": metric["fixed"]["available"],
            "fixed_overflow_count": metric["fixed"]["overflow_count"],
            "precision_sensitive": metric.get("precision_sensitive", False),
            "precision_evidence": metric.get("precision_evidence", []),
        }
        for name, metric in output["metrics"].items()
    }
    if kind == "group":
        return {
            "kind": kind,
            "key": output["key"],
            "rows": output["rows"],
            "metrics": metrics,
        }
    return {
        "kind": kind,
        "window_id": output["window_id"],
        "partition": output["partition"],
        "start": output["start"],
        "end": output["end"],
        "count": output["count"],
        "metrics": metrics,
    }


def compare_results(
    persisted_groups: list[dict[str, Any]],
    persisted_windows: list[dict[str, Any]],
    truth_groups: list[dict[str, Any]],
    truth_windows: list[dict[str, Any]],
) -> dict[str, Any]:
    for kind, persisted, truth in (
        ("group", persisted_groups, truth_groups),
        ("window", persisted_windows, truth_windows),
    ):
        persisted_map = {
            (item.get("key") if kind == "group" else item.get("window_id")): item
            for item in persisted
        }
        truth_map = {
            (item.get("key") if kind == "group" else item.get("window_id")): item
            for item in truth
        }
        for identity in sorted(set(persisted_map) | set(truth_map)):
            left = persisted_map.get(identity)
            right = truth_map.get(identity)
            if left is None or right is None:
                source = left or right
                return {
                    "ok": False,
                    "first_diff": {
                        "kind": kind,
                        "identity": identity,
                        "reason": "missing_result",
                        "sources": source.get("sources", {}) if source else {},
                    },
                }
            left_payload = comparison_payload(kind, left)
            right_payload = comparison_payload(kind, right)
            if canonical_json(left_payload) != canonical_json(right_payload):
                metric_name = None
                for name in sorted(set(left_payload["metrics"]) | set(right_payload["metrics"])):
                    if canonical_json(left_payload["metrics"].get(name)) != canonical_json(
                        right_payload["metrics"].get(name)
                    ):
                        metric_name = name
                        break
                return {
                    "ok": False,
                    "first_diff": {
                        "kind": kind,
                        "identity": identity,
                        "metric": metric_name,
                        "persisted": left_payload,
                        "truth": right_payload,
                        "sources": {
                            "persisted": left.get("sources", {}),
                            "truth": right.get("sources", {}),
                        },
                    },
                }
    return {
        "ok": True,
        "groups_compared": len(truth_groups),
        "windows_compared": len(truth_windows),
    }
