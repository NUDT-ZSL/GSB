from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP, localcontext
from typing import Any, Iterable

from .models import DiagnosticConfig, StorageConfig
from .segments import Segment, estimate_lookup


TRUTH_PLACES = Decimal("0.000000000000000001")


def _round_units(value: Decimal, scale: int) -> int:
    scaled = value * (Decimal(10) ** scale)
    return int(scaled.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _fixed_string(units: int, scale: int) -> str:
    return format(Decimal(units).scaleb(-scale), f".{scale}f")


def _decimal_string(value: Decimal, places: Decimal = TRUTH_PLACES) -> str:
    with localcontext() as context:
        context.prec = 60
        return format(value.quantize(places, rounding=ROUND_HALF_UP), "f")


def _ratio_units(numerator: int, denominator: int, scale: int) -> int:
    if denominator == 0:
        return 0
    with localcontext() as context:
        context.prec = 60
        return _round_units(Decimal(numerator) / Decimal(denominator), scale)


@dataclass(frozen=True)
class LayerStats:
    occurrences: int
    distinct_keys: int
    repeated_keys: int
    max_occurrences: int
    max_occurrence_key: str | None
    tombstone_count: int
    materialized_bytes: int
    live_bytes: int
    reclaimable_bytes: int


def collect_layer_stats(segments: Iterable[Segment]) -> LayerStats:
    records = [record for segment in segments for record in segment.records]
    if not records:
        return LayerStats(0, 0, 0, 0, None, 0, 0, 0, 0)
    occurrences: dict[str, int] = {}
    weights: dict[str, int] = {}
    latest: dict[str, Any] = {}
    materialized = 0
    tombstones = 0
    for record in records:
        occurrences[record.key] = occurrences.get(record.key, 0) + 1
        weights[record.key] = weights.get(record.key, 0) + record.payload_bytes
        materialized += record.payload_bytes
        if record.is_tombstone:
            tombstones += 1
        previous = latest.get(record.key)
        if previous is None or record.seq > previous.seq:
            latest[record.key] = record
    live = sum(record.payload_bytes for record in latest.values() if not record.is_tombstone)
    max_key = min(
        occurrences,
        key=lambda key: (-occurrences[key], -weights[key], key),
    )
    return LayerStats(
        occurrences=len(records),
        distinct_keys=len(occurrences),
        repeated_keys=sum(1 for count in occurrences.values() if count > 1),
        max_occurrences=occurrences[max_key],
        max_occurrence_key=max_key,
        tombstone_count=tombstones,
        materialized_bytes=materialized,
        live_bytes=live,
        reclaimable_bytes=max(0, materialized - live),
    )


def _conversion_step(
    quantity: str,
    raw: Decimal | int,
    quantized: Decimal | int,
    scale: int,
    rounding: str,
    *,
    feeds_amplification: bool,
) -> dict[str, Any]:
    raw_decimal = Decimal(raw)
    quantized_decimal = Decimal(quantized)
    return {
        "quantity": quantity,
        "raw_value": _decimal_string(raw_decimal, Decimal("0.000000000000000000")),
        "converted_value": _decimal_string(quantized_decimal, Decimal("0.000000000000000000")),
        "scale": scale,
        "rounding": rounding,
        "rounding_remainder": _decimal_string(raw_decimal - quantized_decimal),
        "feeds_write_amplification": feeds_amplification,
    }


def _precision_fields(
    fixed_value: Decimal,
    truth_value: Decimal,
) -> dict[str, str]:
    absolute = abs(fixed_value - truth_value)
    if truth_value == 0:
        relative = Decimal(0) if absolute == 0 else Decimal(1)
    else:
        relative = absolute / abs(truth_value)
    return {
        "absolute_deviation": _decimal_string(absolute),
        "relative_deviation": _decimal_string(relative),
    }


def _layer_quantities(
    stats: LayerStats,
    compression_raw: Decimal,
    config: DiagnosticConfig,
    level: int,
) -> dict[str, Any]:
    scale = config.fixed_scale
    unit = Decimal(1) / (Decimal(10) ** scale)
    half_unit = unit / 2
    comp_units = _round_units(compression_raw, scale)
    comp_fixed = Decimal(comp_units).scaleb(-scale)
    duplicate_units = _ratio_units(
        stats.occurrences - stats.distinct_keys,
        stats.occurrences,
        scale,
    )
    tombstone_units = _ratio_units(stats.tombstone_count, stats.occurrences, scale)
    denominator = max(1, stats.live_bytes)
    with localcontext() as context:
        context.prec = 60
        contribution_fixed = Decimal(
            _round_units(
                Decimal(stats.materialized_bytes) * comp_fixed / Decimal(denominator),
                scale,
            )
        ).scaleb(-scale)
        contribution_truth = (
            Decimal(stats.materialized_bytes) * compression_raw / Decimal(denominator)
        )
        gain_fixed = Decimal(
            _round_units(Decimal(stats.reclaimable_bytes) * comp_fixed, scale)
        ).scaleb(-scale)
        gain_truth = Decimal(stats.reclaimable_bytes) * compression_raw
    comp_rounding_error = abs(compression_raw - comp_fixed)
    contribution_bound = (
        Decimal(stats.materialized_bytes) / Decimal(denominator) * comp_rounding_error
        + half_unit
    )
    gain_bound = Decimal(stats.reclaimable_bytes) * comp_rounding_error + half_unit
    contribution_precision = _precision_fields(contribution_fixed, contribution_truth)
    gain_precision = _precision_fields(gain_fixed, gain_truth)
    rounding = "ROUND_HALF_UP"
    error_chain = [
        _conversion_step(
            "compression_ratio",
            compression_raw,
            comp_fixed,
            scale,
            rounding,
            feeds_amplification=True,
        ),
        _conversion_step(
            "duplicate_rate",
            Decimal(stats.occurrences - stats.distinct_keys)
            / Decimal(max(1, stats.occurrences)),
            Decimal(duplicate_units).scaleb(-scale),
            scale,
            rounding,
            feeds_amplification=False,
        ),
        _conversion_step(
            "tombstone_ratio",
            Decimal(stats.tombstone_count) / Decimal(max(1, stats.occurrences)),
            Decimal(tombstone_units).scaleb(-scale),
            scale,
            rounding,
            feeds_amplification=False,
        ),
        _conversion_step(
            "wa_contribution",
            contribution_truth,
            contribution_fixed,
            scale,
            rounding,
            feeds_amplification=True,
        ),
        _conversion_step(
            "merge_gain",
            gain_truth,
            gain_fixed,
            scale,
            rounding,
            feeds_amplification=True,
        ),
    ]
    for step in error_chain:
        step["layer"] = level
    return {
        "layer": level,
        "compression_ratio_raw": format(compression_raw, "f"),
        "compression_ratio_quantized": _fixed_string(comp_units, scale),
        "compression_ratio_scale": scale,
        "compression_rounding": rounding,
        "compression_rounding_remainder": _decimal_string(
            compression_raw - comp_fixed
        ),
        "duplicate_rate": _fixed_string(duplicate_units, scale),
        "tombstone_ratio": _fixed_string(tombstone_units, scale),
        "wa_contribution": {
            "fixed_path": _fixed_string(_round_units(contribution_fixed, scale), scale),
            "truth_path": _decimal_string(contribution_truth),
            **contribution_precision,
            "error_bound": _decimal_string(contribution_bound),
        },
        "merge_gain": {
            "fixed_path": _fixed_string(_round_units(gain_fixed, scale), scale),
            "truth_path": _decimal_string(gain_truth),
            **gain_precision,
            "error_bound": _decimal_string(gain_bound),
        },
        "_fixed_contribution": contribution_fixed,
        "_truth_contribution": contribution_truth,
        "_fixed_gain": gain_fixed,
        "_truth_gain": gain_truth,
        "_wa_error_bound": contribution_bound,
        "_gain_error_bound": gain_bound,
        "error_source_chain": error_chain,
    }


def _segment_gain(
    segment: Segment,
    compression_raw: Decimal,
    config: DiagnosticConfig,
) -> tuple[Decimal, Decimal]:
    quantities = _layer_quantities(
        collect_layer_stats((segment,)),
        compression_raw,
        config,
        0,
    )
    return quantities["_fixed_gain"], quantities["_truth_gain"]


def segment_merge_gain(
    segment: Segment,
    compression_raw: Decimal,
    config: DiagnosticConfig,
) -> tuple[Decimal, Decimal]:
    return _segment_gain(segment, compression_raw, config)


def _comparison(
    *,
    kind: str,
    levels: list[int],
    fixed_values: list[Decimal],
    truth_values: list[Decimal],
    labels: list[str],
    config: DiagnosticConfig,
    decision_dependency: bool,
) -> dict[str, Any]:
    tolerance = config.tolerance_decimal
    fixed_delta = abs(fixed_values[0] - fixed_values[1])
    truth_delta = abs(truth_values[0] - truth_values[1])
    fixed_order = 0 if fixed_values[0] == fixed_values[1] else (-1 if fixed_values[0] < fixed_values[1] else 1)
    truth_order = 0 if truth_values[0] == truth_values[1] else (-1 if truth_values[0] < truth_values[1] else 1)
    within = fixed_delta < tolerance or truth_delta < tolerance
    return {
        "kind": kind,
        "levels": levels,
        "candidates": labels,
        "fixed_delta": _decimal_string(fixed_delta),
        "truth_delta": _decimal_string(truth_delta),
        "tolerance": config.float_tolerance,
        "within_tolerance": within,
        "fixed_order": fixed_order,
        "truth_order": truth_order,
        "order_changed": fixed_order != truth_order,
        "decision_dependency": decision_dependency,
        "precision_sensitive": within and decision_dependency,
    }


def _cross_level_repetition(
    level_segments: dict[int, tuple[Segment, ...]],
) -> dict[str, Any]:
    by_key: dict[str, dict[str, Any]] = {}
    for level in sorted(level_segments):
        for segment in level_segments[level]:
            for record in segment.records:
                entry = by_key.setdefault(
                    record.key,
                    {
                        "key": record.key,
                        "level_counts": {},
                        "occurrences": 0,
                        "byte_weight": 0,
                    },
                )
                level_key = str(level)
                entry["level_counts"][level_key] = (
                    entry["level_counts"].get(level_key, 0) + 1
                )
                entry["occurrences"] += 1
                entry["byte_weight"] += record.payload_bytes
    keys = [by_key[key] for key in sorted(by_key)]
    repeated = [entry for entry in keys if entry["occurrences"] > 1]
    return {
        "distinct_keys": len(keys),
        "repeated_keys": len(repeated),
        "cross_level_repeated_keys": sum(
            1 for entry in keys if len(entry["level_counts"]) > 1
        ),
        "max_occurrences": max((entry["occurrences"] for entry in keys), default=0),
        "keys": keys,
    }


def diagnose_run(
    level_segments: dict[int, tuple[Segment, ...]],
    storage: StorageConfig,
    diagnostic: DiagnosticConfig,
    *,
    query_key: str | None = None,
    level_views: dict[int, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    layer_items: list[dict[str, Any]] = []
    all_segments = [
        segment
        for level in sorted(level_segments)
        for segment in level_segments[level]
    ]
    for level in range(storage.level_count):
        segments = level_segments.get(level, ())
        stats = collect_layer_stats(segments)
        compression = storage.compression_decimal(level)
        quantities = _layer_quantities(stats, compression, diagnostic, level)
        layer_items.append(
            {
                "level": level,
                "segment_count": len(segments),
                "trigger_threshold": storage.level_trigger_thresholds[level],
                "triggered": len(segments) >= storage.level_trigger_thresholds[level],
                "segment_ids": [segment.meta.uid for segment in segments],
                "key_stats": {
                    "occurrences": stats.occurrences,
                    "distinct_keys": stats.distinct_keys,
                    "repeated_keys": stats.repeated_keys,
                    "max_occurrences": stats.max_occurrences,
                    "max_occurrence_key": stats.max_occurrence_key,
                    "tombstone_count": stats.tombstone_count,
                    "materialized_bytes": stats.materialized_bytes,
                    "live_bytes": stats.live_bytes,
                    "reclaimable_bytes": stats.reclaimable_bytes,
                },
                **quantities,
                "precision_sensitive": False,
                "precision_sensitive_reasons": [],
            }
        )

    fixed_total = sum(
        (item["_fixed_contribution"] for item in layer_items),
        Decimal(0),
    )
    truth_total = sum(
        (item["_truth_contribution"] for item in layer_items),
        Decimal(0),
    )
    scale = diagnostic.fixed_scale
    unit = Decimal(1) / (Decimal(10) ** scale)
    addition_bound = unit / 2 * max(0, len(layer_items) - 1)
    wa_bound = sum(
        (item["_wa_error_bound"] for item in layer_items),
        Decimal(0),
    ) + addition_bound
    gain_bound = sum(
        (item["_gain_error_bound"] for item in layer_items),
        Decimal(0),
    )
    total_precision = _precision_fields(fixed_total, truth_total)

    cumulative: list[dict[str, Any]] = []
    running = Decimal(0)
    for item in layer_items:
        previous = running
        running += item["_fixed_contribution"]
        cumulative.append(
            {
                "step": item["level"],
                "operation": "add",
                "operand_fixed": item["wa_contribution"]["fixed_path"],
                "previous_fixed": _fixed_string(_round_units(previous, scale), scale),
                "running_fixed": _fixed_string(_round_units(running, scale), scale),
                "fixed_rounding_remainder": _decimal_string(running - Decimal(_fixed_string(_round_units(running, scale), scale))),
            }
        )

    comparisons: list[dict[str, Any]] = []
    sensitive_levels: set[int] = set()
    for index in range(len(layer_items) - 1):
        left = layer_items[index]
        right = layer_items[index + 1]
        dependency = bool(left["triggered"] or right["triggered"])
        comparison = _comparison(
            kind="layer_pair",
            levels=[index, index + 1],
            fixed_values=[left["_fixed_gain"], right["_fixed_gain"]],
            truth_values=[left["_truth_gain"], right["_truth_gain"]],
            labels=[
                f"L{index}:merge_gain",
                f"L{index + 1}:merge_gain",
            ],
            config=diagnostic,
            decision_dependency=dependency,
        )
        comparisons.append(comparison)
        if comparison["precision_sensitive"]:
            sensitive_levels.update(comparison["levels"])
            reason = (
                f"L{index}/L{index + 1} 增益差小于容差且触发层合并排序依赖该差值"
            )
            left["precision_sensitive_reasons"].append(reason)
            right["precision_sensitive_reasons"].append(reason)

    for level in range(storage.level_count):
        segments = level_segments.get(level, ())
        if len(segments) < 2:
            continue
        scored = [
            (
                *_segment_gain(
                    segment,
                    storage.compression_decimal(level),
                    diagnostic,
                ),
                segment,
            )
            for segment in segments
        ]
        scored.sort(key=lambda item: (-item[0], item[2].meta.segment_no))
        fixed_values = [scored[0][0], scored[1][0]]
        truth_values = [scored[0][1], scored[1][1]]
        triggered = layer_items[level]["triggered"]
        comparison = _comparison(
            kind="segment_pair",
            levels=[level],
            fixed_values=fixed_values,
            truth_values=truth_values,
            labels=[scored[0][2].meta.uid, scored[1][2].meta.uid],
            config=diagnostic,
            decision_dependency=triggered,
        )
        comparisons.append(comparison)
        if comparison["precision_sensitive"]:
            sensitive_levels.add(level)
            layer_items[level]["precision_sensitive_reasons"].append(
                f"L{level} 前两个段候选增益差小于容差且层已触发"
            )

    for level in sensitive_levels:
        layer_items[level]["precision_sensitive"] = True

    if query_key is None:
        keys = sorted(
            {
                record.key
                for segment in all_segments
                for record in segment.records
            }
        )
        query_key = keys[len(keys) // 2] if keys else None
    lookup = (
        estimate_lookup(
            query_key,
            all_segments,
            sample_ratio=diagnostic.sample_ratio,
        )
        if query_key is not None
        else {
            "key": None,
            "mode": "empty",
            "segments_observed": 0,
            "estimated_segment_reads": 0,
            "estimated_block_reads": 0,
            "observations": [],
            "deterministic": True,
        }
    )

    for item in layer_items:
        for key in list(item):
            if key.startswith("_"):
                del item[key]
    output: dict[str, Any] = {
        "schema": "wadiag.diagnosis.v1",
        "layers": layer_items,
        "whole_store_write_amplification": {
            "fixed_path": _fixed_string(_round_units(fixed_total, scale), scale),
            "truth_path": _decimal_string(truth_total),
            **total_precision,
            "error_bound": _decimal_string(wa_bound),
            "accumulation_order": list(range(storage.level_count)),
            "cumulative_steps": cumulative,
        },
        "precision": {
            "fixed_scale": scale,
            "rounding": "ROUND_HALF_UP",
            "tolerance": diagnostic.float_tolerance,
            "error_budget": diagnostic.error_budget,
            "error_budget_exceeded": wa_bound > diagnostic.error_budget_decimal,
            "gain_error_bound": _decimal_string(gain_bound),
            "comparisons": comparisons,
            "precision_sensitive": any(
                comparison["precision_sensitive"] for comparison in comparisons
            ),
            "precision_sensitive_levels": sorted(sensitive_levels),
        },
        "cross_level_key_repetition": _cross_level_repetition(level_segments),
        "lookup_cost": lookup,
    }
    if level_views is not None:
        output["level_views"] = {
            str(level): level_views[level]
            for level in sorted(level_views)
        }
    return output
