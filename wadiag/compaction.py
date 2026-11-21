from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable

from .estimation import segment_merge_gain
from .models import DiagnosticConfig, StorageConfig
from .segments import Segment, build_merged_segments


def _level_bytes(segments: Iterable[Segment]) -> int:
    return sum(segment.meta.size_bytes for segment in segments)


def _selection_for_level(
    level: int,
    level_segments: dict[int, tuple[Segment, ...]],
    storage: StorageConfig,
    diagnostic: DiagnosticConfig,
) -> dict[str, Any]:
    sources = list(level_segments.get(level, ()))
    threshold = storage.level_trigger_thresholds[level]
    if level + 1 >= storage.level_count:
        return {
            "level": level,
            "eligible": False,
            "reason": "top_level_has_no_target",
            "candidates": [],
            "selected": [],
            "budget_used": 0,
            "estimated_gain_fixed": Decimal(0),
            "estimated_gain_truth": Decimal(0),
        }
    if len(sources) < threshold:
        return {
            "level": level,
            "eligible": False,
            "reason": "level_not_triggered",
            "candidates": [],
            "selected": [],
            "budget_used": 0,
            "estimated_gain_fixed": Decimal(0),
            "estimated_gain_truth": Decimal(0),
        }
    scored: list[dict[str, Any]] = []
    compression = storage.compression_decimal(level)
    for segment in sources:
        fixed_gain, truth_gain = segment_merge_gain(segment, compression, diagnostic)
        scored.append(
            {
                "segment": segment,
                "fixed_gain": fixed_gain,
                "truth_gain": truth_gain,
            }
        )
    scored.sort(
        key=lambda item: (
            -item["fixed_gain"],
            -item["truth_gain"],
            item["segment"].meta.segment_no,
        )
    )
    desired = min(len(scored), max(1, len(sources) - threshold + 1))
    target_level = level + 1
    target_existing_bytes = _level_bytes(level_segments.get(target_level, ()))
    target_capacity = (
        storage.level_trigger_thresholds[target_level]
        * storage.segment_size_limit_bytes
    )
    remaining_budget = storage.merge_budget_bytes
    projected_target = target_existing_bytes
    selected: list[Segment] = []
    outcomes: list[dict[str, Any]] = []
    active_abandoned = False
    selected_uids = set()
    for item in scored:
        segment = item["segment"]
        if len(selected) >= desired:
            outcomes.append(
                {
                    "segment_uid": segment.meta.uid,
                    "segment_no": segment.meta.segment_no,
                    "outcome": "NOT_SELECTED_WITHIN_BUDGET",
                    "reason": "selection_target_met",
                    "first_trigger_constraint": "selection_target_met",
                    "size_bytes": segment.meta.size_bytes,
                    "gain_fixed": str(item["fixed_gain"]),
                    "gain_truth": str(item["truth_gain"]),
                }
            )
            continue
        if active_abandoned or segment.meta.size_bytes > remaining_budget:
            active_abandoned = True
            outcomes.append(
                {
                    "segment_uid": segment.meta.uid,
                    "segment_no": segment.meta.segment_no,
                    "outcome": "ACTIVE_ABANDONED",
                    "reason": "merge_budget_exhausted",
                    "first_trigger_constraint": "merge_budget_bytes",
                    "size_bytes": segment.meta.size_bytes,
                    "gain_fixed": str(item["fixed_gain"]),
                    "gain_truth": str(item["truth_gain"]),
                }
            )
            continue
        if projected_target + segment.meta.size_bytes > target_capacity:
            outcomes.append(
                {
                    "segment_uid": segment.meta.uid,
                    "segment_no": segment.meta.segment_no,
                    "outcome": "SELECTED_THEN_CANCELLED",
                    "reason": "target_capacity_exceeded",
                    "first_trigger_constraint": "target_level_capacity_bytes",
                    "size_bytes": segment.meta.size_bytes,
                    "gain_fixed": str(item["fixed_gain"]),
                    "gain_truth": str(item["truth_gain"]),
                }
            )
            continue
        selected.append(segment)
        selected_uids.add(segment.meta.uid)
        remaining_budget -= segment.meta.size_bytes
        projected_target += segment.meta.size_bytes
        outcomes.append(
            {
                "segment_uid": segment.meta.uid,
                "segment_no": segment.meta.segment_no,
                "outcome": "SELECTED",
                "reason": "highest_gain_fitting_budget_and_capacity",
                "first_trigger_constraint": "level_trigger_threshold",
                "size_bytes": segment.meta.size_bytes,
                "gain_fixed": str(item["fixed_gain"]),
                "gain_truth": str(item["truth_gain"]),
            }
        )
    fixed_gain = sum(
        (
            item["fixed_gain"]
            for item in scored
            if item["segment"].meta.uid in selected_uids
        ),
        Decimal(0),
    )
    truth_gain = sum(
        (
            item["truth_gain"]
            for item in scored
            if item["segment"].meta.uid in selected_uids
        ),
        Decimal(0),
    )
    return {
        "level": level,
        "target_level": target_level,
        "eligible": True,
        "reason": "triggered",
        "trigger_threshold": threshold,
        "target_capacity_bytes": target_capacity,
        "target_existing_bytes": target_existing_bytes,
        "budget_bytes": storage.merge_budget_bytes,
        "budget_used": storage.merge_budget_bytes - remaining_budget,
        "candidates": [
            {
                "segment_uid": item["segment"].meta.uid,
                "fixed_gain": str(item["fixed_gain"]),
                "truth_gain": str(item["truth_gain"]),
            }
            for item in scored
        ],
        "selected": [segment.meta.uid for segment in selected],
        "selected_segments": selected,
        "outcomes": outcomes,
        "abandoned": active_abandoned,
        "estimated_gain_fixed": fixed_gain,
        "estimated_gain_truth": truth_gain,
    }


