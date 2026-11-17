from __future__ import annotations

import copy
import tempfile
from typing import Any

from .engine import build_full_result, compare_results, run_incremental
from .samples import load_records


def _weight_only_plan(base_config: dict[str, Any]) -> dict[str, Any]:
    weights = list(base_config["weights"])
    updates: dict[str, float] = {}
    if weights:
        weights[0] = round(weights[0] + 0.07, 6)
        updates["0"] = weights[0]
    if len(weights) > 3:
        weights[3] = round(weights[3] - 0.04, 6)
        updates["3"] = weights[3]
    return {
        "format_version": 1,
        "batches": [
            {
                "batch_id": "CHECK-WEIGHT-ONLY",
                "weight_updates": updates,
            }
        ],
    }


def _data_only_plan(base_entries: list[dict[str, Any]]) -> tuple[dict[str, Any], bool]:
    loaded = load_records(base_entries)
    pair_ids = sorted(loaded["valid"])
    if not pair_ids:
        return {"format_version": 1, "batches": []}, False
    replace_id = pair_ids[0]
    replacement = copy.deepcopy(loaded["valid"][replace_id])
    replacement["chosen"] = replacement["chosen"] + " deterministic replacement token"
    add_record = copy.deepcopy(loaded["valid"][pair_ids[-1]])
    add_record["pair_id"] = "check-data-added-001"
    add_record["chosen"] = add_record["chosen"] + " independently added sample"
    deletes = [pair_ids[1]] if len(pair_ids) > 1 else []
    return (
        {
            "format_version": 1,
            "batches": [
                {
                    "batch_id": "CHECK-DATA-ONLY",
                    "replace": [replacement],
                    "delete": deletes,
                    "add": [add_record],
                }
            ],
        },
        True,
    )


def _event_summary(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for event in events:
        if event.get("event") != "batch":
            continue
        result.append(
            {
                "batch_id": event["batch_id"],
                "invalidated_stages": event["invalidated_stages"],
                "reused_stages": event["reused_stages"],
                "recomputed_pair_counts": event["recomputed_pair_counts"],
                "reused_pair_counts": event["reused_pair_counts"],
                "invariant_checks": event["invariant_checks"],
            }
        )
    return result


def _run_scenario(
    *,
    category: str,
    base_entries: list[dict[str, Any]],
    base_config: dict[str, Any],
    plan: dict[str, Any],
    dependencies: dict[str, Any],
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix=f"reward-diag-{category}-") as state_dir:
        incremental = run_incremental(
            base_entries=base_entries,
            base_config=base_config,
            plan=plan,
            dependencies=dependencies,
            state_dir=state_dir,
        )
        full = build_full_result(
            base_entries=base_entries,
            base_config=base_config,
            plan=plan,
            dependencies=dependencies,
        )
        comparison = compare_results(incremental["canonical_result"], full)
    return {
        "category": category,
        "identical": comparison["identical"],
        "comparison": comparison,
        "batch_events": _event_summary(incremental["events"]),
        "snapshot_version": incremental["manifest"]["global_version"],
    }


def run_consistency_suite(
    *,
    base_entries: list[dict[str, Any]],
    base_config: dict[str, Any],
    main_plan: dict[str, Any],
    dependencies: dict[str, Any],
) -> dict[str, Any]:
    scenarios: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    definitions: list[tuple[str, dict[str, Any]]] = [
        ("weight_only", _weight_only_plan(base_config)),
    ]
    data_plan, has_data = _data_only_plan(base_entries)
    if has_data:
        definitions.append(("data_only", data_plan))
    definitions.append(("mixed", main_plan))

    for category, plan in definitions:
        try:
            scenarios.append(
                _run_scenario(
                    category=category,
                    base_entries=base_entries,
                    base_config=base_config,
                    plan=plan,
                    dependencies=dependencies,
                )
            )
        except BaseException as exc:
            errors.append(
                {
                    "category": category,
                    "exception_type": type(exc).__name__,
                    "message": str(exc),
                }
            )

    required = {"weight_only", "data_only", "mixed"}
    covered = {item["category"] for item in scenarios}
    return {
        "all_identical": bool(scenarios)
        and all(item["identical"] for item in scenarios)
        and required.issubset(covered)
        and not errors,
        "required_categories": sorted(required),
        "covered_categories": sorted(covered),
        "scenarios": scenarios,
        "errors": errors,
    }
