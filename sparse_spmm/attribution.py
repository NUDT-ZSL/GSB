"""Per-output-element error attribution against a float64 reference."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from .csr import CSRMatrix
from .errors import ConfigError, ToleranceError
from .kernels import RowKernelResult


@dataclass(frozen=True)
class PrecisionSettings:
    relative_tolerance: float
    absolute_tolerance: float
    attribution_top_k: int
    long_chain_nnz: int
    cancellation_ratio_threshold: float
    single_nonzero_share_threshold: float
    relative_error_floor: float


def precision_settings(config: dict[str, Any]) -> PrecisionSettings:
    execution = config.get("execution", {})
    tolerance = execution.get("tolerance", {})
    if not isinstance(tolerance, dict):
        raise ConfigError("execution.tolerance must be an object", stage="tolerance-validation")
    rel = float(tolerance.get("relative", 0.0))
    abs_tol = float(tolerance.get("absolute", 0.0))
    if not math.isfinite(rel) or not math.isfinite(abs_tol):
        raise ToleranceError(
            "tolerance values must be finite",
            stage="tolerance-validation",
            details={"relative": rel, "absolute": abs_tol},
        )
    if rel <= 0.0 or abs_tol < 0.0:
        raise ToleranceError(
            "relative tolerance must be > 0 and absolute tolerance must be >= 0",
            stage="tolerance-validation",
            details={"relative": rel, "absolute": abs_tol},
        )
    top_k = execution.get("attribution_top_k", 5)
    if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k <= 0:
        raise ConfigError(
            "execution.attribution_top_k must be a positive integer",
            stage="attribution-config",
            details={"value": repr(top_k)},
        )
    long_chain_nnz = execution.get("long_chain_nnz", 256)
    if isinstance(long_chain_nnz, bool) or not isinstance(long_chain_nnz, int) or long_chain_nnz <= 0:
        raise ConfigError(
            "execution.long_chain_nnz must be a positive integer",
            stage="attribution-config",
        )
    cancellation_ratio_threshold = float(execution.get("cancellation_ratio_threshold", 10.0))
    single_nonzero_share_threshold = float(execution.get("single_nonzero_share_threshold", 0.60))
    relative_error_floor = float(execution.get("relative_error_floor", 1e-30))
    return PrecisionSettings(
        relative_tolerance=rel,
        absolute_tolerance=abs_tol,
        attribution_top_k=int(top_k),
        long_chain_nnz=int(long_chain_nnz),
        cancellation_ratio_threshold=cancellation_ratio_threshold,
        single_nonzero_share_threshold=single_nonzero_share_threshold,
        relative_error_floor=relative_error_floor,
    )


def _classification(
    *,
    nnz: int,
    top_share: float,
    largest_term_share: float,
    cancellation_ratio: float,
    sign_changes: int,
    large_small_pairs: int,
    settings: PrecisionSettings,
) -> tuple[str, str]:
    single_score = max(top_share, largest_term_share)
    if single_score >= settings.single_nonzero_share_threshold:
        return (
            "SINGLE_LARGE_NONZERO",
            "one nonzero contributes the dominant rounding share or exact-term magnitude",
        )
    if (
        cancellation_ratio >= settings.cancellation_ratio_threshold
        and sign_changes > 0
        and large_small_pairs > 0
    ):
        return (
            "ADJACENT_CANCELLATION",
            "mixed-sign contributions with strongly differing magnitudes cause cancellation",
        )
    if nnz >= settings.long_chain_nnz:
        return (
            "LONG_CHAIN_ACCUMULATION",
            "many rounded additions accumulate without a single dominant term",
        )
    return ("MIXED_OR_SHORT_CHAIN", "no dominant factor exceeds the configured classifier thresholds")


def attribute_row(
    csr: CSRMatrix,
    B: np.ndarray,
    *,
    result: RowKernelResult,
    reference64: np.ndarray,
    alpha: float,
    settings: PrecisionSettings,
) -> list[dict[str, Any]]:
    if result.trace is None:
        raise ValueError("attribution requires a traced kernel result")
    trace = result.trace
    row = result.row
    start = int(csr.indptr[row])
    end = int(csr.indptr[row + 1])
    ids = csr.nonzero_ids[start:end]
    nnz = end - start
    N = int(result.values.size)
    alpha64 = float(alpha)
    elements: list[dict[str, Any]] = []
    stage_names = (
        "product_rounding",
        "chunk_local_accumulation_rounding",
        "chunk_merge_allocated_rounding",
    )

    for col in range(N):
        computed = float(result.values[col])
        reference = float(reference64[col])
        absolute_error = abs(computed - reference)
        relative_error = absolute_error / max(abs(reference), settings.relative_error_floor)
        tolerance_threshold = settings.absolute_tolerance + settings.relative_tolerance * abs(reference)
        exceeds = bool(absolute_error > tolerance_threshold)

        raw_by_nonzero = trace.contributions[:, col, :].sum(axis=1)
        propagated = alpha64 * raw_by_nonzero
        absolute_propagated = np.abs(propagated)
        total_abs = float(absolute_propagated.sum())
        order = sorted(
            range(nnz), key=lambda p: (-float(absolute_propagated[p]), ids[p])
        )
        top_indices = [p for p in order if absolute_propagated[p] > 0.0][: settings.attribution_top_k]
        contributions: list[dict[str, Any]] = []
        cumulative_abs = 0.0
        for p in top_indices:
            cumulative_abs += float(absolute_propagated[p])
            chunk_index = int(trace.positions[p, col, 0])
            offset = int(trace.positions[p, col, 1])
            chunk_start, chunk_end = trace.chunk_ranges[chunk_index]
            stage_raw = {
                stage_names[stage]: float(trace.contributions[p, col, stage])
                for stage in range(3)
                if trace.contributions[p, col, stage] != 0.0
            }
            contributions.append(
                {
                    "nz_id": ids[p],
                    "row": int(row),
                    "col": int(csr.indices[start + p]),
                    "global_nonzero_position": int(start + p),
                    "column_sorted_position": int(p + 1),
                    "chunk_index": chunk_index,
                    "position_in_chunk": offset + 1,
                    "chunk_range": [int(chunk_start), int(chunk_end)],
                    "local_rounding": float(raw_by_nonzero[p]),
                    "propagated_contribution": float(propagated[p]),
                    "stage_local_rounding": stage_raw,
                    "abs_share": float(absolute_propagated[p] / total_abs) if total_abs else 0.0,
                    "cumulative_share": float(cumulative_abs / total_abs) if total_abs else 0.0,
                }
            )

        top_share = float(absolute_propagated[order[0]] / total_abs) if nnz and total_abs else 0.0
        exact_terms = trace.terms[:, col]
        term_abs_sum = float(np.abs(exact_terms).sum())
        exact_sum = float(exact_terms.sum())
        largest_term_share = float(np.abs(exact_terms).max() / term_abs_sum) if term_abs_sum else 0.0
        cancellation_ratio = term_abs_sum / max(abs(exact_sum), 1e-300)
        actual_order = np.lexsort(
            (trace.positions[:, col, 1], trace.positions[:, col, 0])
        )
        ordered_terms = exact_terms[actual_order]
        nonzero_terms = ordered_terms[ordered_terms != 0.0]
        sign_changes = int(np.count_nonzero(np.signbit(nonzero_terms[1:]) != np.signbit(nonzero_terms[:-1]))) if nonzero_terms.size > 1 else 0
        magnitudes = np.abs(nonzero_terms)
        large_small_pairs = 0
        if magnitudes.size > 1:
            positive = magnitudes[magnitudes > 0.0]
            if positive.size > 1:
                ratios = np.maximum(positive[1:], positive[:-1]) / np.minimum(positive[1:], positive[:-1])
                large_small_pairs = int(np.count_nonzero(ratios >= 10.0))
        dominant_source, explanation = _classification(
            nnz=nnz,
            top_share=top_share,
            largest_term_share=largest_term_share,
            cancellation_ratio=cancellation_ratio,
            sign_changes=sign_changes,
            large_small_pairs=large_small_pairs,
            settings=settings,
        )

        b_values = np.abs(B[csr.indices[start:end], col].astype(np.float64))
        positive_b = b_values[b_values > 0.0]
        magnitude_ratio = float(positive_b.max() / positive_b.min()) if positive_b.size else 1.0

        epilogue_raw = trace.epilogue_rounding[:, col]
        epilogue_names = (
            "alpha_scale_rounding",
            "beta_product_rounding",
            "final_add_rounding",
        )
        epilogue = {
            name: float(epilogue_raw[index])
            for index, name in enumerate(epilogue_names)
        }
        estimated_error = float(alpha64 * raw_by_nonzero.sum() + epilogue_raw.sum())
        residual = float((computed - reference) - estimated_error)
        elements.append(
            {
                "element_id": f"C-r{row:06d}-c{col:06d}",
                "row": int(row),
                "col": int(col),
                "nnz": int(nnz),
                "computed_float32": computed,
                "reference_float64": reference,
                "absolute_error": float(absolute_error),
                "relative_error": float(relative_error),
                "tolerance_threshold": float(tolerance_threshold),
                "exceeds_tolerance": exceeds,
                "b_magnitude_ratio": magnitude_ratio,
                "dominant_source": dominant_source,
                "dominant_source_explanation": explanation,
                "classification_evidence": {
                    "single_nonzero_share_threshold": settings.single_nonzero_share_threshold,
                    "cancellation_ratio_threshold": settings.cancellation_ratio_threshold,
                    "long_chain_nnz": settings.long_chain_nnz,
                    "top_attribution_share": top_share,
                    "largest_exact_term_share": largest_term_share,
                    "cancellation_ratio": float(cancellation_ratio),
                    "sign_changes": sign_changes,
                    "large_small_adjacent_pairs": large_small_pairs,
                },
                "top1_abs_share": top_share,
                "largest_exact_term_share": largest_term_share,
                "cancellation_ratio": float(cancellation_ratio),
                "cancellation_scan_order": "actual_chunk_local_accumulation_order",
                "sign_changes": sign_changes,
                "large_small_adjacent_pairs": large_small_pairs,
                "total_abs_attributed_contribution": total_abs,
                "estimated_error_from_ledger": estimated_error,
                "unattributed_nonlinear_residual": residual,
                "epilogue_rounding": epilogue,
                "top_contributions": contributions,
                "attribution_note": (
                    "Local rounding is measured per FP32 operation. Chunk-merge "
                    "rounding is allocated by absolute rounded term magnitude; "
                    "epilogue rounding has no nonzero owner and is reported separately."
                ),
            }
        )
    return elements
