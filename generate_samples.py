#!/usr/bin/env python3
"""仓库根入口：等价于 ``python scripts/generate_samples.py``。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from multiscreen.generator import DEFAULT_SEED, generate_sample  # noqa: E402


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="生成多屏状态机固定种子样本")
    parser.add_argument("--out", default=str(ROOT / "task"))
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()
    result = generate_sample(args.out, args.seed)
    print(f"generated {result['events']} events for {result['receivers']} receivers in {result['out_dir']}")
