"""Built-in deterministic sample manifest."""

from __future__ import annotations

from typing import Any


SAMPLE_INDEX_SET: dict[str, Any] = {
    "kind": "generated",
    "num_tokens": 6,
    "hits_per_token": 2,
    "low": 1,
    "high": 15,
    "duplicate_of": [
        {"at": [2, 1], "source": [0, 0]},
        {"at": [5, 0], "source": [3, 1]},
    ],
    "pad_positions": [[1, 1], [4, 0]],
}


def _job(
    *,
    job_id: str,
    table_dtype: str,
    reduction_mode: str = "sum",
    gather_axis: int = 0,
    pad_policy: str | None = None,
    quant_scale: float = 0.03,
) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "vocab_size": 16,
        "embed_dim": 6,
        "table_dtype": table_dtype,
        "index_set": SAMPLE_INDEX_SET,
        "gather_axis": gather_axis,
        "reduction_mode": reduction_mode,
        "pad_index": 0,
        "scale": 0.8,
        "quant_scale": quant_scale,
        "quant_zero_point": 0,
        "seed": 20261121,
        "pad_policy": pad_policy,
    }


SAMPLE_JOB_MAPPINGS: list[dict[str, Any]] = [
    _job(job_id="emb-f64", table_dtype="float64"),
    _job(job_id="emb-f32", table_dtype="float32"),
    _job(job_id="emb-q16", table_dtype="qint16", quant_scale=0.001),
    _job(job_id="emb-q8", table_dtype="qint8"),
    _job(
        job_id="emb-mean-pad-exclude",
        table_dtype="qint8",
        reduction_mode="mean",
        pad_policy="exclude",
    ),
    _job(
        job_id="emb-mean-pad-zero",
        table_dtype="qint8",
        reduction_mode="mean",
        pad_policy="zero_weight",
    ),
    _job(
        job_id="emb-axis1",
        table_dtype="qint16",
        gather_axis=1,
        quant_scale=0.001,
    ),
    _job(
        job_id="emb-concat",
        table_dtype="qint8",
        reduction_mode="concat",
    ),
]
