"""Cross-module consistency checks for blocked execution."""

from __future__ import annotations

from typing import Any

import numpy as np

from .errors import STAGES
from .models import OperatorResult, ToleranceConfig
from .tolerance import compare_arrays
from .util import array_sha256, sha256_json


def run_self_check(
    result: OperatorResult,
    tolerance: ToleranceConfig,
) -> dict[str, Any]:
    failed: list[str] = []
    if result.output is None or result.reference is None:
        return {
            "status": "not_applicable",
            "reason": "output or reference is absent",
            "failed_categories": [],
        }

    expected_indices = set(range(int(result.output.size)))
    block_ids = {block.block_id for block in result.blocks}
    reconstructed = np.full(int(result.output.size), np.nan, dtype=np.float64)
    seen: set[int] = set()
    block_duplicate = False
    block_out_of_range = False
    block_length_mismatch = False
    for block in result.blocks:
        values = np.asarray(block.output_values, dtype=np.float64)
        indices = [int(item) for item in block.element_indices]
        if len(indices) != len(values):
            block_length_mismatch = True
            continue
        for index, value in zip(indices, values.tolist()):
            if index < 0 or index >= reconstructed.size:
                block_out_of_range = True
                continue
            if index in seen:
                block_duplicate = True
                continue
            seen.add(index)
            reconstructed[index] = value

    if block_length_mismatch:
        failed.append("block_index_value_length")
    if block_duplicate:
        failed.append("block_duplicate_indices")
    if block_out_of_range:
        failed.append("block_index_out_of_range")
    if seen != expected_indices:
        failed.append("block_index_coverage")
    if np.any(~np.isfinite(reconstructed)):
        failed.append("block_output_nonfinite")

    stage_sets: dict[str, Any] = {}
    for stage in STAGES:
        indices = result.stage_indices.get(stage, set())
        stage_sets[stage] = {
            "count": len(indices),
            "checksum": sha256_json(sorted(int(item) for item in indices)),
        }
        if indices != expected_indices:
            failed.append(f"stage_indices:{stage}")

    reconstructed_sha = None
    spectrum_sha = None
    if not failed:
        reconstructed_sha = array_sha256(reconstructed.reshape(result.output.shape))
        if reconstructed_sha != array_sha256(result.output):
            failed.append("block_output_vs_final_output")
        if result.tolerance_report is None:
            failed.append("deviation_spectrum_missing")
        else:
            recomputed = compare_arrays(
                reconstructed.reshape(result.output.shape),
                result.reference,
                tolerance,
            )
            spectrum_sha = recomputed["spectrum_sha256"]
            if spectrum_sha != result.tolerance_report.get("spectrum_sha256"):
                failed.append("deviation_spectrum_recompute")

    diagnostic_codes = {item.code for item in result.diagnostics}
    for degradation in result.degradations:
        if degradation.get("trigger_code") not in diagnostic_codes:
            failed.append("degradation_trigger_code")
        if degradation.get("stage") not in STAGES:
            failed.append("degradation_stage")
        affected = degradation.get("affected_blocks", [])
        if any(block_id not in block_ids for block_id in affected):
            failed.append("degradation_block_trace")
        if result.status != "failed" and not affected:
            failed.append("degradation_block_trace_missing")

    unique_failed = sorted(set(failed))
    return {
        "status": "passed" if not unique_failed else "failed",
        "checked_categories": [
            "stage_element_indices",
            "deviation_spectrum_recompute",
            "degradation_block_trace",
        ],
        "failed_categories": unique_failed,
        "stage_sets": stage_sets,
        "reconstructed_output_sha256": reconstructed_sha,
        "recomputed_spectrum_sha256": spectrum_sha,
        "degradation_count": len(result.degradations),
    }
