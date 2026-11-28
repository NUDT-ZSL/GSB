"""Strategy recommendation and cross-version consistency auditing."""

from __future__ import annotations

from typing import Any

from .hashing import stable_hash
from .model import NormalizedMatrix, decimal_text
from .reference import compare_vectors
from .spec import OperationSpec


def matrix_fingerprint(matrix: NormalizedMatrix) -> dict[str, Any]:
    return {
        "rows": matrix.rows,
        "columns": matrix.columns,
        "entries": [
            [entry.row, entry.column, decimal_text(entry.value)]
            for entry in matrix.entries
        ],
    }


def matrix_hash(matrix: NormalizedMatrix) -> str:
    return stable_hash(matrix_fingerprint(matrix))


def vector_hash(matrix: NormalizedMatrix) -> str:
    return stable_hash([float(value) for value in matrix.vector.values])


def result_hash(result_vector: list[float]) -> str:
    return stable_hash([float(value) for value in result_vector])


def ledger_hash(ledger: dict[str, Any]) -> str:
    return stable_hash(ledger)


def _strategy_rank(layout: str, run: dict[str, Any], selected: str) -> tuple[Any, ...]:
    ledger = run["ledger"]
    return (
        ledger["nonzero_scan_total"],
        ledger["hotspot_batch_count"],
        ledger["hotspot_scan_total"],
        ledger["peak_batch_nonzeros"],
        ledger["batch_count"],
        0 if layout == selected else 1,
        -ledger["dense_chunk_hits"],
    )


