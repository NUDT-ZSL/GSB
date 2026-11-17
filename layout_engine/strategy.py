from __future__ import annotations

from typing import Any

from .config import LayoutConfig
from .util import ceil_div, sha256_json


def _resolved_rule(value: str, requested: str) -> str:
    return requested if value == "requested" else value


def select_strategy(
    *,
    page_count: int,
    expected_entries: int,
    metadata: dict[str, Any],
    config: LayoutConfig,
) -> dict[str, Any]:
    profile = metadata.get("layout_profile", {})
    if not isinstance(profile, dict):
        profile = {}
    profile_average_blocks = float(profile.get("average_blocks_per_page", 0.0) or 0.0)
    average_blocks = profile_average_blocks
    average_blocks_source = "metadata.layout_profile.average_blocks_per_page"
    if average_blocks <= 0 and page_count > 0:
        average_blocks = expected_entries / page_count
        average_blocks_source = "expected_reading_order_entries / page_count"
    profile_average_groups = float(profile.get("average_column_groups", 0.0) or 0.0)
    average_groups = profile_average_groups if profile_average_groups > 0 else 1.0
    average_groups_source = (
        "metadata.layout_profile.average_column_groups"
        if profile_average_groups > 0
        else "single-column fallback"
    )
    page_budget = config.runtime.batch_page_budget
    pressure = page_count / max(1, page_budget)
    requested_rule = config.reading.rule

    strategies = [
        {
            "name": "full_resolution",
            "detection_resolution": "full",
            "detection_step": 1,
            "column_inference_candidate": True,
            "reading_order_rule_candidate": "requested",
            "accuracy_prior": 1.00,
            "base_work": 9.0,
            "block_work": 0.80,
            "group_work": 0.55,
            "trigger_conditions": [
                f"page_pressure <= {config.adaptive.balanced_pressure:g}",
                "average_blocks_per_page <= 20",
            ],
        },
        {
            "name": "balanced_resolution",
            "detection_resolution": "half",
            "detection_step": 2,
            "column_inference_candidate": True,
            "reading_order_rule_candidate": "requested",
            "accuracy_prior": 0.82,
            "base_work": 5.5,
            "block_work": 0.55,
            "group_work": 0.40,
            "trigger_conditions": [
                f"page_pressure <= {config.adaptive.fast_pressure:g}",
                "average_blocks_per_page <= 35",
            ],
        },
        {
            "name": "fast_resolution",
            "detection_resolution": "quarter",
            "detection_step": 4,
            "column_inference_candidate": False,
            "reading_order_rule_candidate": "region_first",
            "accuracy_prior": 0.55,
            "base_work": 3.0,
            "block_work": 0.35,
            "group_work": 0.15,
            "trigger_conditions": [
                f"page_pressure > {config.adaptive.fast_pressure:g}",
                "or average_blocks_per_page > 35",
            ],
        },
    ]

    for candidate in strategies:
        work = (
            candidate["base_work"]
            + average_blocks * candidate["block_work"]
            + average_groups * candidate["group_work"]
        )
        candidate["estimated_work_units_per_page"] = round(work, 6)
    max_work = max(candidate["estimated_work_units_per_page"] for candidate in strategies)

    scored: list[dict[str, Any]] = []
    for candidate in strategies:
        if candidate["name"] == "full_resolution":
            pressure_penalty = 0.10 * max(0.0, pressure - 1.0)
        elif candidate["name"] == "balanced_resolution":
            pressure_penalty = 0.10 * max(0.0, pressure - 1.0)
            pressure_penalty += 0.06 * max(0.0, pressure - config.adaptive.fast_pressure)
        else:
            pressure_penalty = 0.0
        structure_penalty = 0.0
        if candidate["name"] == "full_resolution":
            structure_penalty = 0.004 * max(0.0, average_blocks - 20.0) * min(10.0, max(1.0, pressure))
        elif candidate["name"] == "balanced_resolution":
            structure_penalty = 0.002 * max(0.0, average_blocks - 35.0) * min(10.0, max(1.0, pressure))
        cost_penalty = 0.02 * (
            candidate["estimated_work_units_per_page"] / max(1e-9, max_work)
        )
        score = candidate["accuracy_prior"] - pressure_penalty - structure_penalty - cost_penalty
        resolved = dict(candidate)
        resolved["reading_order_rule_candidate"] = _resolved_rule(
            candidate["reading_order_rule_candidate"], requested_rule
        )
        resolved["score"] = round(score, 6)
        resolved["score_parts"] = {
            "accuracy_prior": candidate["accuracy_prior"],
            "pressure_penalty": round(pressure_penalty, 6),
            "structure_penalty": round(structure_penalty, 6),
            "relative_cost_penalty": round(cost_penalty, 6),
        }
        scored.append(resolved)

    if config.adaptive.enabled:
        selected = max(scored, key=lambda candidate: (candidate["score"], candidate["accuracy_prior"]))
        selection_mode = "adaptive_table_score"
        reason = (
            f"selected {selected['name']} with highest score {selected['score']:.6f}; "
            f"page_pressure={pressure:.6f}, average_blocks={average_blocks:.6f}, "
            f"average_column_groups={average_groups:.6f}"
        )
    else:
        selected = {
            "name": "fixed_config",
            "detection_resolution": "full",
            "detection_step": 1,
            "column_inference_candidate": config.columns.enabled,
            "reading_order_rule_candidate": requested_rule,
            "accuracy_prior": 1.0,
            "estimated_work_units_per_page": scored[0]["estimated_work_units_per_page"],
            "score": scored[0]["score"],
            "score_parts": scored[0]["score_parts"],
            "trigger_conditions": ["adaptive.enabled == false"],
        }
        selection_mode = "adaptive_disabled_fixed_config"
        reason = "adaptive selection disabled; use the requested configuration without a scale tradeoff"

    selected_columns_enabled = bool(
        config.columns.enabled and selected["column_inference_candidate"]
    )
    resolved_rule = selected["reading_order_rule_candidate"]
    selected_step = int(selected["detection_step"])
    full_work = float(scored[0]["estimated_work_units_per_page"])
    selected_work = float(selected["estimated_work_units_per_page"])
    triggered_by = []
    if selected["name"] == "full_resolution":
        if pressure <= config.adaptive.balanced_pressure:
            triggered_by.append(selected["trigger_conditions"][0])
        if average_blocks <= 20:
            triggered_by.append(selected["trigger_conditions"][1])
    elif selected["name"] == "balanced_resolution":
        if pressure <= config.adaptive.fast_pressure:
            triggered_by.append(selected["trigger_conditions"][0])
        if average_blocks <= 35:
            triggered_by.append(selected["trigger_conditions"][1])
    elif selected["name"] == "fast_resolution":
        if pressure > config.adaptive.fast_pressure:
            triggered_by.append(selected["trigger_conditions"][0])
        if average_blocks > 35:
            triggered_by.append(selected["trigger_conditions"][1])
    elif selected["name"] == "fixed_config":
        triggered_by.append(selected["trigger_conditions"][0])

    decision = {
        "decision_id": sha256_json(
            {
                "page_count": page_count,
                "expected_entries": expected_entries,
                "page_budget": page_budget,
                "selected": selected["name"],
                "step": selected_step,
                "rule": resolved_rule,
            }
        )[:16],
        "adaptive_enabled": config.adaptive.enabled,
        "selection_mode": selection_mode,
        "metrics": {
            "page_count": page_count,
            "expected_reading_order_entries": expected_entries,
            "average_blocks_per_page": round(average_blocks, 6),
            "average_column_groups": round(average_groups, 6),
            "batch_page_budget": page_budget,
            "page_pressure": round(pressure, 6),
            "thread_pool_workers": config.runtime.thread_pool_workers,
        },
        "metric_sources": {
            "page_count": "PNG files scanned in input directory",
            "average_blocks_per_page": average_blocks_source,
            "average_column_groups": average_groups_source,
            "batch_page_budget": "runtime.batch_page_budget",
        },
        "candidates": scored,
        "selected": {
            "name": selected["name"],
            "detection_resolution": selected["detection_resolution"],
            "detection_step": selected_step,
            "column_inference_enabled": selected_columns_enabled,
            "reading_order_rule": resolved_rule,
            "score": selected["score"],
            "score_parts": selected["score_parts"],
            "trigger_conditions": selected["trigger_conditions"],
            "triggered_by": triggered_by,
        },
        "reason": reason,
        "budget_impact": {
            "page_budget": page_budget,
            "estimated_batches": ceil_div(page_count, page_budget),
            "estimated_pages_per_batch": min(page_count, page_budget),
            "estimated_work_units_per_page": round(selected_work, 6),
            "estimated_total_work_units": round(selected_work * page_count, 6),
            "relative_work_vs_full_resolution": round(
                selected_work / max(1e-9, full_work), 6
            ),
            "column_inference_disabled_for_scale": bool(
                config.columns.enabled and not selected_columns_enabled
            ),
        },
    }
    effective = {
        "detection_step": selected_step,
        "columns_enabled": selected_columns_enabled,
        "reading_rule": resolved_rule,
        "strategy_name": selected["name"],
    }
    return {"decision": decision, "effective": effective}
