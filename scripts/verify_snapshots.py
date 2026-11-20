#!/usr/bin/env python3
"""Independent verifier: recompute conclusions and validate all snapshots."""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scene_pipeline.config import PipelineConfig
from scene_pipeline.errors import InconsistentError, SnapshotCorruptError
from scene_pipeline.manifest import read_manifest
from scene_pipeline.snapshot import SnapshotStore
from scene_pipeline.verifier import verify_store


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/pipeline.sample.json")
    parser.add_argument("--manifest", default="data/manifest.jsonl")
    parser.add_argument("--observation-dir", default="data/observations")
    parser.add_argument("--snapshot-root", default="data/snapshots")
    parser.add_argument("--report", default="data/verification_report.json")
    args = parser.parse_args()

    config = PipelineConfig.load(args.config)
    manifests = read_manifest(args.manifest)
    store = SnapshotStore(args.snapshot_root)
    try:
        result = verify_store(store, manifests, args.observation_dir, config)
    except (InconsistentError, SnapshotCorruptError) as exc:
        result = {
            "all_ok": False,
            "error_code": exc.code,
            "scene_id": exc.scene_id,
            "stage": exc.stage,
            "details": exc.details,
            "message": exc.message,
        }
        with open(args.report, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2, sort_keys=True)
            handle.write("\n")
        print(f"VERIFICATION FAILED: {exc.code} {exc.details or exc.message}")
        return 1

    with open(args.report, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(
        f"verification ok for {result['scene_count']} scenes; "
        f"report={args.report}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
