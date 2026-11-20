#!/usr/bin/env python3
"""Build a revised manifest to exercise incremental failure propagation."""
from __future__ import annotations

import argparse
import dataclasses
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scene_pipeline.manifest import read_manifest, write_manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="data/manifest.jsonl")
    parser.add_argument("--output", default="data/manifest_rev2.jsonl")
    parser.add_argument("--scene-id", default="scene_000")
    parser.add_argument("--geometry-mode", default="shell")
    args = parser.parse_args()

    revised = []
    for manifest in read_manifest(args.input):
        if manifest.scene_id == args.scene_id:
            manifest = dataclasses.replace(
                manifest,
                scene_revision=manifest.scene_revision + 1,
                geometry_mode=args.geometry_mode,
            )
        revised.append(manifest)
    write_manifest(args.output, revised)
    print(f"wrote revised manifest -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
