"""固定种子生成本地确定性样本与参数文件。"""

import argparse
import json
import os

import numpy as np

SEED = 20261117
RECALL_NAMES = ["recall_text", "recall_vector", "recall_hot"]
DENSE_NAMES = ["dense_ctr", "dense_cvr", "dense_embed_x", "dense_embed_y",
               "dense_embed_z", "dense_price"]
SPARSE_NAMES = ["sparse_click_7d", "sparse_cart_7d"]
FEATURE_NAMES = RECALL_NAMES + DENSE_NAMES + SPARSE_NAMES


def _write(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")


def write_params(out_dir):
    weights = [0.22, 0.18, 0.10, 0.16, 0.12, 0.07, 0.05, 0.04, 0.03, 0.02, 0.01]
    config = {
        "feature_names": FEATURE_NAMES,
        "weights": weights,
        "normalization": ["minmax", "minmax", "zscore", "zscore", "zscore",
                          "minmax", "minmax", "minmax", "minmax", "minmax", "minmax"],
        "frac_bits": 3,
        "levels": 9,
        "rounding": "half_even",
        "temperature": 1.25,
        "score_clip": [-4.0, 4.0],
        "rerank_window": 5,
        "diversity_penalty": 0.25,
        "similarity_threshold": 0.75,
        "max_workers": 1,
        "snapshot_interval": 1,
        "error_tolerance": 1,
    }
    groups = {
        "similarity_threshold": 0.75,
        "groups": [
            {"name": "recall_group", "features": RECALL_NAMES, "scale_conflict": False},
            {"name": "dense_quality", "features": DENSE_NAMES[:3], "scale_conflict": True},
            {"name": "dense_embedding", "features": DENSE_NAMES[3:], "scale_conflict": True},
            {"name": "sparse_group", "features": SPARSE_NAMES, "scale_conflict": False},
        ],
    }
    _write(os.path.join(out_dir, "rank_config.json"), config)
    _write(os.path.join(out_dir, "feature_groups.json"), groups)
    _write(os.path.join(out_dir, "weights.json"), {"weights": weights, "feature_names": FEATURE_NAMES})
    return config, groups


def generate_candidates(path, n_requests=12, base_items=14, seed=SEED,
                        include_invalid=True, include_threshold_pair=True):
    rng = np.random.default_rng(seed)
    lines = []
    for r in range(n_requests):
        rid = f"req-{r:03d}"
        n_items = base_items + int(rng.integers(0, 5))
        base_quality = rng.normal(0.0, 1.0)
        for k in range(n_items):
            item_id = f"item-{r:03d}-{k:03d}"
            scores = {
                "recall_text": float(rng.normal(base_quality, 1.0)),
                "recall_vector": float(rng.normal(base_quality * 0.8, 1.0)),
                "recall_hot": float(rng.normal(0.0, 1.0)),
            }
            dense = [
                float(np.clip(rng.normal(0.5, 0.2), 0.0, 1.0)),
                float(np.clip(rng.normal(0.3, 0.15), 0.0, 1.0)),
                float(rng.normal(0.0, 1.0)),
                float(rng.normal(0.0, 1.0)),
                float(rng.normal(0.0, 1.0)),
                float(rng.uniform(0.0, 100.0)),
            ]
            sparse = {
                "sparse_click_7d": int(rng.poisson(3)),
                "sparse_cart_7d": int(rng.poisson(1)),
            }
            lines.append(json.dumps({
                "request_id": rid, "item_id": item_id,
                "scores": scores, "dense": dense, "sparse": sparse,
            }, ensure_ascii=False, sort_keys=True))

        if include_threshold_pair and r in (2, 7):
            twin = json.loads(lines[-1])
            twin["item_id"] = f"item-{r:03d}-twin"
            lines.append(json.dumps(twin, ensure_ascii=False, sort_keys=True))

    if include_invalid:
        lines.append(json.dumps({
            "request_id": "req-bad-nan", "item_id": "item-nan",
            "scores": {"recall_text": float("nan"), "recall_vector": 1.0,
                       "recall_hot": 0.5},
            "dense": [0.1] * 6,
            "sparse": {"sparse_click_7d": 1, "sparse_cart_7d": 0},
        }, allow_nan=True, sort_keys=True))
        lines.append(json.dumps({
            "request_id": "req-bad-sparse", "item_id": "item-sp",
            "scores": {"recall_text": 1.0, "recall_vector": 1.0, "recall_hot": 0.5},
            "dense": [0.1] * 6,
            "sparse": {"sparse_click_7d": -2, "sparse_cart_7d": 0},
        }, sort_keys=True))
        first = json.loads(lines[0])
        dup = dict(first)
        lines.append(json.dumps(dup, sort_keys=True))
        lines.append("{not valid json")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return len(lines)


def write_injection(path):
    injection = {
        "items": [
            {"id": "inj-001", "request_id": "req-003", "stage": "normalize",
             "type": "non_finite_feature", "item_id": None},
            {"id": "inj-002", "request_id": "req-005", "stage": "fuse",
             "type": "weight_length_mismatch"},
            {"id": "inj-003", "request_id": "req-008", "stage": "normalize",
             "type": "frac_bits_out_of_range", "frac_bits": 99},
            {"id": "inj-004", "request_id": "req-002", "stage": "rerank",
             "type": "sim_equal_threshold", "threshold": 1.0},
        ]
    }
    _write(path, injection)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--requests", type=int, default=12)
    args = parser.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)
    write_params(args.out_dir)
    n = generate_candidates(os.path.join(args.out_dir, "candidates.jsonl"), args.requests)
    write_injection(os.path.join(args.out_dir, "injection.json"))
    print(f"generated {n} candidate lines under {args.out_dir}")


if __name__ == "__main__":
    main()
