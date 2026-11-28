from __future__ import annotations

import copy
from typing import Any

from .evaluator import evaluate_batch, evaluate_model
from .generator import GeneratedSample, generate_sample
from .model import (
    BatchBudget,
    BudgetRejected,
    ColumnVector,
    EvalBatch,
)
from .normalize import (
    build_version_difference_manifest,
    normalize_all,
)
from .reference import reference_evaluate_model
from .verify import compare_version_consistency, verify_against_reference


def _truth_and(left: bool | None, right: bool | None) -> bool | None:
    if left is False or right is False:
        return False
    if left is None or right is None:
        return None
    return True


def _truth_or(left: bool | None, right: bool | None) -> bool | None:
    if left is True or right is True:
        return True
    if left is None or right is None:
        return None
    return False


THREE_VALUED_TRUTH_TABLE = [
    *[
        {
            "op": "AND",
            "left": left,
            "right": right,
            "result": _truth_and(left, right),
        }
        for left in (False, True, None)
        for right in (False, True, None)
    ],
    *[
        {
            "op": "OR",
            "left": left,
            "right": right,
            "result": _truth_or(left, right),
        }
        for left in (False, True, None)
        for right in (False, True, None)
    ],
    *[
        {
            "op": "NOT",
            "left": left,
            "result": None if left is None else not left,
        }
        for left in (False, True, None)
    ],
]


def _conditional_isolation_audit(
    main_runs: dict[str, Any],
) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    all_ok = True
    for version, run in main_runs.items():
        for batch in run.batches:
            for ledger_entry in batch.ledger:
                if ledger_entry["op"] != "conditional":
                    continue
                conditional = ledger_entry["conditional"]
                expected_nulls = (
                    conditional["condition_null_rows"]
                    + conditional["true_branch_result_nulls"]
                    + conditional["false_branch_result_nulls"]
                )
                equation_holds = (
                    expected_nulls == conditional["merged_result_nulls"]
                )
                isolation_flag = conditional["unselected_branch_nulls_isolated"]
                ok = equation_holds and isolation_flag
                all_ok = all_ok and ok
                entries.append(
                    {
                        "version": version,
                        "batch_index": batch.batch_index,
                        "node_id": ledger_entry["node_id"],
                        "condition_true_rows": conditional["condition_true_rows"],
                        "condition_false_rows": conditional["condition_false_rows"],
                        "condition_null_rows": conditional["condition_null_rows"],
                        "true_branch_selected_rows": conditional[
                            "true_branch_selected_rows"
                        ],
                        "false_branch_selected_rows": conditional[
                            "false_branch_selected_rows"
                        ],
                        "true_branch_result_nulls": conditional[
                            "true_branch_result_nulls"
                        ],
                        "false_branch_result_nulls": conditional[
                            "false_branch_result_nulls"
                        ],
                        "merged_result_nulls": conditional["merged_result_nulls"],
                        "expected_merged_nulls": expected_nulls,
                        "unselected_branch_nulls_isolated": isolation_flag,
                        "ok": ok,
                    }
                )
    return {
        "ok": all_ok and bool(entries),
        "rule": (
            "merged nulls = condition-null rows + nulls actually selected from "
            "the true branch + nulls actually selected from the false branch; "
            "branch expressions run only on their selected row indices"
        ),
        "checks": entries,
    }


def _budget_rejection_probe(
    version: str,
    model: Any,
    budget: BatchBudget,
) -> dict[str, Any]:
    probes: list[dict[str, Any]] = []
    row_count = budget.max_rows + 1
    oversize_columns = {
        name: ColumnVector(
            dtype=vector.dtype,
            values=[
                vector.values[idx % model.row_count]
                for idx in range(row_count)
            ],
            null_mask=[
                vector.null_mask[idx % model.row_count]
                for idx in range(row_count)
            ],
            source_field=vector.source_field,
        )
        for name, vector in model.columns.items()
    }
    oversize_rows = EvalBatch(
        batch_index=0,
        row_start=0,
        row_count=row_count,
        columns=oversize_columns,
    )
    try:
        evaluate_batch(model, oversize_rows, budget=budget)
        probes.append(
            {
                "probe": "row_limit",
                "accepted": True,
                "rejected": False,
                "silently_truncated": False,
            }
        )
    except BudgetRejected as error:
        probes.append(
            {
                "probe": "row_limit",
                "accepted": False,
                **error.to_json(),
                "silently_truncated": False,
            }
        )

    narrow_budget = BatchBudget(
        max_rows=budget.max_rows,
        max_input_columns=max(1, model.column_width() - 1),
        max_working_columns=budget.max_working_columns,
        max_cells=budget.max_cells,
        rejection_policy=budget.rejection_policy,
    )
    first_batch = model.batch(batch_index=1, row_start=0, row_count=1)
    try:
        evaluate_batch(model, first_batch, budget=narrow_budget)
        probes.append(
            {
                "probe": "column_width_limit",
                "accepted": True,
                "rejected": False,
                "silently_truncated": False,
            }
        )
    except BudgetRejected as error:
        probes.append(
            {
                "probe": "column_width_limit",
                "accepted": False,
                **error.to_json(),
                "silently_truncated": False,
            }
        )
    return {
        "version": version,
        "declared_policy": budget.rejection_policy,
        "ok": all(probe.get("rejected") for probe in probes),
        "probes": probes,
    }


