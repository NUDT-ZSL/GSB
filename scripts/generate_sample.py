#!/usr/bin/env python3
"""固定种子样本生成入口。

用法：python scripts/generate_sample.py [输出目录，默认 sample_data]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sr_pipeline.samplegen import generate_samples  # noqa: E402


def main() -> int:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "sample_data"
    manifest = generate_samples(out_dir)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(f"\n样本已生成: {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
