#!/usr/bin/env python3
"""端到端演示：双精度融合、误差账本、预算裁决、缓存失效与快照版本。

用法：python scripts/run_demo.py [样本目录，默认 sample_data] [缓存库，默认 artifacts/demo_cache.sqlite]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sr_pipeline.pipeline import execute_pipeline  # noqa: E402


def main() -> int:
    sample_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "sample_data"
    cache_db = (
        Path(sys.argv[2])
        if len(sys.argv) > 2
        else ROOT / "artifacts" / "demo_cache.sqlite"
    )
    cache_db.parent.mkdir(parents=True, exist_ok=True)

    result = execute_pipeline(sample_dir, cache_db)
    summary = {
        "config_checksum": result.config_checksum,
        "scheduler": result.report,
        "invalidation": result.invalidation,
        "snapshots": result.snapshots,
        "self_check": result.self_check,
        "sample_fixed_candidate": result.fixed_results[0]["candidates"][0],
        "sample_diagnosis_threshold": result.diagnoses[0]["threshold"],
        "sample_dominant_source": result.diagnoses[0]["dominant_sources"][0],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
