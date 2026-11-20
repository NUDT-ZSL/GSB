#!/usr/bin/env python3
"""Run the deterministic reconstruction pipeline over a manifest JSONL."""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scene_pipeline.config import PipelineConfig
from scene_pipeline.incremental import (
    STAGE_ORDER,
    manifest_fingerprint,
    plan_incremental,
)
from scene_pipeline.manifest import read_manifest
from scene_pipeline.scheduler import run_schedule
from scene_pipeline.snapshot import SnapshotStore
from scene_pipeline.synth import POINT_COUNT


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/pipeline.sample.json")
    parser.add_argument("--manifest", default="data/manifest.jsonl")
    parser.add_argument("--observation-dir", default="data/observations")
    parser.add_argument("--snapshot-root", default="data/snapshots")
    parser.add_argument("--faults")
    parser.add_argument("--report", default="data/schedule_report.json")
    parser.add_argument("--state", default="data/incremental_state.json")
    args = parser.parse_args()

    config = PipelineConfig.load(args.config)
    manifests = read_manifest(args.manifest)
    store = SnapshotStore(args.snapshot_root)

    previous_state: dict = {}
    if os.path.exists(args.state):
        with open(args.state, "r", encoding="utf-8") as handle:
            previous_state = json.load(handle)

    previous_by_id = {
        scene_id: _manifest_from_state(item["manifest"])
        for scene_id, item in previous_state.items()
    }
    plans = {}
    new_state: dict = {}
    for manifest in manifests:
        previous = previous_by_id.get(manifest.scene_id)
        previous_fp = (
            previous_state[manifest.scene_id]["fingerprints"]
            if manifest.scene_id in previous_state else None
        )
        plan = plan_incremental(
            previous=previous,
            current=manifest,
            store=store,
            previous_fingerprints=previous_fp,
        )
        plans[manifest.scene_id] = plan
        new_state[manifest.scene_id] = {
            "manifest": manifest.to_dict(),
            "fingerprints": manifest_fingerprint(manifest),
        }

    # Each occupied voxel contributes eight vertices; the number of occupied
    # voxels is bounded by both the point count and the grid cell count.
    occupied_voxels = min(
        POINT_COUNT, config.geometry.grid_resolution ** 3
    )
    vertex_estimate = occupied_voxels * 8
    report = run_schedule(
        manifests=manifests,
        config=config,
        observation_dir=args.observation_dir,
        snapshot_root=args.snapshot_root,
        point_count=POINT_COUNT,
        vertex_estimate=vertex_estimate,
        fault_path=args.faults,
        incremental_plans=plans,
    )

    payload = report.to_dict()
    payload["incremental"] = {
        scene_id: plan.to_dict() for scene_id, plan in plans.items()
    }
    payload["stage_order"] = list(STAGE_ORDER)
    os.makedirs(os.path.dirname(args.report), exist_ok=True)
    with open(args.report, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    with open(args.state, "w", encoding="utf-8") as handle:
        json.dump(new_state, handle, indent=2, sort_keys=True)
        handle.write("\n")

    failures = len(report.failures)
    print(
        f"processed {len(manifests)} scenes in {len(report.batches)} batches; "
        f"failures={failures}; report={args.report}"
    )
    return 0


def _manifest_from_state(data: dict):
    from scene_pipeline.manifest import SceneManifest
    return SceneManifest.from_dict(data)


if __name__ == "__main__":
    raise SystemExit(main())
