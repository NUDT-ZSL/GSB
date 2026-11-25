#!/usr/bin/env python3
"""验收指标收集：双精度误差、预算裁决、缓存复用与快照版本，输出 JSON。"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sr_pipeline.errors import BusinessError  # noqa: E402
from sr_pipeline.pipeline import execute_pipeline, load_bundle  # noqa: E402
from sr_pipeline.scheduler import run_scheduler  # noqa: E402


def main() -> int:
    cache_db = ROOT / "artifacts" / "val_cache.sqlite"
    for pattern in ("val_cache.sqlite*",):
        for path in ROOT.glob(f"artifacts/{pattern}"):
            path.unlink()

    first = execute_pipeline(ROOT / "sample_data", cache_db)
    second = execute_pipeline(ROOT / "sample_data", cache_db)

    levels = [
        level
        for diagnosis in first.diagnoses
        for level in diagnosis["levels"]
    ]
    metrics = {
        "queries": len(first.diagnoses),
        "max_abs_error": max(level["abs_error"] for level in levels),
        "max_rel_error": max(level["rel_error"] for level in levels),
        "threshold_stable_queries": sum(
            1 for d in first.diagnoses if d["threshold"]["stable"]
        ),
        "near_threshold_match_queries": sum(
            1 for d in first.diagnoses if d["threshold"]["near_threshold_match"]
        ),
        "ordering_preserved_all": all(
            d["ordering_preserved"] and d["dedup_preserved"]
            for d in first.diagnoses
        ),
        "first_run": {
            "recompute": len(first.invalidation["recompute_queries"]),
            "reuse": len(first.invalidation["reuse_queries"]),
            "snapshots": first.snapshots,
        },
        "second_run": {
            "changed_modules": second.invalidation["changed_modules"],
            "recompute": len(second.invalidation["recompute_queries"]),
            "reuse": len(second.invalidation["reuse_queries"]),
        },
        "scheduler": first.report,
        "budget_codes": [],
    }

    docs, queries, term_weights, config = load_bundle(ROOT / "sample_data")
    for label, patched in (
        (
            "candidate",
            replace(
                config,
                scheduler=replace(config.scheduler, candidate_budget=5),
            ),
        ),
        (
            "memory",
            replace(
                config,
                scheduler=replace(config.scheduler, memory_budget_bytes=1),
            ),
        ),
    ):
        try:
            run_scheduler(docs, queries, term_weights, patched)
        except BusinessError as error:
            metrics["budget_codes"].append(
                {
                    "case": label,
                    "code": error.code,
                    "field": error.field_path,
                }
            )

    out_path = ROOT / "artifacts" / "validation_metrics.json"
    out_path.write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
