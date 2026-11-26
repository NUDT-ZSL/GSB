from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from .cache import CacheStore, make_cache_key
from .errors import LedgerError
from .formats_dispatch import read_ledger
from .model import rhs_digests
from .ops import SparseOutput, spmm_fragment, spmspm_fragment
from .semantic import first_csr_diff, float_output_equal
from .workspace import CaseWorkspace


def _sparse_equal(left: SparseOutput, right: SparseOutput) -> dict[str, Any]:
    if left.nrows != right.nrows or left.ncols != right.ncols:
        return {"equal": False, "reason": "shape"}
    if not np.array_equal(left.row_ptr, right.row_ptr):
        return {"equal": False, "reason": "row_ptr"}
    if not np.array_equal(left.col_idx, right.col_idx):
        return {"equal": False, "reason": "col_idx"}
    comparison = float_output_equal(left.values, right.values)
    if not comparison["equal"]:
        return {"equal": False, "reason": "values", **comparison}
    return {"equal": True, "max_abs_diff": comparison["max_abs_diff"]}


def concatenate_sparse(outputs: list[SparseOutput], nrows: int, ncols: int) -> SparseOutput:
    row_ptr = [0]
    col_parts: list[np.ndarray] = []
    value_parts: list[np.ndarray] = []
    total_nnz = 0
    for output in outputs:
        row_ptr.extend((output.row_ptr[1:] + total_nnz).tolist())
        col_parts.append(output.col_idx)
        value_parts.append(output.values)
        total_nnz += int(output.col_idx.size)
    return SparseOutput(
        nrows=nrows,
        ncols=ncols,
        row_ptr=np.asarray(row_ptr, dtype=np.int64),
        col_idx=np.concatenate(col_parts) if col_parts else np.asarray([], dtype=np.int64),
        values=np.concatenate(value_parts) if value_parts else np.asarray([], dtype=np.float64),
        contrib_digest="assembled",
        output_digest="assembled",
    )


