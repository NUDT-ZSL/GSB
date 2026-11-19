from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from .canonical import atomic_write_text, canonical_json, sha256_bytes, write_jsonl
from .models import Job
from .quant_conv import generate_arrays

SAMPLE_SEED = 20261120


def _job(
    *,
    job_id: str,
    input_shape: tuple[int, int, int, int],
    kernel_shape: tuple[int, int, int, int],
    stride: tuple[int, int] = (1, 1),
    padding: tuple[int, int] = (1, 1),
    quant_scale: float,
    zero_point: int,
    seed: int,
    priority: int,
) -> Job:
    return Job(
        job_id=job_id,
        input_shape=input_shape,
        kernel_shape=kernel_shape,
        stride=stride,
        padding=padding,
        quant_scale=quant_scale,
        zero_point=zero_point,
        acc_dtype="int32",
        seed=seed,
        priority=priority,
    )


def default_sample_jobs(base_seed: int = SAMPLE_SEED) -> list[Job]:
    return [
        _job(
            job_id="job_fine_conv",
            input_shape=(1, 2, 8, 8),
            kernel_shape=(4, 2, 3, 3),
            quant_scale=0.01,
            zero_point=-3,
            seed=base_seed + 101,
            priority=90,
        ),
        _job(
            job_id="job_balanced_conv",
            input_shape=(1, 3, 10, 10),
            kernel_shape=(6, 3, 3, 3),
            quant_scale=0.03,
            zero_point=0,
            seed=base_seed + 202,
            priority=70,
        ),
        _job(
            job_id="job_coarse_conv",
            input_shape=(1, 2, 12, 12),
            kernel_shape=(4, 2, 3, 3),
            quant_scale=0.12,
            zero_point=5,
            seed=base_seed + 303,
            priority=80,
        ),
        _job(
            job_id="job_large_conv",
            input_shape=(2, 8, 24, 24),
            kernel_shape=(8, 8, 3, 3),
            quant_scale=0.02,
            zero_point=-1,
            seed=base_seed + 404,
            priority=60,
        ),
    ]


def conflict_demo_jobs(base_seed: int = SAMPLE_SEED) -> list[Job]:
    return [
        _job(
            job_id="conflict_priority_fine",
            input_shape=(1, 2, 8, 8),
            kernel_shape=(4, 2, 3, 3),
            quant_scale=0.01,
            zero_point=-3,
            seed=base_seed + 1101,
            priority=100,
        ),
        _job(
            job_id="conflict_mac_heavy",
            input_shape=(1, 8, 64, 64),
            kernel_shape=(16, 8, 3, 3),
            quant_scale=0.02,
            zero_point=0,
            seed=base_seed + 1202,
            priority=95,
        ),
        _job(
            job_id="conflict_coarse_memory",
            input_shape=(1, 1, 128, 128),
            kernel_shape=(32, 1, 1, 1),
            stride=(1, 1),
            padding=(0, 0),
            quant_scale=0.12,
            zero_point=2,
            seed=base_seed + 1303,
            priority=90,
        ),
        _job(
            job_id="conflict_fine_memory",
            input_shape=(2, 32, 64, 64),
            kernel_shape=(4, 32, 1, 1),
            stride=(1, 1),
            padding=(0, 0),
            quant_scale=0.015,
            zero_point=0,
            seed=base_seed + 1404,
            priority=85,
        ),
        _job(
            job_id="conflict_tiny_progress",
            input_shape=(1, 1, 5, 5),
            kernel_shape=(2, 1, 3, 3),
            stride=(1, 1),
            padding=(0, 0),
            quant_scale=0.04,
            zero_point=1,
            seed=base_seed + 1505,
            priority=80,
        ),
        _job(
            job_id="conflict_slot_denied",
            input_shape=(1, 1, 4, 4),
            kernel_shape=(1, 1, 1, 1),
            stride=(1, 1),
            padding=(0, 0),
            quant_scale=0.03,
            zero_point=0,
            seed=base_seed + 1606,
            priority=70,
        ),
    ]


