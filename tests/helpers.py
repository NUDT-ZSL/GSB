from __future__ import annotations

from pathlib import Path

import numpy as np

from flash_attn_cache import AttentionCase, QKVArrays
from flash_attn_cache.samples import load_sample_arrays


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def sample(case_id: str) -> tuple[AttentionCase, QKVArrays]:
    from flash_attn_cache.validation import load_cases_jsonl

    cases = {
        case.case_id: case
        for case in load_cases_jsonl(SAMPLES / "cases.jsonl")
    }
    case = cases[case_id]
    return case, load_sample_arrays(SAMPLES, case)


def random_case(
    *,
    case_id: str = "unit",
    seq_len: int = 8,
    head_dim: int = 3,
    n_heads: int = 2,
    block_size: int = 3,
    causal: bool = True,
    dtype: str = "float64",
    seed: int = 7,
    kv_revision: int | tuple[int, ...] = 0,
) -> tuple[AttentionCase, QKVArrays]:
    rng = np.random.default_rng(seed)
    shape = (n_heads, seq_len, head_dim)
    arrays = QKVArrays(
        q=rng.standard_normal(shape, dtype=np.dtype(dtype)),
        k=rng.standard_normal(shape, dtype=np.dtype(dtype)),
        v=rng.standard_normal(shape, dtype=np.dtype(dtype)),
    )
    case = AttentionCase(
        case_id=case_id,
        seq_len=seq_len,
        head_dim=head_dim,
        n_heads=n_heads,
        block_size=block_size,
        causal=causal,
        dtype=dtype,
        seed=seed,
        kv_revision=kv_revision,
    )
    return case, arrays