def verify_view_from_disk(
    ws: CaseWorkspace,
    version: int,
    *,
    cache: CacheStore | None = None,
) -> dict[str, Any]:
    stage = "result_summary"
    matrix, info = read_ledger(version, ws.ledger_path(version, committed=True))
    dense_b, sparse_b = ws.load_rhs()
    lineage = ws.load_lineage()
    interval_versions = {
        str(key): int(value) for key, value in lineage["interval_versions"].items()
    }
    rhs_info = rhs_digests(dense_b, sparse_b)
    cache = cache or CacheStore(ws.cache_dir)
    bounds = matrix.interval_bounds()

    fresh_spmm_parts: list[np.ndarray] = []
    cached_spmm_parts: list[np.ndarray] = []
    fresh_spmspm_parts: list[SparseOutput] = []
    cached_spmspm_parts: list[SparseOutput] = []
    interval_records: list[dict[str, Any]] = []

    for start, end in bounds:
        interval_id = matrix.interval_id(start, end)
        fragment = matrix.fragment(start, end)
        touched = np.unique(fragment.col_idx) if fragment.col_idx.size else np.asarray([], dtype=np.int64)
        upstream = {
            int(col): rhs_info["sparse_row_digests"][str(int(col))] for col in touched
        }
        key = make_cache_key(
            case_id=matrix.case_id,
            interval_id=interval_id,
            row_start=start,
            row_end=end,
            fragment_digest=fragment.digest,
            interval_write_version=int(interval_versions[interval_id]),
            format_source=version,
            dense_b_digest=rhs_info["dense_b_digest"],
            sparse_b_digest=rhs_info["sparse_b_digest"],
            upstream_row_digests=upstream,
        )
        entry = cache.load_entry(key)
        if entry.get("fragment_digest") != fragment.digest:
            raise LedgerError(
                "E_CROSS_VIEW_MISMATCH",
                "cache fragment digest does not match content restored from ledger",
                stage=stage,
                details={"view": f"v{version}", "interval_id": interval_id},
            )

        fresh_spmm, fresh_contrib, fresh_output_digest = spmm_fragment(fragment, dense_b)
        cached_spmm = np.asarray(entry["spmm"]["output"], dtype=np.float64)
        spmm_check = float_output_equal(fresh_spmm, cached_spmm)
        if not spmm_check["equal"]:
            raise LedgerError(
                "E_CROSS_VIEW_MISMATCH",
                "cached SpMM output differs from fresh float64 execution",
                stage=stage,
                details={
                    "view": f"v{version}",
                    "interval_id": interval_id,
                    **spmm_check,
                },
            )
        for field, fresh_hash in (
            ("contrib_digest", fresh_contrib),
            ("output_digest", fresh_output_digest),
        ):
            cached_hash = entry["spmm"][field]
            if cached_hash != fresh_hash:
                raise LedgerError(
                    "E_CROSS_VIEW_MISMATCH",
                    "SpMM intermediate contribution cache hash mismatch",
                    stage=stage,
                    details={
                        "view": f"v{version}",
                        "interval_id": interval_id,
                        "field": field,
                        "cached": cached_hash,
                        "fresh": fresh_hash,
                    },
                )

        fresh_spmspm = spmspm_fragment(fragment, sparse_b)
        cached_spmspm = SparseOutput.from_json_dict(entry["spmspm"])
        spmspm_check = _sparse_equal(fresh_spmspm, cached_spmspm)
        if not spmspm_check["equal"]:
            raise LedgerError(
                "E_CROSS_VIEW_MISMATCH",
                "cached SpMSpM output differs from fresh float64 execution",
                stage=stage,
                details={
                    "view": f"v{version}",
                    "interval_id": interval_id,
                    **spmspm_check,
                },
            )
        if cached_spmspm.contrib_digest != fresh_spmspm.contrib_digest:
            raise LedgerError(
                "E_CROSS_VIEW_MISMATCH",
                "SpMSpM intermediate contribution cache hash mismatch",
                stage=stage,
                details={"view": f"v{version}", "interval_id": interval_id},
            )

        fresh_spmm_parts.append(fresh_spmm)
        cached_spmm_parts.append(cached_spmm)
        fresh_spmspm_parts.append(fresh_spmspm)
        cached_spmspm_parts.append(cached_spmspm)
        interval_records.append(
            {
                "interval_id": interval_id,
                "cache_key": key,
                "fragment_digest": fragment.digest,
                "spmm_contrib_digest": fresh_contrib,
                "spmspm_contrib_digest": fresh_spmspm.contrib_digest,
                "spmm_output_digest": fresh_output_digest,
                "spmspm_output_digest": fresh_spmspm.output_digest,
            }
        )

    fresh_spmm_full = np.vstack(fresh_spmm_parts) if fresh_spmm_parts else np.zeros((0, dense_b.shape[1]))
    cached_spmm_full = np.vstack(cached_spmm_parts) if cached_spmm_parts else fresh_spmm_full
    fresh_spmspm_full = concatenate_sparse(fresh_spmspm_parts, matrix.nrows, sparse_b.ncols)
    cached_spmspm_full = concatenate_sparse(cached_spmspm_parts, matrix.nrows, sparse_b.ncols)
    return {
        "version": version,
        "matrix": matrix,
        "info": info,
        "intervals": interval_records,
        "fresh_spmm": fresh_spmm_full,
        "cached_spmm": cached_spmm_full,
        "fresh_spmspm": fresh_spmspm_full,
        "cached_spmspm": cached_spmspm_full,
    }


