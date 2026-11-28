from __future__ import annotations

import hashlib
import json
from typing import Any

from .evaluator import EvaluationRun, NodeResult
from .model import ColumnVector, format_scaled
from .normalize import NormalizedModel
from .reference import ReferenceNodeResult, ReferenceRun


class ReferenceMismatch(Exception):
    def __init__(self, report: dict[str, Any]) -> None:
        self.report = report
        super().__init__(
            "reference mismatch at "
            f"batch={report.get('batch_index')} "
            f"expression={report.get('expression_id')} "
            f"node={report.get('node_id')} "
            f"position={report.get('position')}"
        )


def _public_value(vector: ColumnVector, idx: int) -> Any:
    if vector.null_mask[idx]:
        return None
    value = vector.values[idx]
    if vector.dtype.kind == "decimal":
        return {
            "raw": value,
            "display": format_scaled(int(value), int(vector.dtype.scale or 0)),
        }
    return value


def _mask_payload(
    main_mask: list[bool], reference_mask: list[bool]
) -> dict[str, Any]:
    length = max(len(main_mask), len(reference_mask))
    positions: list[int] = []
    for idx in range(length):
        main_bit = main_mask[idx] if idx < len(main_mask) else None
        reference_bit = reference_mask[idx] if idx < len(reference_mask) else None
        if main_bit != reference_bit:
            positions.append(idx)
    main_bits = "".join("1" if flag else "0" for flag in main_mask)
    reference_bits = "".join("1" if flag else "0" for flag in reference_mask)
    diff_bits = "".join(
        "1" if idx < len(main_mask) and idx < len(reference_mask) and main_mask[idx] != reference_mask[idx] else "0"
        for idx in range(length)
    )
    return {
        "length": length,
        "difference_positions": positions,
        "difference_position_count": len(positions),
        "main_bitstring": main_bits,
        "reference_bitstring": reference_bits,
        "difference_bitstring": diff_bits,
    }


def _first_vector_difference(
    main: ColumnVector,
    reference: ColumnVector,
) -> dict[str, Any] | None:
    if main.dtype != reference.dtype:
        return {
            "field": "dtype",
            "main_dtype": main.dtype.label,
            "reference_dtype": reference.dtype.label,
            "mask_difference": _mask_payload(main.null_mask, reference.null_mask),
        }
    if len(main) != len(reference):
        return {
            "field": "length",
            "main_length": len(main),
            "reference_length": len(reference),
            "mask_difference": _mask_payload(main.null_mask, reference.null_mask),
        }
    for idx, (main_null, reference_null) in enumerate(
        zip(main.null_mask, reference.null_mask)
    ):
        if main_null != reference_null:
            return {
                "field": "null_mask",
                "position": idx,
                "null_mask_position": idx,
                "column_position": idx,
                "result_column_index": 0,
                "main_value": _public_value(main, idx),
                "reference_value": _public_value(reference, idx),
                "mask_difference": _mask_payload(main.null_mask, reference.null_mask),
            }
    for idx, (main_value, reference_value) in enumerate(
        zip(main.values, reference.values)
    ):
        if main_value != reference_value:
            return {
                "field": "value",
                "position": idx,
                "null_mask_position": idx,
                "column_position": idx,
                "result_column_index": 0,
                "main_value": _public_value(main, idx),
                "reference_value": _public_value(reference, idx),
                "mask_difference": _mask_payload(main.null_mask, reference.null_mask),
            }
    return None


def _compare_node_result(
    *,
    batch_index: int,
    expression_id: str,
    node: NodeResult,
    reference: ReferenceNodeResult,
) -> dict[str, Any] | None:
    difference = _first_vector_difference(node.vector, reference.vector)
    if difference is None and node.row_indices != reference.row_indices:
        difference = {
            "field": "row_indices",
            "mask_difference": _mask_payload(
                node.vector.null_mask, reference.vector.null_mask
            ),
        }
    if difference is None:
        return None
    return {
        "batch_index": batch_index,
        "expression_id": expression_id,
        "node_id": node.node_id,
        "op": node.op,
        "row_scope": node.row_scope,
        "selection_context": node.selection_context,
        **difference,
    }


