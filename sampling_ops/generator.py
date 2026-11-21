"""Local deterministic JSONL task and logits generation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np

from .types import TaskSpec, resolve_dtype


_GENERATOR_STREAM = 0x6C6F67697473


def make_rng(seed: Any, *, stream: int = _GENERATOR_STREAM) -> np.random.Generator:
    """Create a local PCG64 generator without touching NumPy global state."""

    if isinstance(seed, bool) or seed is None:
        raise ValueError("seed must be a non-boolean integer")
    value = int(seed)
    seed_sequence = np.random.SeedSequence([value & 0xFFFFFFFF, stream, (value >> 32) & 0xFFFFFFFF])
    return np.random.Generator(np.random.PCG64(seed_sequence))


def generate_logits(
    task: TaskSpec | Mapping[str, Any],
    *,
    seed_override: Any | None = None,
    quantum_override: float | None = None,
) -> np.ndarray:
    """Generate deterministic ``(batch_size, vocab_size)`` logits for a task."""

    if isinstance(task, Mapping):
        spec = TaskSpec.from_mapping(task, fallback_case_id="generated")
    else:
        spec = task
    batch_size = int(spec.batch_size)
    vocab_size = int(spec.vocab_size)
    dtype = resolve_dtype(spec.dtype, case_id=spec.case_id)
    seed = spec.seed if seed_override is None else seed_override
    if seed is None:
        # A missing seed is a sample-stage failure.  Logits still need a stable
        # local source so the failure can be attributed to the correct stage.
        seed = 0
    rng = make_rng(seed)
    values = rng.standard_normal((batch_size, vocab_size), dtype=np.float64)
    quantum = spec.logits_quantum if quantum_override is None else quantum_override
    if quantum is not None:
        quantum_value = float(quantum)
        if not np.isfinite(quantum_value) or quantum_value <= 0:
            raise ValueError("logits_quantum must be finite and positive")
        values = np.round(values / quantum_value) * quantum_value
    return values.astype(dtype, copy=False)


def demo_tasks() -> list[dict[str, Any]]:
    """Return a stable, multi-scale demonstration manifest.

    ``group_size`` is the number of sampling draws per batch row.  All arrays
    are generated at runtime; this list only contains scalar task metadata.
    """

    return [
        {
            "case_id": "small-full-sort",
            "vocab_size": 257,
            "batch_size": 2,
            "seed": 1101,
            "k_mode": "auto",
            "k_value": 8,
            "temperature": 0.85,
            "top_p": 0.93,
            "group_size": 5,
            "dtype": "float64",
            "tie_policy": "index_asc",
            "resource_profile": {
                "max_compare": 50_000_000,
                "max_memory_bytes": 32 * 1024 * 1024,
                "chunk_size": 128,
            },
            "logits_quantum": 0.25,
        },
        {
            "case_id": "compare-tight-heap",
            "vocab_size": 4096,
            "batch_size": 3,
            "seed": 2202,
            "k_mode": "auto",
            "k_value": 16,
            "temperature": 1.05,
            "top_p": 0.90,
            "group_size": 6,
            "dtype": "float32",
            "tie_policy": "score_desc_index_asc",
            "resource_profile": {
                "max_compare": 100_000,
                "max_memory_bytes": 8 * 1024 * 1024,
                "compare_soft_ratio": 0.70,
                "chunk_size": 512,
            },
            "logits_quantum": 0.2,
        },
        {
            "case_id": "memory-tight-chunked",
            "vocab_size": 16384,
            "batch_size": 2,
            "seed": 3303,
            "k_mode": "auto",
            "k_value": 512,
            "temperature": 0.70,
            "top_p": 0.86,
            "group_size": 7,
            "dtype": "float64",
            "tie_policy": "stable_index",
            "resource_profile": {
                "max_compare": 2_000_000,
                "max_memory_bytes": 500 * 1024,
                "memory_soft_ratio": 0.70,
                "chunk_size": 1024,
            },
            "logits_quantum": 0.1,
        },
        {
            "case_id": "forced-routes-consistency",
            "vocab_size": 777,
            "batch_size": 2,
            "seed": 4404,
            "k_mode": "full_sort",
            "k_value": 23,
            "temperature": 1.20,
            "top_p": 0.95,
            "group_size": 4,
            "dtype": "float32",
            "tie_policy": "index_ascending",
            "resource_profile": {
                "max_compare": 50_000_000,
                "max_memory_bytes": 32 * 1024 * 1024,
                "chunk_size": 128,
            },
            "logits_quantum": 0.2,
        },
        {
            "case_id": "inject-candidate-nonfinite",
            "vocab_size": 64,
            "batch_size": 1,
            "seed": 5501,
            "k_mode": "auto",
            "k_value": 5,
            "temperature": 0.9,
            "top_p": 0.9,
            "group_size": 2,
            "dtype": "float64",
            "tie_policy": "index_asc",
            "resource_profile": {
                "max_compare": 1_000_000,
                "max_memory_bytes": 1024 * 1024,
                "chunk_size": 32,
            },
            "injections": [
                {
                    "injection_id": "cand-nan",
                    "stage": "candidate_filter",
                    "type": "non_finite_logits",
                    "field": "logits",
                    "value": "nan",
                }
            ],
        },
        {
            "case_id": "inject-temperature-zero",
            "vocab_size": 64,
            "batch_size": 1,
            "seed": 5502,
            "k_mode": "auto",
            "k_value": 5,
            "temperature": 0.9,
            "top_p": 0.9,
            "group_size": 2,
            "dtype": "float64",
            "tie_policy": "index_asc",
            "resource_profile": {
                "max_compare": 1_000_000,
                "max_memory_bytes": 1024 * 1024,
                "chunk_size": 32,
            },
            "injections": [
                {
                    "injection_id": "temp-zero",
                    "stage": "temperature_scale",
                    "type": "temperature_zero",
                    "field": "temperature",
                    "value": 0.0,
                }
            ],
        },
        {
            "case_id": "inject-top-p-range",
            "vocab_size": 64,
            "batch_size": 1,
            "seed": 5503,
            "k_mode": "auto",
            "k_value": 5,
            "temperature": 0.9,
            "top_p": 0.9,
            "group_size": 2,
            "dtype": "float64",
            "tie_policy": "index_asc",
            "resource_profile": {
                "max_compare": 1_000_000,
                "max_memory_bytes": 1024 * 1024,
                "chunk_size": 32,
            },
            "injections": [
                {
                    "injection_id": "top-p-range",
                    "stage": "top_p_filter",
                    "type": "top_p_out_of_range",
                    "field": "top_p",
                    "value": 1.5,
                }
            ],
        },
        {
            "case_id": "inject-seed-missing",
            "vocab_size": 64,
            "batch_size": 1,
            "seed": 5504,
            "k_mode": "auto",
            "k_value": 5,
            "temperature": 0.9,
            "top_p": 0.9,
            "group_size": 2,
            "dtype": "float64",
            "tie_policy": "index_asc",
            "resource_profile": {
                "max_compare": 1_000_000,
                "max_memory_bytes": 1024 * 1024,
                "chunk_size": 32,
            },
            "injections": [
                {
                    "injection_id": "seed-missing",
                    "stage": "sample",
                    "type": "seed_missing",
                    "field": "seed",
                    "value": None,
                }
            ],
        },
        {
            "case_id": "resource-overrun-check",
            "vocab_size": 4096,
            "batch_size": 1,
            "seed": 6606,
            "k_mode": "auto",
            "k_value": 128,
            "temperature": 1.0,
            "top_p": 0.88,
            "group_size": 3,
            "dtype": "float32",
            "tie_policy": "index_asc",
            "resource_profile": {
                "max_compare": 1000,
                "max_memory_bytes": 1024,
                "chunk_size": 256,
            },
            "logits_quantum": 0.25,
        },
    ]


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            value = json.loads(stripped)
            if not isinstance(value, dict):
                raise ValueError(f"line {line_no} is not a JSON object")
            rows.append(value)
    return rows


def write_jsonl(path: str | Path, rows: Iterable[Mapping[str, Any]]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(dict(row), sort_keys=True, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")

