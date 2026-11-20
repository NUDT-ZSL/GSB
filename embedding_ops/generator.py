"""Fixed-seed NumPy sample generation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .errors import E_SCALE_NEGATIVE, business_error
from .indexing import generate_index_array
from .models import JobSpec
from .util import array_digest


@dataclass(frozen=True)
class JobArrays:
    truth_table: np.ndarray
    indices: np.ndarray
    position_weights: np.ndarray
    table_digest: str
    index_digest: str
    weight_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "truth_table_shape": list(self.truth_table.shape),
            "indices_shape": list(self.indices.shape),
            "position_weights_shape": list(self.position_weights.shape),
            "truth_table_digest": self.table_digest,
            "raw_indices_digest": self.index_digest,
            "position_weights_digest": self.weight_digest,
        }


def _rng(seed: int, stream: int) -> np.random.Generator:
    return np.random.default_rng(np.random.SeedSequence([seed, stream]))


def generate_job_arrays(spec: JobSpec) -> JobArrays:
    if spec.scale < 0.0:
        raise business_error(
            E_SCALE_NEGATIVE,
            "table_build",
            spec.job_id,
            f"scale must be non-negative, got {spec.scale}",
            scale=spec.scale,
        )
    indices = generate_index_array(
        spec.index_set,
        vocab_size=spec.vocab_size,
        pad_index=spec.effective_pad_index,
        seed=spec.seed,
        job_id=spec.job_id,
    )
    array = np.asarray(indices)
    if array.ndim == 1:
        num_tokens = array.shape[0]
    elif array.ndim == 2:
        num_tokens = array.shape[0]
    else:
        num_tokens = 0
    table_rng = _rng(spec.seed, 0)
    truth_table = np.ascontiguousarray(
        table_rng.standard_normal((spec.vocab_size, spec.embed_dim)),
        dtype=np.float64,
    )
    weight_rng = _rng(spec.seed, 2)
    position_weights = np.ascontiguousarray(
        weight_rng.standard_normal((max(num_tokens, 0), spec.embed_dim))
        * spec.scale,
        dtype=np.float64,
    )
    return JobArrays(
        truth_table=truth_table,
        indices=array,
        position_weights=position_weights,
        table_digest=array_digest(truth_table),
        index_digest=array_digest(array),
        weight_digest=array_digest(position_weights),
    )
