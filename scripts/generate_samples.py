"""使用 numpy 固定随机种子生成确定性样本清单，演示 numpy 仅用于样本与整数统计。

运行:
    python scripts\\generate_samples.py
输出:
    samples\\generated_manifest.json
"""

import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import numpy as np

from gpu_diag.estimator import estimate_job
from gpu_diag.manifest import parse_manifest
from gpu_diag.models import Job, MemoryCurve, ScaleParams

SEED = 20261117


def build_manifest():
    rng = np.random.default_rng(SEED)
    jobs = []
    groups = ("team-alpha", "team-beta", "team-gamma")
    constraints = ("same_group", "distinct_group", "any")
    for index in range(6):
        jobs.append({
            "job_id": "gen-job-{:02d}".format(index + 1),
            "pool_name": "pool-a" if index % 2 == 0 else "pool-b",
            "card_count": int(rng.integers(1, 4)),
            "quota_group": groups[int(rng.integers(0, len(groups)))],
            "priority": int(rng.integers(1, 11)),
            "topology_constraint": constraints[int(rng.integers(0, len(constraints)))],
            "memory_curve": {
                "bytes_per_param": int(rng.choice([4, 8, 16])),
                "base_mib": int(rng.integers(128, 1025)),
                "safety_pct": int(rng.integers(0, 21)),
            },
        })
    return {
        "scale_bounds": {
            "batch_size": [1, 256],
            "seq_len": [128, 8192],
            "model_dim": [256, 8192],
        },
        "scale_params": {"batch_size": 16, "seq_len": 512, "model_dim": 768},
        "pools": [
            {
                "name": "pool-a",
                "card_count": 8,
                "card_memory_mib": 81920,
                "topology_groups": [
                    {"label": "rack-a1", "cards": [0, 1, 2, 3]},
                    {"label": "rack-a2", "cards": [4, 5, 6, 7]},
                ],
                "drained_cards": [],
            },
            {
                "name": "pool-b",
                "card_count": 4,
                "card_memory_mib": 40960,
                "topology_groups": [
                    {"label": "rack-b1", "cards": [0, 1]},
                    {"label": "rack-b2", "cards": [2, 3]},
                ],
                "drained_cards": [],
            },
        ],
        "quotas": [
            {"group_id": "team-alpha", "max_cards": 6, "max_memory_mib": 400000},
            {"group_id": "team-beta", "max_cards": 4, "max_memory_mib": 200000},
            {"group_id": "team-gamma", "max_cards": 2, "max_memory_mib": 100000},
        ],
        "jobs": jobs,
    }


def main():
    raw = build_manifest()
    manifest = parse_manifest(raw)
    estimates = []
    for job in manifest.jobs:
        result = estimate_job(job, manifest.scale_params, 1, manifest.scale_bounds)
        estimates.append(result.per_card_mib)
    stats = {
        "seed": SEED,
        "job_count": len(estimates),
        "per_card_mib_mean": int(np.mean(np.asarray(estimates, dtype=np.int64))),
        "per_card_mib_max": int(np.max(np.asarray(estimates, dtype=np.int64))),
        "per_card_mib_min": int(np.min(np.asarray(estimates, dtype=np.int64))),
    }
    out_path = os.path.join("samples", "generated_manifest.json")
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(raw, handle, ensure_ascii=False, indent=2, sort_keys=True)
    print(json.dumps({"written": out_path, "numpy_integer_stats": stats},
                     ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