def adjudicate_from_disk(case_root: str | Path) -> dict[str, Any]:
    ws = CaseWorkspace(case_root)
    cache = CacheStore(ws.cache_dir)
    verified = {
        version: verify_view_from_disk(ws, version, cache=cache)
        for version in (1, 2, 3)
    }
    baseline = verified[1]
    report_views: dict[str, Any] = {}
    interval_reports: list[dict[str, Any]] = []

    for version in (1, 2, 3):
        view = verified[version]
        report_views[f"v{version}"] = {
            "content_hash": view["matrix"].digest,
            "reader_info": {
                key: value
                for key, value in view["info"].items()
                if key not in ("path",)
            },
            "interval_count": len(view["intervals"]),
            "interval_hashes": [
                {
                    "interval_id": item["interval_id"],
                    "spmm_contrib_digest": item["spmm_contrib_digest"],
                    "spmspm_contrib_digest": item["spmspm_contrib_digest"],
                }
                for item in view["intervals"]
            ],
        }

    for version in (2, 3):
        view = verified[version]
        diff = first_csr_diff(baseline["matrix"], view["matrix"])
        if diff is not None:
            raise LedgerError(
                "E_STRUCT_VIEW",
                "format views disagree in restored CSR structure or timestamps",
                stage="result_summary",
                details={"view": f"v{version}", "first_diff": diff},
            )
        spmm_check = float_output_equal(baseline["fresh_spmm"], view["fresh_spmm"])
        if not spmm_check["equal"]:
            raise LedgerError(
                "E_CROSS_VIEW_MISMATCH",
                "SpMM outputs differ across format views",
                stage="result_summary",
                details={"view": f"v{version}", "operation": "SpMM", **spmm_check},
            )
        spmspm_check = _sparse_equal(baseline["fresh_spmspm"], view["fresh_spmspm"])
        if not spmspm_check["equal"]:
            raise LedgerError(
                "E_CROSS_VIEW_MISMATCH",
                "SpMSpM outputs differ across format views",
                stage="result_summary",
                details={"view": f"v{version}", "operation": "SpMSpM", **spmspm_check},
            )
        for interval_index, (left_interval, right_interval) in enumerate(
            zip(baseline["intervals"], view["intervals"])
        ):
            for hash_field in (
                "spmm_contrib_digest",
                "spmspm_contrib_digest",
                "spmm_output_digest",
                "spmspm_output_digest",
            ):
                if left_interval[hash_field] != right_interval[hash_field]:
                    raise LedgerError(
                        "E_CROSS_VIEW_MISMATCH",
                        "interval contribution cache hash differs across views",
                        stage="result_summary",
                        details={
                            "interval_id": left_interval["interval_id"],
                            "hash_field": hash_field,
                            "baseline": left_interval[hash_field],
                            f"v{version}": right_interval[hash_field],
                        },
                    )

    for index, interval in enumerate(baseline["intervals"]):
        interval_reports.append(
            {
                "interval_id": interval["interval_id"],
                "v1_cache_key": verified[1]["intervals"][index]["cache_key"],
                "v2_cache_key": verified[2]["intervals"][index]["cache_key"],
                "v3_cache_key": verified[3]["intervals"][index]["cache_key"],
                "spmm_contrib_digest": interval["spmm_contrib_digest"],
                "spmspm_contrib_digest": interval["spmspm_contrib_digest"],
            }
        )

    return {
        "ok": True,
        "code": None,
        "case_id": baseline["matrix"].case_id,
        "struct_equal": True,
        "timestamp_aligned": True,
        "numeric_equal": True,
        "cache_hashes_equal": True,
        "max_abs_spmm_diff": 0.0,
        "max_abs_spmspm_diff": 0.0,
        "views": report_views,
        "intervals": interval_reports,
    }


def assemble_cached_result(
    ws: CaseWorkspace, version: int = 1
) -> tuple[np.ndarray, SparseOutput]:
    """Assemble persisted interval cache products without using old objects."""

    matrix, _info = read_ledger(version, ws.ledger_path(version, committed=True))
    dense_b, sparse_b = ws.load_rhs()
    lineage = ws.load_lineage()
    interval_versions = {
        str(key): int(value) for key, value in lineage["interval_versions"].items()
    }
    rhs_info = rhs_digests(dense_b, sparse_b)
    cache = CacheStore(ws.cache_dir)
    spmm_parts: list[np.ndarray] = []
    spmspm_parts: list[SparseOutput] = []
    for start, end in matrix.interval_bounds():
        interval_id = matrix.interval_id(start, end)
        fragment = matrix.fragment(start, end)
        touched = np.unique(fragment.col_idx) if fragment.col_idx.size else np.asarray([], dtype=np.int64)
        upstream = {
            int(col): rhs_info["sparse_row_digests"][str(int(col))] for col in touched
        }
        key = make_cache_key(
            case_id=matrix.case_id,
            interval_id=interval_id,
            row_start=start,
            row_end=end,
            fragment_digest=fragment.digest,
            interval_write_version=int(interval_versions[interval_id]),
            format_source=version,
            dense_b_digest=rhs_info["dense_b_digest"],
            sparse_b_digest=rhs_info["sparse_b_digest"],
            upstream_row_digests=upstream,
        )
        entry = cache.load_entry(key)
        spmm_parts.append(np.asarray(entry["spmm"]["output"], dtype=np.float64))
        spmspm_parts.append(SparseOutput.from_json_dict(entry["spmspm"]))
    spmm = np.vstack(spmm_parts)
    spmspm = concatenate_sparse(spmspm_parts, matrix.nrows, sparse_b.ncols)
    return spmm, spmspm
