from __future__ import annotations

from typing import Any

import numpy as np

from .cache import CacheStore, make_cache_key
from .errors import LedgerError
from .model import CSRMatrix, rhs_digests
from .ops import spmm_fragment, spmspm_fragment


def _check_partition(
    all_ids: list[str], recomputed: list[str], reused: list[str]
) -> None:
    recomputed_set = set(recomputed)
    reused_set = set(reused)
    if recomputed_set & reused_set:
        raise LedgerError(
            "E_INCONSISTENT",
            "recomputed and reused interval sets overlap",
            stage="incremental_recompute",
            details={"overlap": sorted(recomputed_set & reused_set)},
        )
    if recomputed_set | reused_set != set(all_ids):
        raise LedgerError(
            "E_INCONSISTENT",
            "recomputed and reused intervals do not cover all row intervals",
            stage="incremental_recompute",
            details={
                "missing": sorted(set(all_ids) - (recomputed_set | reused_set)),
                "unexpected": sorted((recomputed_set | reused_set) - set(all_ids)),
            },
        )


def recompute_view(
    matrix: CSRMatrix,
    *,
    format_source: int,
    cache: CacheStore,
    dense_b: np.ndarray,
    sparse_b: CSRMatrix,
    interval_versions: dict[str, int],
    rhs_info: dict[str, Any] | None = None,
    injection: str | None = None,
    event_hook: Any | None = None,
) -> dict[str, Any]:
    stage = "incremental_recompute"
    rhs_info = rhs_info or rhs_digests(dense_b, sparse_b)
    bounds = matrix.interval_bounds()
    all_ids = [matrix.interval_id(start, end) for start, end in bounds]
    recomputed: list[str] = []
    reused: list[str] = []
    interval_results: list[dict[str, Any]] = []
    new_keys: list[str] = []
    injection_fired = False

    for interval_index, ((start, end), interval_id) in enumerate(zip(bounds, all_ids)):
        fragment = matrix.fragment(start, end)
        interval_write_version = int(interval_versions.get(interval_id, 0))
        touched_columns = np.unique(fragment.col_idx) if fragment.col_idx.size else np.asarray([], dtype=np.int64)
        upstream = {
            int(column): rhs_info["sparse_row_digests"][str(int(column))]
            for column in touched_columns
        }
        cache_key = make_cache_key(
            case_id=matrix.case_id,
            interval_id=interval_id,
            row_start=start,
            row_end=end,
            fragment_digest=fragment.digest,
            interval_write_version=interval_write_version,
            format_source=format_source,
            dense_b_digest=rhs_info["dense_b_digest"],
            sparse_b_digest=rhs_info["sparse_b_digest"],
            upstream_row_digests=upstream,
        )

        if cache.key_is_committed(cache_key):
            entry = cache.load_entry(cache_key)
            reused.append(interval_id)
            interval_results.append(
                {
                    "interval_id": interval_id,
                    "row_start": start,
                    "row_end": end,
                    "status": "reused",
                    "cache_key": cache_key,
                    "fragment_digest": fragment.digest,
                    "spmm_contrib_digest": entry["spmm"]["contrib_digest"],
                    "spmspm_contrib_digest": entry["spmspm"]["contrib_digest"],
                }
            )
            continue

        if injection == "column_oob" and not injection_fired:
            injection_fired = True
            bad_column = matrix.ncols + 99
            raise LedgerError(
                "E_STRUCT",
                "injected column index out of bounds during recompute",
                stage=stage,
                details={
                    "injection": "recompute_column_oob",
                    "interval_id": interval_id,
                    "column": bad_column,
                    "ncols": matrix.ncols,
                },
            )

        for column in fragment.col_idx:
            if int(column) < 0 or int(column) >= matrix.ncols:
                raise LedgerError(
                    "E_STRUCT",
                    "recompute fragment contains an out-of-bounds column",
                    stage=stage,
                    details={"interval_id": interval_id, "column": int(column)},
                )

        cache.store_fragment(fragment)
        spmm_output, spmm_contrib_digest, spmm_output_digest = spmm_fragment(
            fragment, dense_b
        )
        spmspm_output = spmspm_fragment(fragment, sparse_b)
        entry = {
            "cache_key": cache_key,
            "case_id": matrix.case_id,
            "format_source": f"v{format_source}",
            "interval_id": interval_id,
            "row_start": start,
            "row_end": end,
            "interval_write_version": interval_write_version,
            "fragment_digest": fragment.digest,
            "upstream_row_digests": upstream,
            "dense_b_digest": rhs_info["dense_b_digest"],
            "sparse_b_digest": rhs_info["sparse_b_digest"],
            "spmm": {
                "shape": list(spmm_output.shape),
                "output": spmm_output.tolist(),
                "contrib_digest": spmm_contrib_digest,
                "output_digest": spmm_output_digest,
            },
            "spmspm": spmspm_output.to_json_dict(),
        }
        cache.store_entry(entry)
        new_keys.append(cache_key)
        recomputed.append(interval_id)
        if event_hook is not None:
            event_hook(
                "interval_recomputed",
                {
                    "interval_id": interval_id,
                    "format_source": f"v{format_source}",
                    "cache_key": cache_key,
                },
            )
        interval_results.append(
            {
                "interval_id": interval_id,
                "row_start": start,
                "row_end": end,
                "status": "recomputed",
                "cache_key": cache_key,
                "fragment_digest": fragment.digest,
                "spmm_contrib_digest": spmm_contrib_digest,
                "spmspm_contrib_digest": spmspm_output.contrib_digest,
            }
        )

    _check_partition(all_ids, recomputed, reused)
    return {
        "ok": True,
        "format_source": f"v{format_source}",
        "all_intervals": all_ids,
        "recomputed_intervals": recomputed,
        "reused_intervals": reused,
        "interval_results": interval_results,
        "new_cache_keys": new_keys,
        "trigger_write_version": max(interval_versions.values(), default=0),
    }


