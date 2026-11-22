"""生成本地确定性样本: 图文条件清单 / 参考图 / 生成配置.

全部样本由固定种子本地生成, 不依赖外部下载. 重复执行产出
逐字节相同的样本文件.
"""

from __future__ import annotations

import json
import os
import sys

import numpy as np

SAMPLE_SEED = 20231123
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
OUT = os.path.join(ROOT, "samples")


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    sys.path.insert(0, os.path.abspath(ROOT))
    from genapp.textcond import VOCAB

    rng = np.random.default_rng(SAMPLE_SEED)

    # 1) 图文条件清单: 受控词表内确定性抽样拼装.
    descriptions = []
    for idx in range(4):
        k = 4 + idx
        toks = rng.choice(np.array(VOCAB), size=k, replace=True)
        descriptions.append({"id": f"desc-{idx}", "tokens": sorted(toks.tolist())})
    with open(os.path.join(OUT, "descriptions.json"), "w", encoding="utf-8") as fh:
        json.dump(
            {"seed": SAMPLE_SEED, "descriptions": descriptions},
            fh, ensure_ascii=False, indent=2, sort_keys=True,
        )

    # 2) 固定尺寸参考图 (96x96x3 uint8 原始字节).
    height, width, channels = 96, 96, 3
    ref = rng.integers(0, 256, size=(height, width, channels), dtype=np.uint8)
    ref.tofile(os.path.join(OUT, "reference.bin"))
    with open(os.path.join(OUT, "reference.json"), "w", encoding="utf-8") as fh:
        json.dump(
            {"seed": SAMPLE_SEED, "shape": [height, width, channels],
             "dtype": "uint8", "file": "reference.bin"},
            fh, ensure_ascii=False, indent=2, sort_keys=True,
        )

    # 3) 生成配置: 画布 / 步数 / 块网格候选 / 锚定强度 / 预算与资源上限.
    config = {
        "width": width,
        "height": height,
        "channels": channels,
        "steps": 8,
        "precision": "float32",
        "seed": SAMPLE_SEED,
        "cond_dim": 16,
        "dt": 0.15,
        "reaction": 0.05,
        "latency_budget_ms": 250.0,
        "cost_per_pixel_step_us": 0.40,
        "coupling_threshold": 4096,
        "grid_candidates": ["1x1", "2x2", "4x4"],
        "verify_min_tier": 1,
        "anchor_strength": 0.30,
        "limits": {
            "max_total_pixels": 1048576,
            "max_block_bytes": 4194304,
            "max_concurrent_tasks": 4,
        },
    }
    with open(os.path.join(OUT, "config.json"), "w", encoding="utf-8") as fh:
        json.dump(config, fh, ensure_ascii=False, indent=2, sort_keys=True)

    print(f"samples written to {os.path.abspath(OUT)}")


if __name__ == "__main__":
    main()