def _array_digest(values: np.ndarray) -> str:
    contiguous = np.ascontiguousarray(values)
    header = canonical_json(
        {
            "dtype": contiguous.dtype.str,
            "shape": list(contiguous.shape),
            "fortran_order": False,
        }
    ).encode("utf-8")
    return sha256_bytes(header + contiguous.tobytes())


def write_samples(
    root: str | Path,
    jobs: list[Job],
    *,
    seed: int = SAMPLE_SEED,
    include_invalid: bool = True,
    extra_job_sets: dict[str, list[Job]] | None = None,
) -> dict[str, Any]:
    sample_root = Path(root)
    arrays_dir = sample_root / "arrays"
    sample_root.mkdir(parents=True, exist_ok=True)
    arrays_dir.mkdir(parents=True, exist_ok=True)
    jobs_path = sample_root / "jobs.jsonl"
    write_jsonl(jobs_path, [job.to_dict() for job in jobs])
    all_jobs: dict[str, Job] = {job.job_id: job for job in jobs}
    job_files = {"jobs.jsonl": [job.job_id for job in jobs]}
    for file_stem, extra_jobs in (extra_job_sets or {}).items():
        filename = f"{file_stem}.jsonl"
        write_jsonl(sample_root / filename, [job.to_dict() for job in extra_jobs])
        job_files[filename] = [job.job_id for job in extra_jobs]
        for job in extra_jobs:
            all_jobs[job.job_id] = job
    manifest: dict[str, Any] = {
        "format": "qconv-samples-v1",
        "seed": seed,
        "jobs_file": "jobs.jsonl",
        "jobs_checksum": sha256_bytes(jobs_path.read_bytes()),
        "job_files": job_files,
        "jobs": {},
    }
    for job in all_jobs.values():
        arrays = generate_arrays(job)
        values = {
            "input_float": arrays.input_float,
            "weight_float": arrays.weight_float,
            "input_q": arrays.input_q,
            "weight_q": arrays.weight_q,
            "input_dq": arrays.input_dq,
            "weight_dq": arrays.weight_dq,
        }
        relative = f"arrays/{job.job_id}.npz"
        np.savez_compressed(sample_root / relative, **values)
        manifest["jobs"][job.job_id] = {
            "file": relative,
            "seed": job.seed,
            "arrays": {
                name: {
                    "dtype": values[name].dtype.str,
                    "shape": list(values[name].shape),
                    "checksum": _array_digest(values[name]),
                }
                for name in sorted(values)
            },
        }
    if include_invalid:
        invalid = [
            {**jobs[0].to_dict()},
            {
                **jobs[1].to_dict(),
                "job_id": "invalid_shape",
                "input_shape": [1, 3],
            },
            {
                **jobs[1].to_dict(),
                "job_id": "invalid_scale",
                "quant_scale": 0,
            },
            {
                **jobs[1].to_dict(),
                "job_id": "invalid_zero_point",
                "zero_point": 300,
            },
            {
                **jobs[1].to_dict(),
                "job_id": "invalid_acc",
                "acc_dtype": "float64",
            },
        ]
        write_jsonl(sample_root / "invalid_jobs.jsonl", invalid)
        manifest["invalid_jobs_file"] = "invalid_jobs.jsonl"
    atomic_write_text(
        sample_root / "manifest.json",
        json_pretty(manifest),
    )
    return manifest


def json_pretty(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def verify_samples(root: str | Path, jobs: list[Job]) -> list[str]:
    sample_root = Path(root)
    problems: list[str] = []
    for job in jobs:
        path = sample_root / "arrays" / f"{job.job_id}.npz"
        if not path.exists():
            problems.append(f"missing {path}")
            continue
        with np.load(path) as saved:
            arrays = generate_arrays(job)
            checks = {
                "input_float": arrays.input_float,
                "weight_float": arrays.weight_float,
                "input_q": arrays.input_q,
                "weight_q": arrays.weight_q,
                "input_dq": arrays.input_dq,
                "weight_dq": arrays.weight_dq,
            }
            for name, expected in checks.items():
                if name not in saved or not np.array_equal(saved[name], expected):
                    problems.append(f"{job.job_id}:{name} does not match seed")
    return problems

