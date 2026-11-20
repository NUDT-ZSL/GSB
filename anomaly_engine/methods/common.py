"""Shared deterministic score finalization."""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from ..canonical import canonical_sha256, quantize_float
from ..errors import EngineError


def chunk_boundaries(n_rows: int, requested_chunk: int) -> list[dict[str, int]]:
    chunk = max(1, min(int(requested_chunk), n_rows))
    return [
        {"start": start, "end": min(start + chunk, n_rows)}
        for start in range(0, n_rows, chunk)
    ]


def _score_summary(scores: np.ndarray) -> dict[str, float | int]:
    values = np.asarray(scores, dtype=np.float64)
    quantiles = np.quantile(values, [0.5, 0.95], method="linear")
    return {
        "count": int(values.size),
        "min": quantize_float(float(values.min())),
        "max": quantize_float(float(values.max())),
        "mean": quantize_float(float(values.mean())),
        "p50": quantize_float(float(quantiles[0])),
        "p95": quantize_float(float(quantiles[1])),
    }


def finalize_result(
    *,
    method: str,
    candidate_id: str,
    params: dict[str, Any],
    seed: int,
    sample_ids: tuple[str, ...],
    raw_scores: np.ndarray,
    row_chunk_size: int,
    algorithm_details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    values = np.asarray(raw_scores, dtype=np.float64)
    if values.ndim != 1 or values.shape[0] != len(sample_ids):
        raise EngineError(
            "E_INCONSISTENT",
            "method_score",
            "score vector length does not match sample ids",
            method=method,
            details={"category": "score_length", "candidate_id": candidate_id},
        )
    if not np.all(np.isfinite(values)):
        bad_index = int(np.flatnonzero(~np.isfinite(values))[0])
        raise EngineError(
            "E_INCONSISTENT",
            "method_score",
            "method emitted a non-finite anomaly score",
            method=method,
            details={
                "category": "non_finite_score",
                "candidate_id": candidate_id,
                "sample_id": sample_ids[bad_index],
            },
        )

    quantized = [quantize_float(float(value)) for value in values]
    score_details = [
        {"sample_id": sample_id, "score": score}
        for sample_id, score in zip(sample_ids, quantized)
    ]
    ranking = [
        sample_id
        for sample_id, _ in sorted(
            zip(sample_ids, quantized),
            key=lambda item: (-item[1], item[0]),
        )
    ]
    score_map = dict(zip(sample_ids, quantized))
    rank_map = {sample_id: position + 1 for position, sample_id in enumerate(ranking)}
    ranking_details = [
        {
            "rank": rank_map[sample_id],
            "sample_id": sample_id,
            "score": score_map[sample_id],
        }
        for sample_id in sample_ids
    ]
    score_payload = {
        "method": method,
        "candidate_id": candidate_id,
        "params": params,
        "seed": seed,
        "scores": score_details,
        "ranking": ranking,
    }
    return {
        "method": method,
        "candidate_id": candidate_id,
        "params": params,
        "seed": seed,
        "score_semantics": "higher_is_more_anomalous",
        "score_details": score_details,
        "ranking": ranking,
        "ranking_details": ranking_details,
        "score_summary": _score_summary(values),
        "score_checksum": canonical_sha256(score_payload),
        "execution": {
            "row_chunk_size": max(1, min(int(row_chunk_size), len(sample_ids))),
            "chunks": chunk_boundaries(len(sample_ids), row_chunk_size),
        },
        "algorithm_details": algorithm_details or {},
    }


def clip_distance_squared(value: float) -> float:
    return max(0.0, float(value))


def finite_or_error(value: float, *, method: str) -> float:
    converted = float(value)
    if not math.isfinite(converted):
        raise EngineError(
            "E_INCONSISTENT",
            "method_score",
            "method produced a non-finite intermediate score",
            method=method,
            details={"category": "non_finite_score"},
        )
    return converted