def verify_against_reference(
    main_run: EvaluationRun,
    reference_run: ReferenceRun,
    *,
    raise_on_mismatch: bool = True,
) -> dict[str, Any]:
    """Compare every batch, node, result value, and null mask in order."""
    first_mismatch: dict[str, Any] | None = None
    nodes_checked = 0
    expressions_checked = 0
    if main_run.batch_rows != reference_run.batch_rows:
        first_mismatch = {
            "batch_index": 0,
            "expression_id": None,
            "node_id": "$batch_plan",
            "field": "batch_rows",
            "main": main_run.batch_rows,
            "reference": reference_run.batch_rows,
            "mask_difference": _mask_payload([], []),
        }
    if first_mismatch is None and len(main_run.batches) != len(reference_run.batches):
        first_mismatch = {
            "batch_index": 0,
            "expression_id": None,
            "node_id": "$batch_plan",
            "field": "batch_count",
            "main": len(main_run.batches),
            "reference": len(reference_run.batches),
            "mask_difference": _mask_payload([], []),
        }

    batch_limit = min(len(main_run.batches), len(reference_run.batches))
    expression_order = [
        expression["expression_id"] for expression in main_run.expressions
    ]
    for batch_idx in range(batch_limit):
        if first_mismatch is not None:
            break
        main_batch = main_run.batches[batch_idx]
        reference_batch = reference_run.batches[batch_idx]
        if (
            main_batch.batch_index != reference_batch.batch_index
            or main_batch.row_start != reference_batch.row_start
            or main_batch.row_count != reference_batch.row_count
        ):
            first_mismatch = {
                "batch_index": batch_idx,
                "expression_id": None,
                "node_id": "$batch_plan",
                "field": "row_range",
                "main": {
                    "batch_index": main_batch.batch_index,
                    "row_start": main_batch.row_start,
                    "row_count": main_batch.row_count,
                },
                "reference": {
                    "batch_index": reference_batch.batch_index,
                    "row_start": reference_batch.row_start,
                    "row_count": reference_batch.row_count,
                },
                "mask_difference": _mask_payload([], []),
            }
            break
        for expression_id in expression_order:
            if first_mismatch is not None:
                break
            main_node_map = main_batch.node_results.get(expression_id, {})
            reference_node_map = reference_batch.node_results.get(expression_id, {})
            expression_final = main_batch.expression_results[expression_id]
            reference_final = reference_batch.expression_results[expression_id]
            expressions_checked += 1
            main_ids = list(main_node_map)
            reference_ids = list(reference_node_map)
            if set(main_ids) != set(reference_ids):
                first_mismatch = {
                    "batch_index": batch_idx,
                    "expression_id": expression_id,
                    "node_id": "$node_set",
                    "field": "node_set",
                    "main_only": sorted(set(main_ids) - set(reference_ids)),
                    "reference_only": sorted(set(reference_ids) - set(main_ids)),
                    "mask_difference": _mask_payload([], []),
                }
                break
            for node_id in main_ids:
                nodes_checked += 1
                mismatch = _compare_node_result(
                    batch_index=batch_idx,
                    expression_id=expression_id,
                    node=main_node_map[node_id],
                    reference=reference_node_map[node_id],
                )
                if mismatch is not None:
                    first_mismatch = mismatch
                    break
            if first_mismatch is not None:
                break
            final_difference = _first_vector_difference(expression_final, reference_final)
            if final_difference is not None:
                first_mismatch = {
                    "batch_index": batch_idx,
                    "expression_id": expression_id,
                    "node_id": "$expression_result",
                    "op": "expression_result",
                    "row_scope": "full_batch",
                    "selection_context": None,
                    **final_difference,
                }
                break

    report = {
        "ok": first_mismatch is None,
        "mode": "independent_three_valued_reference",
        "batches_checked": batch_limit,
        "expressions_checked": expressions_checked,
        "nodes_checked": nodes_checked,
        "first_mismatch": first_mismatch,
        "first_mismatch_contract": (
            "报告首个不一致批次、表达式节点、结果位置与完整空值掩码差异"
        ),
    }
    if first_mismatch is not None and raise_on_mismatch:
        raise ReferenceMismatch(report)
    return report


