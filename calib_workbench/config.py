"""Configuration loading and validation.

The config is a single JSON document; ``config_version`` is a monotone
integer bumped by the operator on every change.  All cache keys embed
the version plus the relevant config subset, so a version bump with
unchanged semantics still reuses content-addressed artifacts.
"""

from __future__ import annotations

import json

from .errors import (
    E_BATCH_LIMIT,
    E_CONFIG_INVALID,
    E_INVALID_BINS,
    E_INVALID_SPLIT_RATIO,
    E_MEM_TABLE_LIMIT,
    E_PARALLEL_LIMIT,
    E_UNKNOWN_CALIBRATOR,
    WorkbenchError,
)

KNOWN_CALIBRATORS = ("platt", "isotonic", "histogram")

MIN_BINS, MAX_BINS = 2, 1000

DEFAULTS = {
    "config_version": 1,
    "seed": 20240501,
    "data": {
        "path": "data/samples.jsonl",
        "n_samples": 600,
        "numeric_features": ["f0", "f1", "f2", "f3"],
        "categorical_features": {
            "segment": ["alpha", "beta", "gamma"],
            "channel": ["app", "web"],
        },
        "groups": ["g1", "g2", "g3"],
    },
    "scoring": {"temperature": 2.5, "offset": 0.4},
    "split": {"ref_ratio": 0.6},
    "calibrators": {
        "platt": {"max_iter": 50, "tol": 1e-10, "l2": 1e-6},
        "isotonic": {},
        "histogram": {"n_bins": 10},
    },
    "metrics": {"ece_bins": 10},
    "scheduler": {"workers": 4, "batch_size": 2},
    "budget": {"max_total_iters": 100000, "max_resident_bytes": 1 << 20},
    "limits": {
        "max_batch_size": 8,
        "max_prob_table_bytes": 4 << 20,
        "max_parallel_combos": 64,
    },
    "output_dir": "out",
    "state_dir": ".workbench",
}


def _merge(base, override):
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    cfg = _merge(DEFAULTS, raw)
    validate_config(cfg)
    return cfg


def validate_config(cfg: dict) -> None:
    stage = "config"
    if not isinstance(cfg.get("seed"), int):
        raise WorkbenchError(E_CONFIG_INVALID, stage, "seed must be an integer", field="seed")

    ratio = cfg["split"]["ref_ratio"]
    if not isinstance(ratio, (int, float)) or isinstance(ratio, bool) or not 0.0 < ratio < 1.0:
        raise WorkbenchError(
            E_INVALID_SPLIT_RATIO, stage,
            f"split.ref_ratio must be in the open interval (0, 1), got {ratio!r}",
            field="split.ref_ratio",
        )

    for name in cfg["calibrators"]:
        if name not in KNOWN_CALIBRATORS:
            raise WorkbenchError(
                E_UNKNOWN_CALIBRATOR, stage,
                f"unknown calibrator {name!r}; known: {sorted(KNOWN_CALIBRATORS)}",
                field=f"calibrators.{name}",
            )
    if not cfg["calibrators"]:
        raise WorkbenchError(E_CONFIG_INVALID, stage, "at least one calibrator is required",
                             field="calibrators")

    hist = cfg["calibrators"].get("histogram")
    if hist is not None:
        n_bins = hist.get("n_bins")
        if not isinstance(n_bins, int) or isinstance(n_bins, bool) or not MIN_BINS <= n_bins <= MAX_BINS:
            raise WorkbenchError(
                E_INVALID_BINS, stage,
                f"histogram n_bins must be an integer in [{MIN_BINS}, {MAX_BINS}], got {n_bins!r}",
                field="calibrators.histogram.n_bins",
            )

    ece_bins = cfg["metrics"]["ece_bins"]
    if not isinstance(ece_bins, int) or isinstance(ece_bins, bool) or not MIN_BINS <= ece_bins <= MAX_BINS:
        raise WorkbenchError(
            E_INVALID_BINS, stage,
            f"metrics.ece_bins must be an integer in [{MIN_BINS}, {MAX_BINS}], got {ece_bins!r}",
            field="metrics.ece_bins",
        )

    limits = cfg["limits"]
    batch_size = cfg["scheduler"]["batch_size"]
    if not isinstance(batch_size, int) or batch_size < 1:
        raise WorkbenchError(E_CONFIG_INVALID, stage, "scheduler.batch_size must be a positive integer",
                             field="scheduler.batch_size")
    if batch_size > limits["max_batch_size"]:
        raise WorkbenchError(
            E_BATCH_LIMIT, stage,
            f"scheduler.batch_size={batch_size} exceeds limits.max_batch_size={limits['max_batch_size']}",
            field="scheduler.batch_size",
            details={"batch_size": batch_size, "max_batch_size": limits["max_batch_size"]},
        )

    workers = cfg["scheduler"]["workers"]
    if not isinstance(workers, int) or workers < 1:
        raise WorkbenchError(E_CONFIG_INVALID, stage, "scheduler.workers must be a positive integer",
                             field="scheduler.workers")

    n_combos = len(cfg["calibrators"]) * len(cfg["data"]["groups"])
    if n_combos > limits["max_parallel_combos"]:
        raise WorkbenchError(
            E_PARALLEL_LIMIT, stage,
            f"{n_combos} calibrator x group combos exceed limits.max_parallel_combos="
            f"{limits['max_parallel_combos']}",
            field="calibrators",
            details={"combos": n_combos, "max_parallel_combos": limits["max_parallel_combos"]},
        )

    budget = cfg["budget"]
    for key in ("max_total_iters", "max_resident_bytes"):
        if not isinstance(budget.get(key), int) or budget[key] < 0:
            raise WorkbenchError(E_CONFIG_INVALID, stage, f"budget.{key} must be a non-negative integer",
                                 field=f"budget.{key}")

    if not isinstance(limits.get("max_prob_table_bytes"), int) or limits["max_prob_table_bytes"] < 0:
        raise WorkbenchError(E_CONFIG_INVALID, stage,
                             "limits.max_prob_table_bytes must be a non-negative integer",
                             field="limits.max_prob_table_bytes")
    _ = E_MEM_TABLE_LIMIT  # enforced in the pipeline once eval sizes are known
