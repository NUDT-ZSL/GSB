"""Precision audit against the float64 serial reference path."""

from __future__ import annotations

import numpy as np

from .reference import reference_block
from .util import num_blocks

DEFAULT_ATOL = 1e-4
DEFAULT_RTOL = 1e-4


def _max_errors(actual: np.ndarray, expected: np.ndarray):
    actual64 = actual.astype(np.float64)
    expected64 = expected.astype(np.float64)
    abs_error = np.abs(actual64 - expected64)
    denom = np.maximum(np.abs(expected64), 1e-12)
    rel_error = abs_error / denom
    return float(np.max(abs_error)), float(np.max(rel_error)), abs_error, rel_error


def _attribute_block(
    block_index, queries, keys, values, block_size, scale, actual_output
):
    """Attribute float32 rounding to max selection, exp-sum, or weighted sum."""
    variants = {
        "row_max_selection": reference_block(
            block_index, queries, keys, values, block_size, scale, "max32"
        ).output.astype(np.float64),
        "exp_sum_accumulation": reference_block(
            block_index, queries, keys, values, block_size, scale, "sum32"
        ).output.astype(np.float64),
        "weighted_sum": reference_block(
            block_index, queries, keys, values, block_size, scale, "weighted32"
        ).output.astype(np.float64),
    }
    actual64 = actual_output.astype(np.float64)
    contributions = {
        name: float(np.max(np.abs(actual64 - tensor)))
        for name, tensor in variants.items()
    }
    # The stage whose float32 variant is closest to the engine output is the
    # dominant remaining error source.
    dominant = min(contributions, key=contributions.get)
    return dominant, contributions


def audit_engine(engine, atol=DEFAULT_ATOL, rtol=DEFAULT_RTOL) -> dict:
    state = engine.state
    block_size = state.config.block_size
    scale = state.scale_value()
    n_blocks = num_blocks(state.queries.shape[0], block_size)
    block_reports = []
    all_ok = True

    for index in range(n_blocks):
        if index not in state.results:
            all_ok = False
            block_reports.append({
                "block_index": index,
                "status": "missing",
                "code": "E_RECOMPUTE_PENDING",
            })
            continue
        actual = state.results[index]
        truth = reference_block(
            index,
            state.queries,
            state.keys,
            state.values,
            block_size,
            scale,
            "full",
        )
        max_abs, max_rel, abs_error, rel_error = _max_errors(
            actual.output, truth.output
        )
        row_max_abs, row_max_rel, _, _ = _max_errors(
            actual.row_max, truth.row_max
        )
        exp_sum_abs, exp_sum_rel, _, _ = _max_errors(
            actual.exp_sum, truth.exp_sum
        )
        over_mask = (abs_error > atol) | (rel_error > rtol)
        dominant, contributions = _attribute_block(
            index,
            state.queries,
            state.keys,
            state.values,
            block_size,
            scale,
            actual.output,
        )
        within_tolerance = not np.any(over_mask)
        all_ok = all_ok and within_tolerance
        block_reports.append({
            "block_index": index,
            "status": "ok" if within_tolerance else "out_of_tolerance",
            "max_abs_error": max_abs,
            "max_rel_error": max_rel,
            "row_max_abs_error": row_max_abs,
            "row_max_rel_error": row_max_rel,
            "exp_sum_abs_error": exp_sum_abs,
            "exp_sum_rel_error": exp_sum_rel,
            "out_of_tolerance_elements": int(np.count_nonzero(over_mask)),
            "dominant_error_stage": dominant if not within_tolerance else None,
            "stage_error_contributions": contributions,
            "fingerprint": actual.fingerprint,
            "reference_fingerprint": truth.fingerprint,
            "kv_block_range": list(actual.kv_block_range),
        })

    return {
        "atol": atol,
        "rtol": rtol,
        "all_within_tolerance": all_ok,
        "blocks": sorted(block_reports, key=lambda item: item["block_index"]),
    }


def verify_equivalence(engine, atol=DEFAULT_ATOL, rtol=DEFAULT_RTOL) -> dict:
    """Compare incremental state with a one-shot full recompute."""
    full = engine.full_recompute()
    state = engine.state
    mismatches = []
    for result in full:
        index = result.block_index
        if index not in state.results:
            mismatches.append({"block_index": index, "reason": "not-computed"})
            continue
        incremental = state.results[index]
        if incremental.fingerprint != result.fingerprint:
            mismatches.append({
                "block_index": index,
                "reason": "fingerprint-mismatch",
                "incremental": incremental.fingerprint,
                "full": result.fingerprint,
            })
        if not np.array_equal(incremental.output, result.output):
            mismatches.append({
                "block_index": index,
                "reason": "output-bytes-mismatch",
            })
        if not np.array_equal(incremental.row_max, result.row_max):
            mismatches.append({
                "block_index": index,
                "reason": "row-max-bytes-mismatch",
            })
        if not np.array_equal(incremental.exp_sum, result.exp_sum):
            mismatches.append({
                "block_index": index,
                "reason": "exp-sum-bytes-mismatch",
            })
    return {
        "equivalent": not mismatches,
        "mismatches": mismatches,
        "block_count": len(full),
    }
