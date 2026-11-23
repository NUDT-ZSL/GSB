"""Deterministic group-wise split and feature preprocessing (requirement 1).

Splitting rules (fixed, no hidden randomness):
  * groups are processed in sorted order;
  * within a group, rows are sorted by sample_id ascending, then permuted
    with ``default_rng(stable_seed(seed, "split", group))``;
  * ``n_ref = floor(n * ref_ratio)``; the first ``n_ref`` permuted
    positions form the reference set, the rest the evaluation set; both
    are then re-sorted by sample_id ascending for canonical output.

Preprocessing rules (fixed):
  * numeric features are standardized with mean/std computed on the
    group's reference split only (std == 0 falls back to 1.0);
  * categorical features are encoded by the fixed rule
    ``index / (k - 1)`` where the category order is the lexicographically
    sorted declared category list from the config (k = category count,
    k == 1 encodes to 0.0).

The same manifest + config + seed yields byte-identical output.
"""

from __future__ import annotations

import numpy as np

from .errors import E_INVALID_SPLIT_RATIO, WorkbenchError
from .util import floats_to_lists, stable_seed

STAGE = "split"


def split_and_preprocess(rows: list[dict], cfg: dict) -> dict:
    seed = cfg["seed"]
    ratio = cfg["split"]["ref_ratio"]
    num_feats = list(cfg["data"]["numeric_features"])
    cat_spec = {k: sorted(v) for k, v in cfg["data"]["categorical_features"].items()}
    cat_feats = sorted(cat_spec.keys())

    by_group = {}
    for row in rows:
        by_group.setdefault(row["group"], []).append(row)

    groups_out = {}
    for group in sorted(by_group):
        grows = sorted(by_group[group], key=lambda r: r["sample_id"])
        n = len(grows)
        rng = np.random.default_rng(stable_seed(seed, "split", group))
        perm = rng.permutation(n)
        n_ref = int(n * ratio)
        n_eval = n - n_ref
        if n_ref < 1 or n_eval < 1:
            raise WorkbenchError(
                E_INVALID_SPLIT_RATIO, STAGE,
                f"split.ref_ratio={ratio} yields a degenerate split for group "
                f"{group!r} (n={n}, n_ref={n_ref}, n_eval={n_eval})",
                field="split.ref_ratio",
                details={"group": group, "n": n, "n_ref": n_ref, "n_eval": n_eval},
            )
        ref_rows = sorted((grows[i] for i in perm[:n_ref]), key=lambda r: r["sample_id"])
        eval_rows = sorted((grows[i] for i in perm[n_ref:]), key=lambda r: r["sample_id"])

        def matrix(subset):
            x_num = np.array([[r["numeric"][f] for f in num_feats] for r in subset], dtype=float)
            x_cat = np.array(
                [[_encode_cat(cat_spec[f], r["categorical"][f]) for f in cat_feats] for r in subset],
                dtype=float,
            )
            y = np.array([r["label"] for r in subset], dtype=float)
            scores = np.array([r["raw_score"] for r in subset], dtype=float)
            ids = [r["sample_id"] for r in subset]
            return ids, x_num, x_cat, y, scores

        ref_ids, ref_num, ref_cat, ref_y, ref_scores = matrix(ref_rows)
        ev_ids, ev_num, ev_cat, ev_y, ev_scores = matrix(eval_rows)

        mean = ref_num.mean(axis=0)
        std = ref_num.std(axis=0)
        std = np.where(std == 0.0, 1.0, std)
        ref_num = (ref_num - mean) / std
        ev_num = (ev_num - mean) / std

        groups_out[group] = {
            "ref": _pack(ref_ids, ref_num, ref_cat, ref_y, ref_scores),
            "eval": _pack(ev_ids, ev_num, ev_cat, ev_y, ev_scores),
            "scaler": {"mean": mean.tolist(), "std": std.tolist()},
        }

    return floats_to_lists({
        "seed": seed,
        "ref_ratio": ratio,
        "numeric_features": num_feats,
        "categorical_encoding": {
            f: {"categories": cat_spec[f], "rule": "index/(k-1), k==1 -> 0.0"}
            for f in cat_feats
        },
        "groups": groups_out,
    })


def _encode_cat(categories_sorted, value):
    k = len(categories_sorted)
    if k <= 1:
        return 0.0
    return categories_sorted.index(value) / (k - 1)


def _pack(ids, x_num, x_cat, y, scores):
    return {
        "sample_ids": list(ids),
        "x_num": x_num.tolist(),
        "x_cat": x_cat.tolist(),
        "y": [int(v) for v in y],
        "scores": scores.tolist(),
    }
