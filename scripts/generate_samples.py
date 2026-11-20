#!/usr/bin/env python3
"""Generate deterministic scene manifests and multi-modal observations."""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scene_pipeline.manifest import SceneManifest, write_manifest
from scene_pipeline.observations import generate_scene_bundle
from scene_pipeline.synth import build_camera_specs


def build_manifests(scene_count: int, camera_count: int) -> list[SceneManifest]:
    manifests: list[SceneManifest] = []
    for index in range(scene_count):
        point_seed = 1000 + index * 17
        cameras, _ = build_camera_specs(point_seed, camera_count)
        manifests.append(
            SceneManifest(
                scene_id=f"scene_{index:03d}",
                scene_revision=1,
                camera_poses=cameras,
                point_cloud_seed=point_seed,
                texture_seed=5000 + index * 31,
                geometry_mode="shell" if index % 2 else "solid",
                target_face_budget=64 if index % 2 else 96,
                priority=(index * 7) % 10,
            )
        )
    return manifests


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default="data/manifest.jsonl")
    parser.add_argument("--observation-dir", default="data/observations")
    parser.add_argument("--scene-count", type=int, default=6)
    parser.add_argument("--camera-count", type=int, default=3)
    args = parser.parse_args()

    manifests = build_manifests(args.scene_count, args.camera_count)
    os.makedirs(os.path.dirname(args.manifest), exist_ok=True)
    write_manifest(args.manifest, manifests)

    ground_truth = {}
    for manifest in manifests:
        ground_truth[manifest.scene_id] = generate_scene_bundle(
            manifest, args.observation_dir
        )
    with open(os.path.join(os.path.dirname(args.manifest),
                           "ground_truth_index.json"), "w",
              encoding="utf-8") as handle:
        json.dump(ground_truth, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(
        f"generated {len(manifests)} scenes, {args.camera_count} cameras each "
        f"-> {args.manifest}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
