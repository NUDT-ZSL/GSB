"""Deterministic local sample manifest generation.

Produces a JSONL manifest where each line holds ``sample_id``, numeric
features, categorical features, ``label`` and ``group``, plus a
deterministic raw classifier score.  The raw score is deliberately
miscalibrated (temperature + offset on the true logit) so that
calibration has measurable effect.  The only randomness source is
``numpy.random.default_rng(seed)``; no system time or environment state
is consulted.
"""

from __future__ import annotations

import json

import numpy as np

from .util import floats_to_lists, stable_seed


def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-z))


def generate_manifest(cfg: dict) -> list[dict]:
    seed = cfg["seed"]
    data = cfg["data"]
    n = int(data["n_samples"])
    num_feats = list(data["numeric_features"])
    cat_feats = {k: list(v) for k, v in data["categorical_features"].items()}
    groups = list(data["groups"])
    temperature = float(cfg["scoring"]["temperature"])
    offset = float(cfg["scoring"]["offset"])

    # Fixed model coefficients, drawn once from sub-seeds so they do not
    # depend on n_samples.
    rng_w = np.random.default_rng(stable_seed(seed, "weights"))
    w_num = rng_w.normal(0.0, 1.0, size=len(num_feats))
    cat_effects = {
        name: dict(zip(cats, rng_w.normal(0.0, 0.6, size=len(cats)).tolist()))
        for name, cats in cat_feats.items()
    }
    group_effects = dict(zip(groups, rng_w.normal(0.0, 0.4, size=len(groups)).tolist()))

    rng = np.random.default_rng(stable_seed(seed, "manifest"))
    x_num = rng.normal(0.0, 1.0, size=(n, len(num_feats)))
    cat_draws = {
        name: rng.choice(len(cats), size=n).tolist() for name, cats in cat_feats.items()
    }
    group_draws = rng.choice(len(groups), size=n).tolist()

    z = x_num @ w_num
    for i in range(n):
        for name, cats in cat_feats.items():
            z[i] += cat_effects[name][cats[cat_draws[name][i]]]
        z[i] += group_effects[groups[group_draws[i]]]
    p_true = _sigmoid(z)
    labels = (rng.random(n) < p_true).astype(int)
    raw_scores = np.clip(_sigmoid(temperature * z + offset), 1e-6, 1.0 - 1e-6)

    rows = []
    for i in range(n):
        row = {
            "sample_id": f"S{i:06d}",
            "numeric": {name: float(x_num[i, j]) for j, name in enumerate(num_feats)},
            "categorical": {name: cats[cat_draws[name][i]] for name, cats in cat_feats.items()},
            "label": int(labels[i]),
            "group": groups[group_draws[i]],
            "raw_score": float(raw_scores[i]),
        }
        rows.append(row)
    return rows


def write_manifest(rows: list[dict], path) -> None:
    """Write the manifest as canonical JSONL (sorted keys, one row/line)."""
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(floats_to_lists(row), sort_keys=True, separators=(",", ":")))
            fh.write("\n")


def load_manifest(path) -> list[dict]:
    rows = []
    with open(path, "r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            row["_line_no"] = line_no
            rows.append(row)
    return rows
