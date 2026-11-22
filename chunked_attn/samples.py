"""Deterministic local sample generation.

Q/K/V arrays are generated with ``numpy.random.default_rng(seed)`` (PCG64,
guaranteed stable across runs and platforms), so the same case_id + seed +
config always reproduces byte-identical inputs. No model files, GPUs or
external services are involved.
"""
from __future__ import annotations

import json
import os

import numpy as np

SAMPLE_CASES = [
    {"case_id": "case-001", "seq_len": 64, "head_dim": 32, "n_heads": 2,
     "q_block_len": 16, "kv_block_len": 16, "causal": True,
     "dtype": "float32", "seed": 20261123, "latency_budget_ms": 5000.0},
    {"case_id": "case-002", "seq_len": 128, "head_dim": 64, "n_heads": 4,
     "q_block_len": 32, "kv_block_len": 32, "causal": False,
     "dtype": "float64", "seed": 20261124, "latency_budget_ms": 5000.0},
    {"case_id": "case-003", "seq_len": 96, "head_dim": 48, "n_heads": 3,
     "q_block_len": 16, "kv_block_len": 48, "causal": True,
     "dtype": "float32", "seed": 20261125, "latency_budget_ms": 5000.0},
    {"case_id": "case-004", "seq_len": 256, "head_dim": 64, "n_heads": 4,
     "q_block_len": 64, "kv_block_len": 64, "causal": True,
     "dtype": "float32", "seed": 20261126, "latency_budget_ms": 5000.0},
]


def gen_arrays(record):
    """Deterministically generate (q, k, v) for one manifest record."""
    rng = np.random.default_rng(record["seed"])
    shape = (record["n_heads"], record["seq_len"], record["head_dim"])
    dtype = np.dtype(record["dtype"])
    q = rng.standard_normal(shape).astype(dtype)
    k = rng.standard_normal(shape).astype(dtype)
    v = rng.standard_normal(shape).astype(dtype)
    return q, k, v


def array_paths(data_dir, case_id):
    return {name: os.path.join(data_dir, "%s_%s.npy" % (case_id, name))
            for name in ("q", "k", "v")}


def generate_samples(out_dir):
    """Write cases.jsonl and the deterministic .npy arrays under out_dir."""
    os.makedirs(out_dir, exist_ok=True)
    data_dir = os.path.join(out_dir, "arrays")
    os.makedirs(data_dir, exist_ok=True)
    manifest_path = os.path.join(out_dir, "cases.jsonl")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        for rec in SAMPLE_CASES:
            q, k, v = gen_arrays(rec)
            paths = array_paths(data_dir, rec["case_id"])
            np.save(paths["q"], q)
            np.save(paths["k"], k)
            np.save(paths["v"], v)
            fh.write(json.dumps(rec, ensure_ascii=True) + "\n")
    return manifest_path


def load_manifest(manifest_path):
    records = []
    with open(manifest_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def load_arrays(data_dir, case_id):
    paths = array_paths(data_dir, case_id)
    return (np.load(paths["q"]), np.load(paths["k"]), np.load(paths["v"]))
