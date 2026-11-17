from __future__ import annotations

from typing import Any

import numpy as np

from .data import Dataset
from .utils import digest_json


BINNING_VERSION = 1
TIE_POLICY = "stable_original_row_index"


def binning_cache_key(dataset: Dataset, n_bins: int) -> str:
    return digest_json(
        {
            "stage": "binning",
            "version": BINNING_VERSION,
            "data_fingerprint": dataset.data_fingerprint,
            "feature_names": dataset.feature_names,
            "feature_kinds": dataset.feature_kinds,
            "n_bins": int(n_bins),
            "tie_policy": TIE_POLICY,
        }
    )


def _numeric_bins(values: np.ndarray, n_bins: int) -> tuple[np.ndarray, list[int], list[int]]:
    rows = len(values)
    row_indices = np.arange(rows, dtype=np.int64)
    order = np.lexsort((row_indices, values))
    ranks = np.empty(rows, dtype=np.int64)
    ranks[order] = np.arange(rows, dtype=np.int64)
    boundaries = [int((index * rows) // n_bins) for index in range(n_bins + 1)]
    internal_boundaries = np.asarray(boundaries[1:-1], dtype=np.int64)
    bins = np.searchsorted(internal_boundaries, ranks, side="right").astype(np.int32)
    counts = np.bincount(bins, minlength=n_bins).astype(int).tolist()
    return bins, boundaries, counts


def _categorical_bins(values: np.ndarray) -> tuple[np.ndarray, list[int], list[int]]:
    codes = np.asarray(values, dtype=np.int64)
    unique_codes = sorted(int(value) for value in np.unique(codes))
    bins = np.ascontiguousarray(codes, dtype=np.int32)
    boundaries = [-1, *unique_codes]
    counts = [int(np.sum(codes == code)) for code in unique_codes]
    return bins, boundaries, counts


def build_binning(dataset: Dataset, n_bins: int) -> dict[str, Any]:
    rows = dataset.rows
    feature_count = dataset.feature_count
    sample_bins = np.empty((rows, feature_count), dtype=np.int32)
    feature_records: list[dict[str, Any]] = []
    for feature_index, name in enumerate(dataset.feature_names):
        kind = dataset.feature_kinds[feature_index]
        values = dataset.X[:, feature_index]
        if kind == "categorical":
            bins, boundaries, counts = _categorical_bins(values)
            method = "category_codes"
        else:
            bins, boundaries, counts = _numeric_bins(values, n_bins)
            method = "equal_frequency_stable_ties"
        sample_bins[:, feature_index] = bins
        feature_records.append(
            {
                "feature_index": feature_index,
                "name": name,
                "kind": kind,
                "method": method,
                "n_bins": int(n_bins if kind != "categorical" else len(counts)),
                "boundaries": boundaries,
                "bin_counts": counts,
            }
        )

    fingerprint = digest_json(
        {
            "version": BINNING_VERSION,
            "data_fingerprint": dataset.data_fingerprint,
            "n_bins": int(n_bins),
            "tie_policy": TIE_POLICY,
            "features": feature_records,
            "sample_bins": sample_bins,
        }
    )
    return {
        "version": BINNING_VERSION,
        "fingerprint": fingerprint,
        "cache_key": binning_cache_key(dataset, n_bins),
        "data_fingerprint": dataset.data_fingerprint,
        "n_bins": int(n_bins),
        "tie_policy": TIE_POLICY,
        "feature_names": list(dataset.feature_names),
        "feature_kinds": list(dataset.feature_kinds),
        "features": feature_records,
        "sample_bins": sample_bins,
        "shape": [rows, feature_count],
    }

