from __future__ import annotations

from collections import defaultdict
from typing import Any

from .common import canonical_core, stable_json


DECISION_COMPARE_FIELDS = (
    "verdict",
    "trigger",
    "pause_evidence",
    "resume_evidence",
    "rollback_target",
    "metric_basis",
    "reason",
)


def build_difference_report(
    normalized_by_schema: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    grouped: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for schema, project in normalized_by_schema.items():
        for canonical_field, entry in project["provenance"].items():
            grouped[canonical_field][schema] = entry

    rows = []
    kind_counts: dict[str, int] = defaultdict(int)
    all_normalized_equal = True
    for canonical_field in sorted(grouped):
        by_schema = grouped[canonical_field]
        normalized_values = {
            schema: entry["normalized_value"]
            for schema, entry in by_schema.items()
        }
        if len({stable_json(value) for value in normalized_values.values()}) > 1:
            all_normalized_equal = False
        kinds = sorted({entry["difference_kind"] for entry in by_schema.values()})
        for kind in kinds:
            kind_counts[kind] += 1
        rows.append(
            {
                "canonical_field": canonical_field,
                "difference_kinds": kinds,
                "normalized_values_by_schema": normalized_values,
                "sources": {
                    schema: {
                        "path": entry["source_path"],
                        "field": entry["source_field"],
                        "raw_value": entry["raw_value"],
                        "raw_unit": entry["raw_unit"],
                        "normalized_unit": entry["normalized_unit"],
                    }
                    for schema, entry in sorted(by_schema.items())
                },
            }
        )
    return {
        "field_count": len(rows),
        "kind_counts": dict(sorted(kind_counts.items())),
        "all_normalized_values_equal": all_normalized_equal,
        "fields": rows,
    }


def _decision_projection(decision: dict[str, Any]) -> dict[str, Any]:
    return {
        "step_number": decision["step_number"],
        "step_id": decision["step_id"],
        "declared_traffic_percent": decision["declared_traffic_percent"],
        "declared_traffic_ppm": decision["declared_traffic_ppm"],
        **{field: decision.get(field) for field in DECISION_COMPARE_FIELDS},
    }


def _ledger_projection(ledger: dict[str, Any]) -> dict[str, Any]:
    return {
        "elapsed_window_seconds": ledger["elapsed_window_seconds"],
        "paused_seconds": ledger["paused_seconds"],
        "pause_count": ledger["pause_count"],
        "rollback_count": ledger["rollback_count"],
        "metrics": {
            metric: {
                "breach_count": counts["breach_count"],
                "spike_count": counts["spike_count"],
                "sustained_breach_count": counts["sustained_breach_count"],
            }
            for metric, counts in ledger["metrics"].items()
        },
    }


def _reference_ledger_projection(ledger: dict[str, Any]) -> dict[str, Any]:
    return {
        "elapsed_window_seconds": ledger["elapsed_window_seconds"],
        "paused_seconds": ledger["paused_seconds"],
        "pause_count": ledger["pause_count"],
        "rollback_count": ledger["rollback_count"],
        "metrics": {
            metric: {
                "breach_count": counts["breach_count"],
                "spike_count": counts["spike_count"],
                "sustained_breach_count": counts["sustained_breach_count"],
            }
            for metric, counts in ledger["metrics"].items()
        },
    }


def first_reference_mismatch(
    main_run: dict[str, Any], reference_run: dict[str, Any]
) -> dict[str, Any] | None:
    main_by_step = {
        item["step_number"]: item for item in main_run["step_decisions"]
    }
    reference_by_step = {
        item["step_number"]: item for item in reference_run["step_decisions"]
    }
    all_steps = sorted(set(main_by_step) | set(reference_by_step))
    for step_number in all_steps:
        main_decision = main_by_step.get(step_number)
        reference_decision = reference_by_step.get(step_number)
        if main_decision is None or reference_decision is None:
            return {
                "step_number": step_number,
                "step_id": (
                    (main_decision or reference_decision or {}).get("step_id")
                ),
                "field": "step_presence",
                "main_value": main_decision is not None,
                "reference_value": reference_decision is not None,
                "metric_basis": {
                    "main": (
                        main_decision.get("metric_basis")
                        if main_decision
                        else None
                    ),
                    "reference": (
                        reference_decision.get("metric_basis")
                        if reference_decision
                        else None
                    ),
                },
            }
        for field in DECISION_COMPARE_FIELDS:
            main_value = main_decision.get(field)
            reference_value = reference_decision.get(field)
            if stable_json(main_value) != stable_json(reference_value):
                return {
                    "step_number": step_number,
                    "step_id": main_decision["step_id"],
                    "field": field,
                    "main_value": main_value,
                    "reference_value": reference_value,
                    "metric_basis": {
                        "main": main_decision.get("metric_basis"),
                        "reference": reference_decision.get("metric_basis"),
                    },
                }

        main_ledger = next(
            (
                item
                for item in main_run["ledger"]
                if item["step_number"] == step_number
            ),
            None,
        )
        reference_ledger = reference_decision.get("reference_ledger")
        if main_ledger is None or reference_ledger is None:
            return {
                "step_number": step_number,
                "step_id": main_decision["step_id"],
                "field": "ledger_presence",
                "main_value": main_ledger is not None,
                "reference_value": reference_ledger is not None,
                "metric_basis": {
                    "main": main_decision.get("metric_basis"),
                    "reference": reference_decision.get("metric_basis"),
                },
            }
        main_ledger_value = _ledger_projection(main_ledger)
        reference_ledger_value = _reference_ledger_projection(reference_ledger)
        if stable_json(main_ledger_value) != stable_json(reference_ledger_value):
            return {
                "step_number": step_number,
                "step_id": main_decision["step_id"],
                "field": "ledger",
                "main_value": main_ledger_value,
                "reference_value": reference_ledger_value,
                "metric_basis": {
                    "main": main_decision.get("metric_basis"),
                    "reference": reference_decision.get("metric_basis"),
                },
            }

    if stable_json(main_run.get("rollback_target")) != stable_json(
        reference_run.get("rollback_target")
    ):
        return {
            "step_number": (
                main_run["rollback"]["failed_step"]["step_number"]
                if main_run.get("rollback")
                else None
            ),
            "step_id": (
                main_run["rollback"]["failed_step"]["step_id"]
                if main_run.get("rollback")
                else None
            ),
            "field": "rollback_target",
            "main_value": main_run.get("rollback_target"),
            "reference_value": reference_run.get("rollback_target"),
            "metric_basis": None,
        }
    return None


def run_signature(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "step_decisions": [
            _decision_projection(item) for item in run["step_decisions"]
        ],
        "ledger": run["ledger"],
        "rollback": run["rollback"],
        "rollback_target": run["rollback_target"],
        "action_events": run["action_events"],
        "summary": run["summary"],
    }


def cross_version_report(
    normalized_by_schema: dict[str, dict[str, Any]],
    runs_by_schema: dict[str, dict[str, Any]],
    reference_by_schema: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    schemas = sorted(normalized_by_schema)
    baseline = schemas[0]
    core_hashes = {
        schema: stable_json(canonical_core(normalized_by_schema[schema]))
        for schema in schemas
    }
    run_hashes = {
        schema: stable_json(run_signature(runs_by_schema[schema]))
        for schema in schemas
    }
    reference_hashes = {
        schema: stable_json(reference_by_schema[schema]["step_decisions"])
        for schema in schemas
    }
    normalized_equal = len(set(core_hashes.values())) == 1
    decisions_equal = len(
        {
            stable_json(
                [
                    _decision_projection(item)
                    for item in runs_by_schema[schema]["step_decisions"]
                ]
            )
            for schema in schemas
        }
    ) == 1
    rollback_targets_equal = len(
        {
            stable_json(runs_by_schema[schema]["rollback_target"])
            for schema in schemas
        }
    ) == 1
    ledgers_equal = len(
        {
            stable_json(runs_by_schema[schema]["ledger"])
            for schema in schemas
        }
    ) == 1
    runs_equal = len(set(run_hashes.values())) == 1
    references_equal = len(set(reference_hashes.values())) == 1
    first_mismatch = None
    if not runs_equal:
        for schema in schemas[1:]:
            if run_hashes[schema] != run_hashes[baseline]:
                first_mismatch = {
                    "baseline_schema": baseline,
                    "other_schema": schema,
                    "step_number": None,
                    "field": "run_signature",
                }
                for left, right in zip(
                    runs_by_schema[baseline]["step_decisions"],
                    runs_by_schema[schema]["step_decisions"],
                ):
                    for field in DECISION_COMPARE_FIELDS:
                        if stable_json(left.get(field)) != stable_json(
                            right.get(field)
                        ):
                            first_mismatch.update(
                                {
                                    "step_number": left["step_number"],
                                    "step_id": left["step_id"],
                                    "field": field,
                                    "baseline_value": left.get(field),
                                    "other_value": right.get(field),
                                }
                            )
                            break
                    if first_mismatch["step_number"] is not None:
                        break
                break
    return {
        "schemas": schemas,
        "normalized_core_equal": normalized_equal,
        "decisions_equal": decisions_equal,
        "rollback_targets_equal": rollback_targets_equal,
        "ledgers_equal": ledgers_equal,
        "runs_equal": runs_equal,
        "reference_runs_equal": references_equal,
        "first_main_mismatch": first_mismatch,
        "hashes": {
            "normalized_core": core_hashes,
            "run_signature": run_hashes,
            "reference_decisions": reference_hashes,
        },
    }