def _generated_schema_summary(sample: GeneratedSample) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for version, schema in sample.schemas.items():
        summary[version] = {
            "source_system": schema["source_system"],
            "row_count": schema["row_count"],
            "name_rule": schema["name_rule"],
            "physical_decimal_scale": schema["physical_decimal_scale"],
            "physical_scale_encoding": schema["physical_scale_encoding"],
            "field_map": copy.deepcopy(schema["field_map"]),
            "null_codec": {
                "kind": schema["null_codec"]["kind"],
                "storage": schema["null_codec"].get("storage"),
                "bit_order": schema["null_codec"].get("bit_order"),
                "null_bit": schema["null_codec"].get("null_bit"),
            },
        }
    return summary


def run_acceptance(
    *,
    seed: int = 20261128,
    row_count: int = 257,
    batch_rows: int = 32,
    budget: BatchBudget | None = None,
    include_run_details: bool = True,
) -> dict[str, Any]:
    if budget is None:
        budget = BatchBudget(
            max_rows=64,
            max_input_columns=12,
            max_working_columns=64,
            max_cells=4096,
        )
    sample = generate_sample(seed=seed, row_count=row_count)
    models = normalize_all(sample)
    difference_manifest = build_version_difference_manifest(models)
    main_runs = {
        version: evaluate_model(model, batch_rows=batch_rows, budget=budget)
        for version, model in models.items()
    }
    reference_runs = {
        version: reference_evaluate_model(
            model, batch_rows=batch_rows, budget=budget
        )
        for version, model in models.items()
    }
    reference_verification = {
        version: verify_against_reference(
            main_runs[version], reference_runs[version], raise_on_mismatch=False
        )
        for version in models
    }
    version_consistency = compare_version_consistency(
        models, main_runs, reference_runs
    )
    conditional_audit = _conditional_isolation_audit(main_runs)
    budget_probes = {
        version: _budget_rejection_probe(version, model, budget)
        for version, model in models.items()
    }

    all_reference_ok = all(
        verification["ok"] for verification in reference_verification.values()
    )
    all_budget_probes_ok = all(probe["ok"] for probe in budget_probes.values())
    version_diff_expected_only = (
        not difference_manifest["unexpected_changes"]
        and not any(
            entry["distinct"]
            for entry in difference_manifest["differences"]
            if entry["aspect"] == "canonical_column_set"
        )
    )
    status = (
        "PASS"
        if (
            all_reference_ok
            and version_consistency["ok"]
            and version_diff_expected_only
            and conditional_audit["ok"]
            and all_budget_probes_ok
        )
        else "FAIL"
    )

    normalized_summaries = {
        version: {
            "schema_version": model.schema_version,
            "row_count": model.row_count,
            "original_to_canonical": copy.deepcopy(model.original_to_canonical),
            "scale_alignments": copy.deepcopy(model.scale_alignments),
            "normalized_null_codec": copy.deepcopy(model.normalized_null_codec),
            "canonical_columns": {
                name: vector.dtype.label for name, vector in model.columns.items()
            },
            "columns": {
                name: vector.to_json(include_display=False)
                for name, vector in model.columns.items()
            },
            "normalized_expressions": copy.deepcopy(model.expressions),
            "expression_field_rewrites": copy.deepcopy(model.expression_rewrites),
        }
        for version, model in models.items()
    }

    report: dict[str, Any] = {
        "status": status,
        "generator": {
            "seed": seed,
            "row_count": row_count,
            "deterministic_stdlib_only": True,
            "schemas": _generated_schema_summary(sample),
        },
        "batch_budget": budget.to_json(),
        "batch_rows": batch_rows,
        "batch_plan": {
            version: [
                {
                    "batch_index": batch.batch_index,
                    "row_start": batch.row_start,
                    "row_count": batch.row_count,
                }
                for batch in run.batches
            ]
            for version, run in main_runs.items()
        },
        "version_difference_manifest": difference_manifest,
        "normalization": normalized_summaries,
        "reference_verification": reference_verification,
        "version_consistency": version_consistency,
        "three_valued_logic": {
            "truth_table": THREE_VALUED_TRUTH_TABLE,
            "semantics": (
                "arithmetic and comparison nodes mark any null operand as null; "
                "AND treats false as dominant, OR treats true as dominant, "
                "and the remaining null combinations propagate null"
            ),
        },
        "conditional_isolation_audit": conditional_audit,
        "budget_rejection_audit": budget_probes,
        "acceptance_criteria": {
            "independent_reference_matches_every_batch_node": all_reference_ok,
            "three_schema_versions_normalize_and_evaluate_identically": version_consistency[
                "ok"
            ],
            "only_declared_version_differences_remain": version_diff_expected_only,
            "conditional_unselected_nulls_are_isolated": conditional_audit["ok"],
            "over_budget_batches_are_rejected": all_budget_probes_ok,
        },
    }
    if include_run_details:
        report["main_runs"] = {
            version: run.to_json(include_node_results=False)
            for version, run in main_runs.items()
        }
        report["reference_runs"] = {
            version: reference_run.to_json(include_node_results=False)
            for version, reference_run in reference_runs.items()
        }
    else:
        report["main_run_summaries"] = {
            version: {
                "expression_summaries": run.expression_summaries(),
                "expression_results": {
                    expression_id: vector.to_json()
                    for expression_id, vector in run.expression_results.items()
                },
            }
            for version, run in main_runs.items()
        }
    return report
