"""Deterministic synthetic sample generation."""

import json
import os

import numpy as np

from .io_utils import atomic_write_json, atomic_write_text

DEFAULTS = {
    "seed": 20240501,
    "n_samples": 24,
    "n_keys": 6,
    "dim": 64,
    "train_ctx_len": 512,
    "extrap_target_len": 2048,
    "base_freq": 10000.0,
    "scale_factor": 4.0,
    "precision": "fp32",
    "max_workers": 4,
    "tolerance": 1e-6,
}


def _round(values):
    return np.round(np.asarray(values, dtype=np.float64), 8).tolist()


def generate(outdir, seed=DEFAULTS["seed"], n_samples=DEFAULTS["n_samples"]):
    os.makedirs(outdir, exist_ok=True)
    dim = DEFAULTS["dim"]
    target = DEFAULTS["extrap_target_len"]

    config = {
        "scheme": "all",
        "base_freq": DEFAULTS["base_freq"],
        "train_ctx_len": DEFAULTS["train_ctx_len"],
        "extrap_target_len": target,
        "dim": dim,
        "scale_factor": DEFAULTS["scale_factor"],
        "precision": DEFAULTS["precision"],
        "max_workers": DEFAULTS["max_workers"],
        "tolerance": DEFAULTS["tolerance"],
        "seed": seed,
    }

    lines = []
    for index in range(n_samples):
        rng = np.random.default_rng(seed + index)
        sample = {
            "sample_id": "s%04d" % index,
            "query_pos": int(rng.integers(0, target)),
            "key_positions": sorted(
                int(p) for p in rng.integers(0, target, size=DEFAULTS["n_keys"])),
            "features": {
                "query": _round(rng.standard_normal(dim)),
                "keys": [_round(rng.standard_normal(dim))
                         for _ in range(DEFAULTS["n_keys"])],
            },
        }
        lines.append(json.dumps(sample, sort_keys=True))

    injections = {
        "version": 1,
        "entries": [
            {"inj_id": "INJ-001", "sample_id": "s0002", "stage": "encode",
             "fault": "nonfinite_feature",
             "detail": {"component": "query", "dim_index": 5, "value": "nan"}},
            {"inj_id": "INJ-002", "sample_id": "s0005", "stage": "score",
             "fault": "pos_out_of_range",
             "detail": {"key_index": 0, "position": target + 100}},
            {"inj_id": "INJ-003", "sample_id": "s0008", "stage": "encode",
             "fault": "base_zero", "detail": {}},
            {"inj_id": "INJ-004", "sample_id": "s0011", "stage": "score",
             "fault": "scale_invalid", "detail": {"scale_factor": -2.0}},
        ],
    }

    atomic_write_json(os.path.join(outdir, "config.json"), config)
    atomic_write_text(os.path.join(outdir, "samples.jsonl"), "\n".join(lines) + "\n")
    atomic_write_json(os.path.join(outdir, "injections.json"), injections)
    return config
