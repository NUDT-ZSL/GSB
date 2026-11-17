#!/usr/bin/env python3
"""Run the normal demonstration and then the injected demonstration."""

from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from intent_engine.pipeline import run_pipeline
from intent_engine.utils import atomic_write_json


def main() -> int:
    baseline = run_pipeline(
        ROOT / "data" / "corpus.jsonl",
        ROOT / "data" / "resources",
        config_path=ROOT / "data" / "resources" / "config.json",
        state_dir=ROOT / "state" / "demo-baseline",
        fresh=True,
    )
    injected = run_pipeline(
        ROOT / "data" / "corpus.jsonl",
        ROOT / "data" / "resources",
        config_path=ROOT / "data" / "resources" / "config.json",
        injection_path=ROOT / "data" / "inject.json",
        state_dir=ROOT / "state" / "demo-injected",
        fresh=True,
    )
    atomic_write_json(ROOT / "reports" / "baseline.json", baseline.report)
    atomic_write_json(ROOT / "reports" / "injected.json", injected.report)
    print(
        json.dumps(
            {
                "baseline": {
                    "samples": baseline.report["sample_count"],
                    "success": baseline.report["success_count"],
                    "failure": baseline.report["failure_count"],
                    "snapshot_version": baseline.snapshot_version,
                },
                "injected": {
                    "samples": injected.report["sample_count"],
                    "success": injected.report["success_count"],
                    "failure": injected.report["failure_count"],
                    "injection_hits": len(injected.report["injection_hits"]),
                    "snapshot_version": injected.snapshot_version,
                },
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