def _stable_digest(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _model_canonical_payload(model: NormalizedModel) -> dict[str, Any]:
    return {
        "row_count": model.row_count,
        "columns": {
            name: {
                "dtype": vector.dtype.label,
                "values": list(vector.values),
                "null_mask": list(vector.null_mask),
            }
            for name, vector in model.columns.items()
        },
    }


def _run_result_payload(run: EvaluationRun | ReferenceRun) -> dict[str, Any]:
    return {
        "row_count": run.row_count,
        "results": {
            expression_id: {
                "dtype": vector.dtype.label,
                "values": list(vector.values),
                "null_mask": list(vector.null_mask),
            }
            for expression_id, vector in run.expression_results.items()
        },
    }


def compare_version_consistency(
    models: dict[str, NormalizedModel],
    main_runs: dict[str, EvaluationRun],
    reference_runs: dict[str, ReferenceRun],
) -> dict[str, Any]:
    """Verify canonical inputs/results are identical while schemas remain different."""
    versions = sorted(models)
    base = versions[0]
    canonical_differences: list[dict[str, Any]] = []
    expression_differences: list[dict[str, Any]] = []
    result_differences: list[dict[str, Any]] = []
    reference_result_differences: list[dict[str, Any]] = []
    result_hashes: dict[str, dict[str, Any]] = {}
    expression_hashes: dict[str, str] = {}
    for version in versions:
        model_payload = _model_canonical_payload(models[version])
        main_payload = _run_result_payload(main_runs[version])
        ref_payload = _run_result_payload(reference_runs[version])
        expression_hashes[version] = _stable_digest(models[version].expressions)
        result_hashes[version] = {
            "normalized_input_sha256": _stable_digest(model_payload),
            "normalized_expression_sha256": expression_hashes[version],
            "main_result_sha256": _stable_digest(main_payload),
            "reference_result_sha256": _stable_digest(ref_payload),
        }
        if version == base:
            continue
        if model_payload != _model_canonical_payload(models[base]):
            canonical_differences.append({"version": version, "base": base})
        if models[version].expressions != models[base].expressions:
            expression_differences.append({"version": version, "base": base})
        if main_payload != _run_result_payload(main_runs[base]):
            result_differences.append({"version": version, "base": base})
        if ref_payload != _run_result_payload(reference_runs[base]):
            reference_result_differences.append({"version": version, "base": base})

    identical_hashes = {
        "normalized_input": len(
            {entry["normalized_input_sha256"] for entry in result_hashes.values()}
        )
        == 1,
        "main_result": len(
            {entry["main_result_sha256"] for entry in result_hashes.values()}
        )
        == 1,
        "reference_result": len(
            {entry["reference_result_sha256"] for entry in result_hashes.values()}
        )
        == 1,
        "normalized_expressions": len(set(expression_hashes.values())) == 1,
    }
    ok = (
        not canonical_differences
        and not expression_differences
        and not result_differences
        and not reference_result_differences
        and all(identical_hashes.values())
    )
    return {
        "ok": ok,
        "versions_compared": versions,
        "canonical_inputs_identical": not canonical_differences,
        "normalized_expressions_identical": not expression_differences,
        "main_results_identical": not result_differences,
        "reference_results_identical": not reference_result_differences,
        "canonical_differences": canonical_differences,
        "normalized_expression_differences": expression_differences,
        "main_result_differences": result_differences,
        "reference_result_differences": reference_result_differences,
        "result_hashes": result_hashes,
        "identical_hash_groups": identical_hashes,
    }
