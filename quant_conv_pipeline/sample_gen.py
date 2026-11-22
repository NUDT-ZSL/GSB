"""Deterministic local sample generation.

Everything is derived from fixed seeds and the generator config only;
no external datasets or network access. Produces:

    jobs.jsonl              conv job manifests (one JSON object per line)
    weight_params.jsonl     weight quantization params @ weight_revision
    calib_stats.jsonl       calibration stats @ calib_revision
    updates/weight_params_v2.jsonl   higher-revision weight params (subset)
    updates/calib_stats_v2.jsonl     higher-revision calib stats (subset)
    bad_jobs.jsonl          manifests that trip the validation chain
"""
from __future__ import annotations

import json
import os

import numpy as np

JOB_FIELDS = ("job_id", "input_shape", "kernel_shape", "stride", "padding",
              "quant_scale", "zero_point", "acc_dtype", "weight_revision",
              "calib_revision", "seed", "block_size")


def gen_activation(job):
    rng = np.random.default_rng(int(job["seed"]))
    return rng.standard_normal(tuple(job["input_shape"])) * 1.5


def gen_kernel(job):
    rng = np.random.default_rng(int(job["seed"]) + 1)
    return rng.standard_normal(tuple(job["kernel_shape"])) * 0.4


def _write_jsonl(path, rows):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")


def read_jsonl(path):
    rows = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _weight_params_row(job, revision):
    """Deterministic weight quant params for a job at a given revision."""
    w = gen_kernel(job)
    base = float(np.max(np.abs(w))) / 127.0
    scale = base * (1.0 + 0.01 * (revision - 1))
    zp = int((revision - 1) % 3)
    return {"job_id": job["job_id"], "weight_revision": revision,
            "weight_scale": scale, "weight_zero_point": zp}


def _calib_row(job, revision):
    """Deterministic calibration stats at a given revision."""
    from .conv_core import float_reference_conv
    rng = np.random.default_rng(int(job["seed"]) + 1000 + revision)
    cal = rng.standard_normal(tuple(job["input_shape"])) * 1.5
    ref = float_reference_conv(cal, gen_kernel(job), int(job["stride"]),
                               int(job["padding"]))
    max_abs = max(float(np.max(np.abs(ref))), 1e-6)
    scale = max_abs / 127.0 * 1.3 * (1.0 + 0.02 * (revision - 1))
    return {"job_id": job["job_id"], "calib_revision": revision,
            "out_scale": scale, "out_zero_point": 0}


def default_jobs(n_jobs=12):
    """A deterministic, varied job list."""
    jobs = []
    shapes = [((1, 3, 8, 8), (4, 3, 3, 3), 1, 1),
              ((2, 3, 8, 8), (4, 3, 3, 3), 2, 1),
              ((1, 4, 10, 9), (6, 4, 3, 3), 1, 0),
              ((1, 2, 12, 12), (3, 2, 5, 5), 2, 2),
              ((2, 4, 9, 9), (4, 4, 3, 3), 1, 1),
              ((1, 3, 16, 16), (8, 3, 3, 3), 2, 1),
              ((1, 6, 7, 7), (6, 6, 3, 3), 1, 1),
              ((2, 3, 10, 10), (5, 3, 3, 3), 1, 2),
              ((1, 4, 14, 14), (4, 4, 5, 5), 2, 2),
              ((1, 3, 9, 11), (4, 3, 3, 3), 1, 1),
              ((2, 2, 8, 8), (2, 2, 3, 3), 1, 0),
              ((1, 5, 11, 11), (5, 5, 3, 3), 1, 1)]
    for idx in range(n_jobs):
        ish, ksh, stride, pad = shapes[idx % len(shapes)]
        seed = 20240000 + idx * 17
        probe = {"seed": seed, "input_shape": list(ish)}
        act_max = float(np.max(np.abs(gen_activation(probe))))
        jobs.append({
            "job_id": f"job-{idx:03d}",
            "input_shape": list(ish),
            "kernel_shape": list(ksh),
            "stride": stride,
            "padding": pad,
            "quant_scale": round(act_max / 120.0, 8),
            "zero_point": (idx % 5) - 2,
            "acc_dtype": "int32",
            "weight_revision": 1,
            "calib_revision": 1,
            "seed": seed,
            "block_size": [16, 32, 64, 128][idx % 4],
        })
    return jobs


def generate(out_dir, n_jobs=12, weight_update_jobs=("job-002", "job-005", "job-007"),
             calib_update_jobs=("job-003", "job-007", "job-009")):
    """Generate the full deterministic sample tree under ``out_dir``."""
    jobs = default_jobs(n_jobs)
    by_id = {j["job_id"]: j for j in jobs}
    _write_jsonl(os.path.join(out_dir, "jobs.jsonl"), jobs)
    _write_jsonl(os.path.join(out_dir, "weight_params.jsonl"),
                 [_weight_params_row(j, 1) for j in jobs])
    _write_jsonl(os.path.join(out_dir, "calib_stats.jsonl"),
                 [_calib_row(j, 1) for j in jobs])
    _write_jsonl(os.path.join(out_dir, "updates", "weight_params_v2.jsonl"),
                 [_weight_params_row(by_id[j], 2) for j in weight_update_jobs])
    _write_jsonl(os.path.join(out_dir, "updates", "calib_stats_v2.jsonl"),
                 [_calib_row(by_id[j], 2) for j in calib_update_jobs])
    bad = [
        dict(jobs[0]),
        dict(jobs[1], kernel_shape=[4, 9, 3, 3]),
        dict(jobs[2], quant_scale=-0.01),
        dict(jobs[3], zero_point=200),
        dict(jobs[4], acc_dtype="int16"),
        dict(jobs[5], weight_revision=0),
    ]
    for i, row in enumerate(bad):
        # row 0 stays a duplicate of job-000; row 5 keeps job-005 so its
        # weight_revision=0 registers as a rollback against the registry
        row["job_id"] = row["job_id"] if i in (0, 5) else f"bad-{i:03d}"
    _write_jsonl(os.path.join(out_dir, "bad_jobs.jsonl"), bad)
    return jobs
