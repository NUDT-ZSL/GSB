"""Assemble deterministic, auditable report sections from row-block outputs."""

from __future__ import annotations

import platform
from collections import Counter
from typing import Any

import numpy as np

from . import __version__
from .attribution import PrecisionSettings
from .csr import CSRMatrix, data_fingerprint, structure_fingerprint
from .kernels import VALID_ORDERS, kernel_matrix_digest
from .layers import analyze_layers
from .utils import canonical_json_bytes, sha256_hex


def _assemble_matrix(
    completed: dict[str, dict[str, Any]], blocks: list[tuple[int, int, str]], key: str
) -> np.ndarray:
    rows: list[list[float]] = []
    for start, end, block_key in sorted(blocks, key=lambda item: item[0]):
        block = completed[block_key]
        values = block[key]
        if len(values) != end - start:
            raise ValueError(f"checkpoint block {block_key} has an invalid row count for {key}")
        rows.extend(values)
    return np.asarray(rows, dtype=np.float64)


def _assemble_rows(
    completed: dict[str, dict[str, Any]], blocks: list[tuple[int, int, str]], key: str
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for start, end, block_key in sorted(blocks, key=lambda item: item[0]):
        values = completed[block_key][key]
        if len(values) != end - start:
            raise ValueError(f"checkpoint block {block_key} has an invalid row-record count for {key}")
        records.extend(values)
    return records


def _matrix_section(matrix: np.ndarray, dtype: Any, rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "shape": [int(matrix.shape[0]), int(matrix.shape[1])],
        "dtype": np.dtype(dtype).name,
        "matrix": matrix.tolist(),
        "matrix_fingerprint": kernel_matrix_digest(matrix, dtype),
        "row_metadata": rows,
    }


def _delta_matrix(left: np.ndarray, right: np.ndarray) -> np.ndarray:
    return left.astype(np.float64) - right.astype(np.float64)


def _delta_summary(delta: np.ndarray) -> dict[str, Any]:
    absolute = np.abs(delta)
    return {
        "max_abs_delta": float(absolute.max()) if absolute.size else 0.0,
        "nonzero_delta_count": int(np.count_nonzero(delta)),
        "differing_element_count": int(np.count_nonzero(delta != 0.0)),
        "sum_abs_delta": float(absolute.sum()) if absolute.size else 0.0,
    }


def _element_lookup(elements: list[dict[str, Any]]) -> dict[tuple[int, int], dict[str, Any]]:
    return {(int(element["row"]), int(element["col"])): element for element in elements}


def assemble_report(
    *,
    sample_config: dict[str, Any],
    strategy_config: dict[str, Any],
    case_digest: str,
    csr: CSRMatrix,
    csr_metadata: dict[str, Any],
    strategy_decision: dict[str, Any],
    completed: dict[str, dict[str, Any]],
    blocks: list[tuple[int, int, str]],
    precision: PrecisionSettings,
    recovery: dict[str, Any],
) -> dict[str, Any]:
    selected_f32 = _assemble_matrix(completed, blocks, "selected_f32_rows")
    reference_f32 = _assemble_matrix(completed, blocks, "reference_f32_rows")
    reference_f64 = _assemble_matrix(completed, blocks, "reference_f64_rows")
    selected_f64 = _assemble_matrix(completed, blocks, "selected_f64_rows")
    elements: list[dict[str, Any]] = []
    for start, end, block_key in sorted(blocks, key=lambda item: item[0]):
        elements.extend(completed[block_key]["elements"])
    elements.sort(key=lambda element: (int(element["row"]), int(element["col"])))

    selected_f32_records = _assemble_rows(completed, blocks, "selected_f32_row_records")
    reference_f32_records = _assemble_rows(completed, blocks, "reference_f32_row_records")
    reference_f64_records = _assemble_rows(completed, blocks, "reference_f64_row_records")
    selected_f64_records = _assemble_rows(completed, blocks, "selected_f64_row_records")

    variant_matrices: dict[str, np.ndarray] = {}
    variant_records: dict[str, list[dict[str, Any]]] = {}
    for order in VALID_ORDERS:
        variant_matrices[order] = _assemble_matrix(
            completed, blocks, f"variant_f32_rows_{order}"
        )
        variant_records[order] = _assemble_rows(
            completed, blocks, f"variant_f32_row_records_{order}"
        )

    reference_f32_work = np.asarray(reference_f32, dtype=np.float32)
    reference_f64_work = np.asarray(reference_f64, dtype=np.float64)
    variants: dict[str, Any] = {}
    for order in VALID_ORDERS:
        matrix = np.asarray(variant_matrices[order], dtype=np.float32)
        delta_ref32 = _delta_matrix(matrix, reference_f32_work)
        delta_ref64 = _delta_matrix(matrix, reference_f64_work)
        variants[order] = {
            "order_id": f"CHUNKED_{order.upper()}|bs={strategy_decision['block_size']}",
            "matrix": matrix.tolist(),
            "matrix_fingerprint": kernel_matrix_digest(matrix, np.float32),
            "per_element_delta_vs_reference_f32": delta_ref32.tolist(),
            "per_element_delta_vs_reference_f64": delta_ref64.tolist(),
            "delta_vs_reference_f32_summary": _delta_summary(delta_ref32),
            "delta_vs_reference_f64_summary": _delta_summary(delta_ref64),
            "row_metadata": variant_records[order],
        }

    precision_delta = _delta_matrix(
        np.asarray(selected_f32, dtype=np.float32), np.asarray(selected_f64, dtype=np.float64)
    )
    layers = analyze_layers(elements, sample_config)
    threshold = float(sample_config.get("execution", {}).get("precision_delta_threshold", 1e-6))
    lookup = _element_lookup(elements)
    over_threshold: list[dict[str, Any]] = []
    for row in range(precision_delta.shape[0]):
        for col in range(precision_delta.shape[1]):
            delta = float(precision_delta[row, col])
            if abs(delta) > threshold:
                element = lookup[(row, col)]
                top_ids = [item["nz_id"] for item in element["top_contributions"][:3]]
                over_threshold.append(
                    {
                        "element_id": element["element_id"],
                        "row": row,
                        "col": col,
                        "float32_minus_float64": delta,
                        "abs_delta": abs(delta),
                        "reference_float64": element["reference_float64"],
                        "dominant_source": element["dominant_source"],
                        "attribution_top_nonzero_ids": top_ids,
                        "layer_id": element["layer_id"],
                    }
                )
    over_threshold.sort(key=lambda item: (-item["abs_delta"], item["row"], item["col"]))
    tier_source_counts = Counter(item["dominant_source"] for item in over_threshold)

    exceeded = [element for element in elements if element["exceeds_tolerance"]]
    source_counts = Counter(element["dominant_source"] for element in elements)
    violation_source_counts = Counter(element["dominant_source"] for element in exceeded)
    absolute_errors = np.asarray([element["absolute_error"] for element in elements], dtype=np.float64)
    relative_errors = np.asarray([element["relative_error"] for element in elements], dtype=np.float64)
    statistics = {
        "element_count": len(elements),
        "tolerance_violation_count": len(exceeded),
        "tolerance_violation_ratio": len(exceeded) / len(elements) if elements else 0.0,
        "max_absolute_error": float(absolute_errors.max()) if absolute_errors.size else 0.0,
        "max_relative_error": float(relative_errors.max()) if relative_errors.size else 0.0,
        "mean_absolute_error": float(absolute_errors.mean()) if absolute_errors.size else 0.0,
        "p95_absolute_error": float(np.percentile(absolute_errors, 95)) if absolute_errors.size else 0.0,
        "mean_top1_contribution_share": (
            sum(float(element["top1_abs_share"]) for element in elements) / len(elements)
            if elements
            else 0.0
        ),
        "dominant_source_counts": dict(sorted(source_counts.items())),
        "violation_dominant_source_counts": dict(sorted(violation_source_counts.items())),
        "order_variant_difference_counts_vs_reference_f32": {
            order: variants[order]["delta_vs_reference_f32_summary"]["nonzero_delta_count"]
            for order in VALID_ORDERS
        },
        "precision_delta_over_threshold_count": len(over_threshold),
        "precision_delta_threshold": threshold,
        "structure": {
            "M": csr.rows,
            "K": csr.cols,
            "N": int(selected_f32.shape[1]),
            "nnz": csr.nnz,
            "min_row_nnz": int(csr.row_nnz().min()),
            "max_row_nnz": int(csr.row_nnz().max()),
            "zero_row_count": int(np.count_nonzero(csr.row_nnz() == 0)),
            "row_nnz_p50": float(np.percentile(csr.row_nnz(), 50)),
            "row_nnz_p90": float(np.percentile(csr.row_nnz(), 90)),
            "row_nnz_p99": float(np.percentile(csr.row_nnz(), 99)),
        },
    }

    report = {
        "schema_version": 1,
        "component_version": __version__,
        "status": "PASS",
        "sample": {
            "sample_id": sample_config.get("sample_id", "unnamed"),
            "seed": sample_config.get("seed"),
            "shape": {
                "M": csr.rows,
                "K": csr.cols,
                "N": int(selected_f32.shape[1]),
            },
            "config_digest": case_digest,
            "config": sample_config,
        },
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "platform": platform.platform(),
            "machine": platform.machine(),
            "byte_order": "little" if np.little_endian else "big",
        },
        "structure": {
            **{
                key: value
                for key, value in csr_metadata.items()
                if key not in {"indptr", "indices", "nonzero_ids"}
            },
            "structure_fingerprint": structure_fingerprint(csr),
            "data_fingerprint": data_fingerprint(csr),
            "shape": [csr.rows, csr.cols],
            "nnz": csr.nnz,
            "indptr": csr.indptr.astype(int).tolist(),
            "indices": csr.indices.astype(int).tolist(),
            "nonzero_ids": csr.nonzero_ids,
        },
        "csr_validation": {
            "business_code": "E_OK",
            "valid": True,
            "checks": {
                "indptr_length_is_M_plus_one": True,
                "indptr_starts_at_zero": True,
                "indptr_is_non_decreasing": True,
                "indptr_last_equals_nnz": True,
                "column_indices_are_in_range": True,
                "columns_are_strictly_increasing_per_row": True,
                "nonzero_id_count_equals_nnz": True,
            },
            "invalid_business_code": "E_CSR_INVALID",
        },
        "strategy_decision": strategy_decision,
        "kernels": {
            "reference_float32": _matrix_section(reference_f32_work, np.float32, reference_f32_records),
            "reference_float64": _matrix_section(reference_f64_work, np.float64, reference_f64_records),
            "selected_float32": _matrix_section(
                np.asarray(selected_f32, dtype=np.float32), np.float32, selected_f32_records
            ),
            "selected_float64": _matrix_section(
                np.asarray(selected_f64, dtype=np.float64), np.float64, selected_f64_records
            ),
        },
        "order_variants": variants,
        "precision_comparison": {
            "strategy_id": strategy_decision["strategy_id"],
            "block_size": strategy_decision["block_size"],
            "order": strategy_decision["order"],
            "threshold": threshold,
            "per_element_delta_float32_minus_float64": precision_delta.tolist(),
            "max_abs_delta": float(np.abs(precision_delta).max()) if precision_delta.size else 0.0,
            "over_threshold_count": len(over_threshold),
            "over_threshold_elements": over_threshold,
            "dominant_source_counts_for_over_threshold": dict(sorted(tier_source_counts.items())),
            "attribution_basis": "selected_float32 local rounding ledger against reference_float64",
        },
        "attribution": {
            "basis": "selected_float32-vs-reference_float64",
            "tolerance": {
                "relative": precision.relative_tolerance,
                "absolute": precision.absolute_tolerance,
                "violation_rule": "abs_error > absolute + relative*abs(reference_float64)",
                "relative_error_floor": precision.relative_error_floor,
            },
            "top_k": precision.attribution_top_k,
            "element_count": len(elements),
            "elements": elements,
        },
        "layers": layers,
        "statistics": statistics,
        "failures": [],
        "business_codes": [],
        "injections": {
            "attempted_count": 0,
            "hit_count": 0,
            "attempted_ids": [],
            "hit_ids": [],
            "all_expected_codes_hit": True,
        },
        "recovery": recovery,
    }
    return report


def finalize_status(report: dict[str, Any], failures: list[dict[str, Any]]) -> dict[str, Any]:
    report["failures"] = failures
    codes = sorted({failure["business_code"] for failure in failures})
    report["business_codes"] = codes
    report["status"] = "PASS_WITH_INJECTED_FAILURES" if failures else "PASS"
    return report


def report_digest_without_recovery(report: dict[str, Any]) -> str:
    payload = {key: value for key, value in report.items() if key != "recovery"}
    return sha256_hex(canonical_json_bytes(payload))