def recompute_all_views(
    views: dict[int, CSRMatrix],
    *,
    cache: CacheStore,
    dense_b: np.ndarray,
    sparse_b: CSRMatrix,
    interval_versions: dict[str, int],
    injection: str | None = None,
    event_hook: Any | None = None,
) -> dict[str, Any]:
    rhs_info = rhs_digests(dense_b, sparse_b)
    view_reports: dict[str, Any] = {}
    common_recomputed: list[str] | None = None
    common_reused: list[str] | None = None
    new_keys: list[str] = []

    for version in sorted(views):
        report = recompute_view(
            views[version],
            format_source=version,
            cache=cache,
            dense_b=dense_b,
            sparse_b=sparse_b,
            interval_versions=interval_versions,
            rhs_info=rhs_info,
            injection=injection,
            event_hook=event_hook,
        )
        view_reports[f"v{version}"] = report
        new_keys.extend(report["new_cache_keys"])
        if common_recomputed is None:
            common_recomputed = report["recomputed_intervals"]
            common_reused = report["reused_intervals"]
        elif (
            common_recomputed != report["recomputed_intervals"]
            or common_reused != report["reused_intervals"]
        ):
            raise LedgerError(
                "E_INCONSISTENT",
                "three format views disagree on recompute/reuse intervals",
                stage="incremental_recompute",
                details={
                    "expected_recomputed": common_recomputed,
                    "view": f"v{version}",
                    "actual_recomputed": report["recomputed_intervals"],
                },
            )

    all_ids = next(iter(view_reports.values()))["all_intervals"]
    _check_partition(all_ids, common_recomputed or [], common_reused or [])
    trigger_version = max(interval_versions.values(), default=0)
    return {
        "ok": True,
        "all_intervals": all_ids,
        "recomputed_intervals": common_recomputed or [],
        "reused_intervals": common_reused or [],
        "trigger_write_version": trigger_version,
        "views": view_reports,
        "new_cache_keys": sorted(set(new_keys)),
    }
