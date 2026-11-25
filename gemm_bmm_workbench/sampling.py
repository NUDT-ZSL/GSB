from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from .canonical import atomic_write_json, canonical_dumps
from .specs import SUPPORTED_DTYPES


def deterministic_arrays(spec: dict[str, Any]) -> dict[str, np.ndarray]:
    """Generate A/B/C from job seed and revision using stable SeedSequence."""

    seed = int(spec["seed"])
    content_seed = int(spec.get("content_seed", 0))
    seed_sequence = np.random.SeedSequence([seed & 0xFFFFFFFF, content_seed, 0x57424D42])
    role_seeds = seed_sequence.spawn(3)
    np_dtype = SUPPORTED_DTYPES[spec["dtype"]]
    arrays: dict[str, np.ndarray] = {}
    for role, role_seed in zip(("A", "B", "C"), role_seeds):
        rng = np.random.default_rng(role_seed)
        shape = tuple(spec["matrix_shapes"][role])
        values = rng.uniform(-1.0, 1.0, size=shape).astype(np_dtype)
        if spec.get("layout") == "col_major":
            values = np.asfortranarray(values)
        arrays[role] = values
    return arrays


def _job(
    job_id: str,
    op_kind: str,
    shapes: dict[str, list[int]],
    *,
    seed: int,
    block_size: int,
    dtype: str = "float32",
    layout: str = "row_major",
    alpha: float = 1.0,
    beta: float = 0.0,
    revision: int = 1,
) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "op_kind": op_kind,
        "matrix_shapes": shapes,
        "dtype": dtype,
        "layout": layout,
        "seed": seed,
        "block_size": block_size,
        "alpha": alpha,
        "beta": beta,
        "revision": revision,
    }


def scenario_batches() -> list[list[dict[str, Any]]]:
    batch_one = [
        _job(
            "gemm_content",
            "gemm",
            {"A": [8, 6], "B": [6, 7], "C": [8, 7]},
            seed=1001,
            block_size=4,
            alpha=1.0,
            beta=0.5,
        ),
        _job(
            "bmm_alpha",
            "bmm",
            {"A": [2, 5, 4], "B": [2, 4, 6], "C": [2, 5, 6]},
            seed=2002,
            block_size=3,
            dtype="float64",
            alpha=1.0,
            beta=1.0,
        ),
        _job(
            "gemm_block",
            "gemm",
            {"A": [6, 8], "B": [8, 5], "C": [6, 5]},
            seed=3003,
            block_size=4,
            alpha=0.75,
            beta=0.0,
        ),
        _job(
            "gemm_dtype",
            "gemm",
            {"A": [5, 5], "B": [5, 5], "C": [5, 5]},
            seed=4004,
            block_size=2,
            dtype="float32",
            alpha=1.0,
            beta=0.25,
        ),
        _job(
            "gemm_beta",
            "gemm",
            {"A": [4, 7], "B": [7, 4], "C": [4, 4]},
            seed=5005,
            block_size=3,
            alpha=-0.5,
            beta=0.25,
        ),
    ]

    batch_two = [
        {**batch_one[0], "revision": 2, "content_seed": 77001},
        {**batch_one[1], "revision": 2, "alpha": 1.5},
        {**batch_one[2], "revision": 2, "block_size": 2},
        {**batch_one[3], "revision": 2, "dtype": "float64"},
        {**batch_one[4], "revision": 2, "beta": 0.8},
        _job(
            "gemm_added",
            "gemm",
            {"A": [4, 3], "B": [3, 5], "C": [4, 5]},
            seed=6006,
            block_size=2,
            alpha=1.25,
            beta=-0.2,
        ),
    ]
    return [batch_one, batch_two]


def write_jsonl(path: str | Path, records: list[dict[str, Any]]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(canonical_dumps(record) for record in records) + "\n", encoding="utf-8", newline="\n")


def generate_sample_tree(root: str | Path) -> dict[str, str]:
    root_path = Path(root)
    batches = scenario_batches()
    batch_paths: list[str] = []
    npz_arrays: dict[str, np.ndarray] = {}
    final_records = batches[1]

    for index, batch in enumerate(batches, 1):
        relative = f"batches/batch-{index:03d}.jsonl"
        write_jsonl(root_path / relative, batch)
        batch_paths.append(relative)
        for record in batch:
            # Import locally to avoid a module cycle at import time.
            from .specs import normalize_job

            spec = normalize_job(record)
            arrays = deterministic_arrays(spec)
            for role, array in arrays.items():
                npz_arrays[f"{spec['job_id']}/r{spec['revision']}/{role}"] = array

    write_jsonl(root_path / "final-jobs.jsonl", final_records)
    npz_path = root_path / "matrices.npz"
    npz_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(npz_path, **npz_arrays)

    manifest = {
        "generator": "gemm_bmm_workbench.sampling",
        "numpy_version": np.__version__,
        "fixed_seed_family": "np.random.SeedSequence([seed, revision, salt])",
        "batches": batch_paths,
        "final_jobs": "final-jobs.jsonl",
        "matrices": "matrices.npz",
        "job_count_batch_1": len(batches[0]),
        "job_count_batch_2": len(batches[1]),
    }
    atomic_write_json(root_path / "sample-manifest.json", manifest)
    return manifest


def load_npz_arrays(npz_path: str | Path, spec: dict[str, Any]) -> dict[str, np.ndarray]:
    with np.load(npz_path, allow_pickle=False) as archive:
        prefix = f"{spec['job_id']}/r{spec['revision']}/"
        return {role: archive[prefix + role] for role in ("A", "B", "C") if prefix + role in archive.files}
