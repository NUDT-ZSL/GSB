"""Incremental vs full-rebuild consistency matrix.

For every combination of RLE compression level (0/1) and source format
(v1/v2), applies an edit sequence (metadata change, single mask replacement,
format version switch, IoU threshold change) incrementally and compares each
step's panoptic pixels, fused instance ordering and conflict records against a
fresh full rebuild started from an empty workspace with the same final inputs.
"""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any

from panseg.models import PipelineConfig
from panseg.pipeline import PanopticPipeline
from panseg.snapshot import _deserialize_fusion
from panseg.v1_package import V1RawInstance, parse_v1_package, write_v1_package
from panseg.v2_package import parse_v2_package, write_v2_package, V2RawInstance


def _load_config(base_dir: Path, compression: int, threshold: float) -> PipelineConfig:
    raw = json.loads((base_dir / "pipeline_config.json").read_text(encoding="utf-8"))
    raw["rle_compression"] = compression
    raw["iou_threshold"] = threshold
    raw["workers"] = 4
    raw["fault_injection"] = {}
    return PipelineConfig(**raw)


def _fingerprint_outputs(work_dir: Path) -> dict[str, Any]:
    store_dir = work_dir / "snapshots"
    pointer = json.loads((store_dir / "current.json").read_text(encoding="utf-8"))
    snapshot = store_dir / f"snapshot-{pointer['version']:06d}"
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    scenes = {}
    for scene_id, file_name in sorted(manifest["fusion_files"].items()):
        fusion = _deserialize_fusion(json.loads((snapshot / "fusion" / file_name).read_text(encoding="utf-8")))
        scenes[scene_id] = {
            "label_map": fusion.label_map.tolist(),
            "fused": [item.to_dict() for item in fusion.fused_instances],
            "conflicts": [item.to_dict() for item in fusion.conflicts],
            "dropped": list(fusion.dropped_instances),
        }
    return {
        "config_hash": manifest["config_hash"],
        "instance_versions": manifest["instance_versions"],
        "scenes": scenes,
    }


def _run(base_dir: Path, work_dir: Path, config: PipelineConfig) -> dict[str, Any]:
    pipeline = PanopticPipeline(base_dir, config, work_dir)
    pipeline.run()
    return _fingerprint_outputs(work_dir)


def _edit_metadata(base_dir: Path) -> None:
    path = base_dir / "metadata.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    for entry in doc["instances"]:
        if entry["instance_id"] == "scene-01-inst-01":
            entry["confidence"] = 0.1234
            entry["is_crowd"] = not entry["is_crowd"]
    path.write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")


def _replace_single_mask(base_dir: Path, source: str) -> None:
    target = "scene-02-inst-02"
    if source == "v1":
        path = base_dir / "scene-02.v1.bin"
        width, height, instances = parse_v1_package(path)
        replaced = []
        for item in instances:
            if item.instance_id == target:
                runs = tuple(list(item.runs[:1]) + [(width * height - 12, 12)])
                replaced.append(V1RawInstance(item.instance_id, item.semantic_class, runs, item.pixel_count))
            else:
                replaced.append(item)
        write_v1_package(path, width, height, replaced, compression=1)
    else:
        path = base_dir / "scene-02.v2.json"
        width, height, instances, palette = parse_v2_package(path)
        replaced = []
        for item in instances:
            if item.instance_id == target:
                runs = tuple(list(item.runs[:1]) + [(width * height - 8, 8)])
                replaced.append(
                    V2RawInstance(
                        item.instance_id, item.semantic_class, runs, item.pixel_count,
                        item.confidence, item.is_crowd, item.palette_name, item.palette_rgb,
                    )
                )
            else:
                replaced.append(item)
        write_v2_package(path, width, height, replaced, palette)


def _switch_format_version(base_dir: Path, source: str) -> None:
    # Re-encode the active source package with the opposite compression/JSON
    # formatting while keeping mask bytes identical; the cache key must stay
    # stable because content is unchanged.
    if source == "v1":
        path = base_dir / "scene-00.v1.bin"
        width, height, instances = parse_v1_package(path)
        write_v1_package(path, width, height, instances, compression=0)
    else:
        path = base_dir / "scene-01.v2.json"
        width, height, instances, palette = parse_v2_package(path)
        write_v2_package(path, width, height, instances, palette)


def run_matrix(sample_source: Path, root: Path) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    results: dict[str, Any] = {}
    for compression in (0, 1):
        for source in ("v1", "v2"):
            key = f"c{compression}-{source}"
            incremental_dir = root / key / "incremental"
            full_dir = root / key / "full"
            base_incremental = root / key / "inputs_inc"
            base_full = root / key / "inputs_full"
            shutil.copytree(sample_source, base_incremental)
            shutil.copytree(sample_source, base_full)
            # remove corrupt helper directory for matrix inputs
            shutil.rmtree(base_incremental / "corrupt", ignore_errors=True)
            shutil.rmtree(base_full / "corrupt", ignore_errors=True)

            threshold = 0.35
            step_results = []
            for step, edit in enumerate(
                (
                    lambda: None,
                    lambda: _edit_metadata(base_incremental),
                    lambda: _replace_single_mask(base_incremental, source),
                    lambda: _switch_format_version(base_incremental, source),
                    lambda: None,
                )
            ):
                if step == 4:
                    threshold = 0.22
                config_inc = _load_config(base_incremental, compression, threshold)
                edit()
                inc_fp = _run(base_incremental, incremental_dir, config_inc)

                # Mirror the same edits onto the clean full input set.
                if step == 1:
                    _edit_metadata(base_full)
                elif step == 2:
                    _replace_single_mask(base_full, source)
                elif step == 3:
                    _switch_format_version(base_full, source)
                config_full = _load_config(base_full, compression, threshold)
                full_fp = _run_fresh_full(base_full, root / key / f"full_step{step}", config_full)

                equal = inc_fp == full_fp
                step_results.append({"step": step, "equal": equal, "threshold": threshold})
                if not equal:
                    raise AssertionError(f"incremental/full mismatch at {key} step {step}")
            results[key] = step_results
    return results


def _run_fresh_full(base_dir: Path, work_dir: Path, config: PipelineConfig) -> dict[str, Any]:
    shutil.rmtree(work_dir, ignore_errors=True)
    return _run(base_dir, work_dir, config)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", default="sample_data_v2")
    parser.add_argument("--out", default="validation/consistency")
    args = parser.parse_args()
    matrix = run_matrix(Path(args.sample), Path(args.out))
    summary = {key: all(step["equal"] for step in steps) for key, steps in matrix.items()}
    print(json.dumps(summary, indent=2, sort_keys=True))