def recommend_strategy(
    operation: OperationSpec,
    selected_layout: str,
    strategy_runs: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    layouts = list(strategy_runs)
    conflicts: list[dict[str, Any]] = []
    pairwise: dict[str, Any] = {}
    for left_index in range(len(layouts)):
        for right_index in range(left_index + 1, len(layouts)):
            left = layouts[left_index]
            right = layouts[right_index]
            comparison = compare_vectors(
                strategy_runs[left]["result_vector"],
                strategy_runs[right]["result_vector"],
                operation.tolerance,
            )
            pairwise[f"{left}_vs_{right}"] = comparison
            if not comparison["consistent"]:
                conflicts.append(
                    {
                        "kind": "strategy_result_conflict",
                        "layouts": [left, right],
                        "comparison": comparison,
                    }
                )

    for layout, run in strategy_runs.items():
        if not run["comparison"]["within_tolerance"]:
            conflicts.append(
                {
                    "kind": "strategy_reference_conflict",
                    "layout": layout,
                    "comparison": run["comparison"],
                }
            )

    candidates = {
        layout: {
            "layout": layout,
            "nonzero_scan_total": run["ledger"]["nonzero_scan_total"],
            "batch_count": run["ledger"]["batch_count"],
            "hotspot_batch_ids": run["ledger"]["hotspot_batch_ids"],
            "hotspot_batch_count": run["ledger"]["hotspot_batch_count"],
            "hotspot_scan_total": run["ledger"]["hotspot_scan_total"],
            "peak_batch_nonzeros": run["ledger"]["peak_batch_nonzeros"],
            "dense_chunk_hits": run["ledger"]["dense_chunk_hits"],
            "direction_matches_selected_layout": layout == selected_layout,
            "within_tolerance_of_dense_reference": run["comparison"][
                "within_tolerance"
            ],
        }
        for layout, run in strategy_runs.items()
    }
    if conflicts:
        return None, {
            "status": "conflict",
            "message": "Strategy results conflict; recommendation is refused",
            "conflicts": conflicts,
            "pairwise_comparisons": pairwise,
            "candidates": candidates,
        }

    ranked = sorted(
        layouts,
        key=lambda layout: _strategy_rank(layout, strategy_runs[layout], selected_layout),
    )
    recommended = ranked[0]
    winner_ledger = strategy_runs[recommended]["ledger"]
    rationale = [
        (
            f"all {len(layouts)} strategies match the independent dense reference "
            f"within tolerance {operation.tolerance:g}"
        ),
        "nonzero_scan_total is the first recommendation criterion",
        (
            f"{recommended} has scan_total={winner_ledger['nonzero_scan_total']}, "
            f"hotspots={winner_ledger['hotspot_batch_ids']}, "
            f"hotspot_scan_total={winner_ledger['hotspot_scan_total']}, "
            f"peak_batch_nonzeros={winner_ledger['peak_batch_nonzeros']}, "
            f"batch_count={winner_ledger['batch_count']}"
        ),
    ]
    if len(ranked) > 1:
        runner_up = ranked[1]
        runner_ledger = strategy_runs[runner_up]["ledger"]
        rationale.append(
            f"runner-up {runner_up} has scan_total={runner_ledger['nonzero_scan_total']}, "
            f"hotspots={runner_ledger['hotspot_batch_ids']}, "
            f"hotspot_scan_total={runner_ledger['hotspot_scan_total']}, "
            f"peak_batch_nonzeros={runner_ledger['peak_batch_nonzeros']}, "
            f"batch_count={runner_ledger['batch_count']}"
        )
    if recommended == selected_layout:
        rationale.append(
            f"{recommended} also matches the direction-derived layout selection "
            f"for {operation.direction}"
        )
    else:
        rationale.append(
            f"{recommended} overrides the direction-derived default {selected_layout} "
            "because its measured hotspot/workload evidence ranks first"
        )
    if winner_ledger["hotspot_batch_count"] == 0:
        rationale.append("the recommended layout has no batches above the hotspot threshold")
    else:
        rationale.append(
            "hot batches are retained as explicit evidence rather than hidden by "
            "splitting an unsplittable row"
        )

    return {
        "recommended_layout": recommended,
        "selected_layout_input": selected_layout,
        "operation_direction": operation.direction,
        "decision_order": [
            "nonzero_scan_total",
            "hotspot_batch_count",
            "hotspot_scan_total",
            "peak_batch_nonzeros",
            "batch_count",
            "direction_match",
            "dense_chunk_hits_descending",
        ],
        "candidates": candidates,
        "pairwise_comparisons": pairwise,
        "rationale": rationale,
    }, None


def _matrix_difference(
    left: NormalizedMatrix,
    right: NormalizedMatrix,
) -> list[dict[str, Any]]:
    if left.rows != right.rows or left.columns != right.columns:
        return [
            {
                "kind": "shape",
                "versions": [left.version, right.version],
                "left": [left.rows, left.columns],
                "right": [right.rows, right.columns],
            }
        ]
    left_entries = {(entry.row, entry.column): entry for entry in left.entries}
    right_entries = {(entry.row, entry.column): entry for entry in right.entries}
    differences: list[dict[str, Any]] = []
    for key in sorted(set(left_entries) | set(right_entries)):
        left_entry = left_entries.get(key)
        right_entry = right_entries.get(key)
        if left_entry is None or right_entry is None:
            differences.append(
                {
                    "kind": "missing_coordinate",
                    "versions": [left.version, right.version],
                    "row": key[0],
                    "column": key[1],
                    "missing_in": left.version if left_entry is None else right.version,
                    "value_present": decimal_text(
                        (left_entry or right_entry).value  # type: ignore[union-attr]
                    ),
                }
            )
        elif left_entry.value != right_entry.value:
            differences.append(
                {
                    "kind": "weight",
                    "versions": [left.version, right.version],
                    "row": key[0],
                    "column": key[1],
                    "left": decimal_text(left_entry.value),
                    "right": decimal_text(right_entry.value),
                }
            )
        if len(differences) >= 64:
            break
    return differences


def cross_version_audit(
    matrices: list[NormalizedMatrix],
    version_reports: list[dict[str, Any]],
    operation: OperationSpec,
) -> dict[str, Any]:
    if not matrices:
        return {
            "consistent": True,
            "applicable": False,
            "reason": "no_versions",
            "differences": [],
        }
    baseline = matrices[0]
    baseline_report = version_reports[0]
    matrix_hashes = [matrix_hash(matrix) for matrix in matrices]
    vector_hashes = [vector_hash(matrix) for matrix in matrices]
    matrix_differences = []
    for matrix in matrices[1:]:
        matrix_differences.extend(_matrix_difference(baseline, matrix))

    vector_checks = []
    for matrix in matrices[1:]:
        comparison = compare_vectors(
            list(baseline.vector.values),
            list(matrix.vector.values),
            operation.tolerance,
        )
        comparison["versions"] = [baseline.version, matrix.version]
        vector_checks.append(comparison)

    layout_checks: dict[str, Any] = {}
    for layout in operation.strategies:
        result_hashes = []
        ledger_hashes = []
        result_comparisons = []
        baseline_result = baseline_report["strategies"][layout]["kernel"]["result_vector"]
        baseline_ledger_hash = ledger_hash(
            baseline_report["strategies"][layout]["ledger"]
        )
        for report in version_reports:
            run = report["strategies"][layout]
            result_hashes.append(result_hash(run["kernel"]["result_vector"]))
            ledger_hashes.append(ledger_hash(run["ledger"]))
        for report in version_reports[1:]:
            comparison = compare_vectors(
                baseline_result,
                report["strategies"][layout]["kernel"]["result_vector"],
                operation.tolerance,
            )
            comparison["versions"] = [baseline.version, report["version"]]
            result_comparisons.append(comparison)
        layout_checks[layout] = {
            "result_hashes": result_hashes,
            "result_hashes_equal": len(set(result_hashes)) == 1,
            "ledger_hashes": ledger_hashes,
            "ledger_hashes_equal": len(set(ledger_hashes)) == 1,
            "result_elementwise_checks": result_comparisons,
            "result_elementwise_equal": all(
                comparison["consistent"] for comparison in result_comparisons
            ),
            "baseline_ledger_hash": baseline_ledger_hash,
        }

    empty_rows_equal = len(
        {tuple(report["normalization"]["empty_rows"]) for report in version_reports}
    ) == 1
    shapes_equal = len(
        {
            (report["normalization"]["normalized_rows"], report["normalization"]["normalized_columns"])
            for report in version_reports
        }
    ) == 1
    recommendations = [
        report["recommendation"]["recommended_layout"]
        if report.get("recommendation")
        else None
        for report in version_reports
    ]
    recommendations_equal = len(set(recommendations)) == 1 and recommendations[0] is not None
    checks = {
        "same_normalized_shape": shapes_equal,
        "same_matrix": len(set(matrix_hashes)) == 1,
        "same_dense_vector": len(set(vector_hashes)) == 1
        and all(comparison["consistent"] for comparison in vector_checks),
        "same_empty_rows": empty_rows_equal,
        "same_recommendation": recommendations_equal,
        "strategies": layout_checks,
    }
    consistent = (
        shapes_equal
        and checks["same_matrix"]
        and checks["same_dense_vector"]
        and empty_rows_equal
        and all(
            check["result_hashes_equal"]
            and check["ledger_hashes_equal"]
            and check["result_elementwise_equal"]
            for check in layout_checks.values()
        )
        and recommendations_equal
    )
    corrections: list[dict[str, Any]] = []
    for report in version_reports:
        corrections.extend(report["normalization"]["field_corrections"])
    return {
        "applicable": len(matrices) > 1,
        "consistent": consistent,
        "baseline_version": baseline.version,
        "checks": checks,
        "hashes": {
            "matrix": matrix_hashes,
            "dense_vector": vector_hashes,
            "results": {
                layout: layout_checks[layout]["result_hashes"]
                for layout in operation.strategies
            },
            "ledgers": {
                layout: layout_checks[layout]["ledger_hashes"]
                for layout in operation.strategies
            },
        },
        "matrix_element_differences": matrix_differences,
        "dense_vector_elementwise_checks": vector_checks,
        "differences": corrections,
        "correction_count": len(corrections),
    }
