from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from .adjudicate import (
    _sparse_equal,
    assemble_cached_result,
    verify_view_from_disk,
)
from .cache import CacheStore
from .errors import LedgerError
from .ops import spmm_full, spmspm_full
from .semantic import float_output_equal
from .state import load_all_snapshots
from .workspace import CaseWorkspace


def run_self_check(
    case_root: str | Path, *, require_committed_summary: bool = True
) -> dict[str, Any]:
    ws = CaseWorkspace(case_root)
    snapshots = load_all_snapshots(ws)
    required_stage_count = 4 if require_committed_summary else 3
    if len(snapshots) < required_stage_count:
        raise LedgerError(
            "E_INCONSISTENT",
            "expected committed stage snapshots",
            stage="self_check",
            details={"snapshot_count": len(snapshots)},
        )

    interval_sets = {
        snapshot.get("row_interval_set_hash") for snapshot in snapshots
    }
    if len(interval_sets) != 1:
        raise LedgerError(
            "E_INCONSISTENT",
            "stage snapshots disagree on row interval set",
            stage="self_check",
            details={"interval_set_hashes": sorted(interval_sets)},
        )
    for snapshot in snapshots:
        if set(snapshot.get("completed_intervals", [])) != set(
            snapshot.get("all_intervals", [])
        ):
            raise LedgerError(
                "E_INCONSISTENT",
                "snapshot contains an incomplete interval set",
                stage="self_check",
                details={"snapshot_version": snapshot.get("snapshot_version")},
            )

    latest = snapshots[-1]
    target_version = int(latest["write_version"])
    target_snapshots = [s for s in snapshots if int(s.get("write_version", -1)) == target_version]
    stage_indices = sorted(int(s["stage_index"]) for s in target_snapshots)
    expected_indices = [1, 2, 3, 4] if require_committed_summary else [1, 2, 3]
    if stage_indices != expected_indices:
        raise LedgerError(
            "E_INCONSISTENT",
            "latest write version does not have the required committed stages",
            stage="self_check",
            details={
                "write_version": target_version,
                "stages": stage_indices,
                "expected": expected_indices,
            },
        )

    recompute_snapshot = next(
        s for s in target_snapshots if int(s["stage_index"]) == 3
    )
    recompute_report = recompute_snapshot.get("report", {})
    all_intervals = set(recompute_report.get("all_intervals", []))
    recomputed = set(recompute_report.get("recomputed_intervals", []))
    reused = set(recompute_report.get("reused_intervals", []))
    if recomputed & reused:
        raise LedgerError(
            "E_INCONSISTENT",
            "recomputed and reused intervals overlap",
            stage="self_check",
            details={"overlap": sorted(recomputed & reused)},
        )
    if recomputed | reused != all_intervals or not all_intervals:
        raise LedgerError(
            "E_INCONSISTENT",
            "recompute/reuse coverage is not a complete partition",
            stage="self_check",
            details={
                "all": sorted(all_intervals),
                "recomputed": sorted(recomputed),
                "reused": sorted(reused),
            },
        )

    cache = CacheStore(ws.cache_dir)
    for version in (1, 2, 3):
        verify_view_from_disk(ws, version, cache=cache)

    matrix = ws.load_committed_csr()
    dense_b, sparse_b = ws.load_rhs()
    incremental_spmm, incremental_spmspm = assemble_cached_result(ws, 1)
    full_spmm, _, _ = spmm_full(matrix, dense_b)
    full_spmspm = spmspm_full(matrix, sparse_b)

    spmm_comparison = float_output_equal(incremental_spmm, full_spmm)
    if not spmm_comparison["equal"]:
        raise LedgerError(
            "E_INCONSISTENT",
            "incremental SpMM differs from one-shot whole-matrix rebuild",
            stage="self_check",
            details=spmm_comparison,
        )
    spmspm_comparison = _sparse_equal(incremental_spmspm, full_spmspm)
    if not spmspm_comparison["equal"]:
        raise LedgerError(
            "E_INCONSISTENT",
            "incremental SpMSpM differs from one-shot whole-matrix rebuild",
            stage="self_check",
            details=spmspm_comparison,
        )

    return {
        "ok": True,
        "code": None,
        "snapshot_count": len(snapshots),
        "latest_write_version": target_version,
        "stage_interval_set_hash": latest.get("row_interval_set_hash"),
        "all_intervals": sorted(all_intervals),
        "recomputed_intervals": sorted(recomputed),
        "reused_intervals": sorted(reused),
        "cache_hashes_reproduced": True,
        "spmm_elementwise_equal": True,
        "spmspm_elementwise_equal": True,
        "spmm_max_abs_diff": spmm_comparison["max_abs_diff"],
        "spmspm_max_abs_diff": spmspm_comparison.get("max_abs_diff", 0.0),
        "float_dtype": "float64",
    }
