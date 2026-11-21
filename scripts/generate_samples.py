#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from poscascade.samples import DEFAULT_SEED, generate_samples


def main() -> int:
    parser = argparse.ArgumentParser(description="生成固定种子的中文分词标注样本")
    parser.add_argument("--root", default=str(ROOT / "data"))
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()
    manifest = generate_samples(args.root, args.seed)
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