def merge_selected_records(
    selected: Iterable[Segment],
    all_visible: Iterable[Segment],
    storage: StorageConfig,
) -> tuple[list[Any], list[str], int]:
    selected_tuple = tuple(selected)
    selected_uids = {segment.meta.uid for segment in selected_tuple}
    outside = [segment for segment in all_visible if segment.meta.uid not in selected_uids]
    outside_keys = {record.key for segment in outside for record in segment.records}
    output_generation = max(segment.meta.generation for segment in selected_tuple) + 1
    latest: dict[str, Any] = {}
    for segment in selected_tuple:
        for record in segment.records:
            previous = latest.get(record.key)
            if previous is None or record.seq > previous.seq:
                latest[record.key] = record
    kept = []
    cleared: list[str] = []
    for key in sorted(latest):
        record = latest[key]
        if record.is_tombstone:
            age = output_generation - record.origin_generation
            if age >= storage.tombstone_retention_generations and key not in outside_keys:
                cleared.append(key)
                continue
        kept.append(record)
    return kept, cleared, output_generation


def plan_compaction(
    level_segments: dict[int, tuple[Segment, ...]],
    storage: StorageConfig,
    diagnostic: DiagnosticConfig,
) -> dict[str, Any]:
    candidates = [
        _selection_for_level(level, level_segments, storage, diagnostic)
        for level in range(storage.level_count)
    ]
    eligible = [item for item in candidates if item["eligible"]]
    if not eligible:
        return {
            "schema": "wadiag.compaction_plan.v1",
            "started": False,
            "reason": "no_level_triggered",
            "level_candidates": candidates,
            "precision_sensitive": False,
        }
    ranked = sorted(
        eligible,
        key=lambda item: (
            -item["estimated_gain_fixed"],
            -item["estimated_gain_truth"],
            item["level"],
        ),
    )
    chosen = ranked[0]
    if not chosen["selected_segments"]:
        return {
            "schema": "wadiag.compaction_plan.v1",
            "started": False,
            "reason": "merge_budget_or_capacity_prevented_all_segments",
            "level_candidates": candidates,
            "precision_sensitive": False,
        }
    tolerance = diagnostic.tolerance_decimal
    precision_sensitive = False
    comparisons: list[dict[str, Any]] = []
    if len(ranked) > 1:
        fixed_delta = abs(
            ranked[0]["estimated_gain_fixed"] - ranked[1]["estimated_gain_fixed"]
        )
        truth_delta = abs(
            ranked[0]["estimated_gain_truth"] - ranked[1]["estimated_gain_truth"]
        )
        fixed_order = (
            0
            if ranked[0]["estimated_gain_fixed"] == ranked[1]["estimated_gain_fixed"]
            else (
                -1
                if ranked[0]["estimated_gain_fixed"]
                < ranked[1]["estimated_gain_fixed"]
                else 1
            )
        )
        truth_order = (
            0
            if ranked[0]["estimated_gain_truth"] == ranked[1]["estimated_gain_truth"]
            else (
                -1
                if ranked[0]["estimated_gain_truth"]
                < ranked[1]["estimated_gain_truth"]
                else 1
            )
        )
        precision_sensitive = fixed_delta < tolerance or truth_delta < tolerance
        comparisons.append(
            {
                "kind": "triggered_level_pair",
                "levels": [ranked[0]["level"], ranked[1]["level"]],
                "fixed_delta": str(fixed_delta),
                "truth_delta": str(truth_delta),
                "tolerance": diagnostic.float_tolerance,
                "within_tolerance": precision_sensitive,
                "order_changed": fixed_order != truth_order,
                "decision_dependency": True,
            }
        )
    plan = {
        "schema": "wadiag.compaction_plan.v1",
        "started": True,
        "reason": "level_triggered",
        "chosen_level": chosen["level"],
        "target_level": chosen["target_level"],
        "selected_segment_uids": chosen["selected"],
        "selected_segments": chosen["selected_segments"],
        "budget_used": chosen["budget_used"],
        "target_capacity_bytes": chosen["target_capacity_bytes"],
        "outcomes": chosen["outcomes"],
        "level_candidates": candidates,
        "precision_sensitive": precision_sensitive,
        "precision_comparisons": comparisons,
        "decision_basis": (
            "truth_tiebreak_after_sensitive_flag"
            if precision_sensitive
            else "fixed_path"
        ),
    }
    if precision_sensitive and len(ranked) > 1:
        truth_ranked = sorted(
            ranked,
            key=lambda item: (
                -item["estimated_gain_truth"],
                -item["estimated_gain_fixed"],
                item["level"],
            ),
        )
        truth_chosen = truth_ranked[0]
        if truth_chosen["selected_segments"]:
            plan["chosen_level"] = truth_chosen["level"]
            plan["target_level"] = truth_chosen["target_level"]
            plan["selected_segment_uids"] = truth_chosen["selected"]
            plan["selected_segments"] = truth_chosen["selected_segments"]
            plan["budget_used"] = truth_chosen["budget_used"]
            plan["target_capacity_bytes"] = truth_chosen["target_capacity_bytes"]
            plan["outcomes"] = truth_chosen["outcomes"]
    return plan


def build_compaction_outputs(
    plan: dict[str, Any],
    all_visible: Iterable[Segment],
    storage: StorageConfig,
    run_id: str,
    first_segment_no: int,
) -> tuple[tuple[Segment, ...], list[str], int, list[Any]]:
    selected = tuple(plan["selected_segments"])
    kept, cleared, generation = merge_selected_records(
        selected,
        all_visible,
        storage,
    )
    outputs = build_merged_segments(
        kept,
        storage,
        run_id,
        first_segment_no=first_segment_no,
        level=plan["target_level"],
        generation=generation,
    )
    return outputs, cleared, generation, kept
