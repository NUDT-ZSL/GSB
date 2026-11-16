"""Generate the fully deterministic local panoptic sample set.

Everything is derived from a fixed seed; no network access is used.  The
generator also creates a boundary scene containing guaranteed float32/float64
decision flips and corrupt packages driven by an independent fault-injection
profile (source code is never modified to produce failures).
"""

from __future__ import annotations

import argparse
import base64
import json
import random
from pathlib import Path
from typing import Any

import numpy as np

from panseg.png_io import write_instance_png
from panseg.rle import bitmap_to_runs
from panseg.v1_package import V1RawInstance, write_v1_package
from panseg.v2_package import V2RawInstance, write_v2_package

SEED = 20261117
WIDTH = 64
HEIGHT = 48
CLASSES = ["sky", "road", "car", "person", "vegetation"]
CLASS_INDEX = {name: index + 1 for index, name in enumerate(CLASSES)}


def _runs_from_rectangle(x0: int, y0: int, x1: int, y1: int, width: int, height: int):
    bitmap = np.zeros((height, width), dtype=np.uint8)
    bitmap[y0:y1, x0:x1] = 1
    return bitmap_to_runs(bitmap)


def _runs_from_bitmap(bitmap: np.ndarray):
    return bitmap_to_runs(bitmap)


def generate_core_scenes() -> dict[str, dict[str, Any]]:
    rng = random.Random(SEED)
    scenes: dict[str, dict[str, Any]] = {}
    for scene_index in range(3):
        scene_id = f"scene-{scene_index:02d}"
        instances = []
        for local_index in range(6):
            instance_id = f"{scene_id}-inst-{local_index:02d}"
            semantic_class = CLASSES[(scene_index * 2 + local_index) % len(CLASSES)]
            box_w = rng.randint(10, 26)
            box_h = rng.randint(8, 20)
            x0 = rng.randint(0, WIDTH - box_w - 1)
            y0 = rng.randint(0, HEIGHT - box_h - 1)
            runs, pixel_count = _runs_from_rectangle(x0, y0, x0 + box_w, y0 + box_h, WIDTH, HEIGHT)
            confidence = round(rng.uniform(0.55, 0.98), 4)
            is_crowd = local_index == 5
            instances.append(
                {
                    "instance_id": instance_id,
                    "semantic_class": semantic_class,
                    "runs": runs,
                    "pixel_count": pixel_count,
                    "confidence": confidence,
                    "is_crowd": is_crowd,
                    "rect": [x0, y0, x0 + box_w, y0 + box_h],
                }
            )
        scenes[scene_id] = {"width": WIDTH, "height": HEIGHT, "instances": instances}
    return scenes


def _find_iou_flip(target: float = 0.7) -> tuple[int, int, int, int, int]:
    """Search a deterministic integer (intersection, union) pair whose f32 and
    f64 threshold decisions disagree."""

    threshold_f32 = float(np.float32(target))
    for union in range(2, 17_000_001):
        intersection = round(target * union)
        if not 0 < intersection < union:
            continue
        f64 = float(intersection) / float(union)
        f32 = float(np.float32(np.float32(intersection) / np.float32(union)))
        if (f64 >= target) != (f32 >= threshold_f32):
            return intersection, union, intersection, union - intersection, union
    raise RuntimeError("no deterministic IoU flip pair found")


def generate_boundary_scene() -> dict[str, Any]:
    intersection, union, _inter, _only_a, _total_union = _find_iou_flip()
    exclusive_total = union - intersection
    only_a = exclusive_total // 2
    only_b = exclusive_total - only_a
    needed = union
    side = int(np.ceil(np.sqrt(needed)))
    width = side
    height = side
    while width * height < needed:
        height += 1
    total = width * height

    bitmap_a = np.zeros(total, dtype=np.uint8)
    bitmap_b = np.zeros(total, dtype=np.uint8)
    # [0, intersection) shared; [intersection, intersection+only_a) A only;
    # [intersection+only_a, union) B only.  |A|, |B| <= union and |A union B|
    # stays exactly equal to the searched union constant.
    bitmap_a[: intersection + only_a] = 1
    bitmap_b[:intersection] = 1
    bitmap_b[intersection + only_a : union] = 1
    bitmap_a = bitmap_a.reshape(height, width)
    bitmap_b = bitmap_b.reshape(height, width)

    runs_a, count_a = _runs_from_bitmap(bitmap_a)
    runs_b, count_b = _runs_from_bitmap(bitmap_b)
    actual_inter = int(np.count_nonzero(bitmap_a & bitmap_b))
    actual_union = int(np.count_nonzero(bitmap_a | bitmap_b))
    assert actual_inter == intersection and actual_union == union

    # 10x10 policy scene: 1/100 coverage flips under float32 at 0.01.
    policy_width = policy_height = 10
    policy_bitmap = np.zeros(policy_width * policy_height, dtype=np.uint8)
    policy_bitmap[0] = 1
    policy_runs, policy_pixels = _runs_from_bitmap(policy_bitmap.reshape(policy_height, policy_width))

    return {
        "iou": {
            "scene_id": "scene-boundary-iou",
            "width": width,
            "height": height,
            "instances": [
                {
                    "instance_id": "scene-boundary-iou-inst-00",
                    "semantic_class": "car",
                    "runs": runs_a,
                    "pixel_count": count_a,
                    "confidence": 0.9,
                    "is_crowd": False,
                    "source": "v1",
                },
                {
                    "instance_id": "scene-boundary-iou-inst-01",
                    "semantic_class": "car",
                    "runs": runs_b,
                    "pixel_count": count_b,
                    "confidence": 0.88,
                    "is_crowd": False,
                    "source": "v2",
                },
            ],
            "intersection": intersection,
            "union": union,
        },
        "policy": {
            "scene_id": "scene-boundary-policy",
            "width": policy_width,
            "height": policy_height,
            "instances": [
                {
                    "instance_id": "scene-boundary-policy-inst-00",
                    "semantic_class": "person",
                    "runs": policy_runs,
                    "pixel_count": policy_pixels,
                    "confidence": 0.8,
                    "is_crowd": False,
                    "source": "v2",
                }
            ],
        },
    }


def _palette_for(instances: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    palette: dict[str, dict[str, Any]] = {}
    for index, instance in enumerate(instances):
        name = f"color_{instance['semantic_class']}"
        if name not in palette:
            palette[name] = {
                "rgb": [
                    (47 * (index + 3)) % 256,
                    (91 * (index + 5)) % 256,
                    (173 * (index + 7)) % 256,
                ]
            }
    return palette


def _write_clean_packages(out_dir: Path, scene_id: str, width: int, height: int, instances: list[dict[str, Any]]) -> None:
    def _source_of(item: dict[str, Any]) -> str:
        return item.get("source", "both")

    v1_items = [item for item in instances if _source_of(item) in ("v1", "both")]
    v2_items = [item for item in instances if _source_of(item) in ("v2", "both")]
    v1_instances = [
        V1RawInstance(item["instance_id"], item["semantic_class"], item["runs"], item["pixel_count"])
        for item in v1_items
    ]
    write_v1_package(out_dir / f"{scene_id}.v1.bin", width, height, v1_instances, compression=1)
    palette = _palette_for(instances)
    v2_instances = [
        V2RawInstance(
            instance_id=item["instance_id"],
            semantic_class=item["semantic_class"],
            runs=item["runs"],
            pixel_count=item["pixel_count"],
            confidence=item["confidence"],
            is_crowd=item["is_crowd"],
            palette_name=f"color_{item['semantic_class']}",
            palette_rgb=tuple(palette[f"color_{item['semantic_class']}"]["rgb"]),
        )
        for item in v2_items
    ]
    write_v2_package(out_dir / f"{scene_id}.v2.json", width, height, v2_instances, palette)


def _inject_v1_rle_overrun(path: Path) -> None:
    """Rebuild the package with one run that exceeds the pixel total."""

    from panseg.v1_package import parse_v1_package
    from panseg.v1_package import V1RawInstance as _V1

    width, height, instances = parse_v1_package(path)
    total = width * height
    target = instances[0]
    broken_runs = list(target.runs)
    # Add a 1-run that starts inside the image but runs past its last pixel.
    broken_runs.append((total - 2, 8))
    broken = _V1(target.instance_id, target.semantic_class, tuple(broken_runs), target.pixel_count + 8)
    replaced = [broken] + instances[1:]
    write_v1_package(path, width, height, replaced, compression=1)


def _inject_v2_length_mismatch(path: Path) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    target = document["instances"][0]
    decoded = bytearray(base64.b64decode(target["mask_b64"]))
    decoded.pop()
    target["mask_b64"] = base64.b64encode(bytes(decoded)).decode("ascii")
    path.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")


def _inject_v2_palette_missing(path: Path) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    target = document["instances"][-1]
    target["palette_name"] = "color_missing"
    path.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="sample_data")
    args = parser.parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    core = generate_core_scenes()
    boundary = generate_boundary_scene()
    all_scenes = dict(core)
    all_scenes["scene-boundary-iou"] = boundary["iou"]
    all_scenes["scene-boundary-policy"] = boundary["policy"]

    metadata_entries: list[dict[str, Any]] = []
    for scene_id, scene in all_scenes.items():
        width, height = scene["width"], scene["height"]
        instances = scene["instances"]
        _write_clean_packages(out_dir, scene_id, width, height, instances)
        png_dir = out_dir / "png" / scene_id
        for index, instance in enumerate(instances):
            write_instance_png(
                png_dir / f"{instance['instance_id']}.png",
                instance["runs"],
                height,
                width,
                instance_index=index + 1,
                class_index=CLASS_INDEX.get(instance["semantic_class"], 9),
            )
        for item in instances:
            metadata_entries.append(
                {
                    "instance_id": item["instance_id"],
                    "semantic_class": item["semantic_class"],
                    "confidence": item["confidence"],
                    "is_crowd": item["is_crowd"],
                }
            )

    (out_dir / "metadata.json").write_text(
        json.dumps({"format_version": 1, "instances": metadata_entries}, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    config = {
        "workers": 4,
        "rle_compression": 1,
        "retention_policy": "drop_below_coverage",
        "min_coverage": 0.01,
        "iou_threshold": 0.35,
        "default_confidence": 0.5,
        "default_crowd": False,
        "snapshot_commit_interval": 2,
        "boundary_iou_threshold": 0.7,
    }
    (out_dir / "pipeline_config.json").write_text(json.dumps(config, indent=2, sort_keys=True), encoding="utf-8")

    fault_injection = {
        "parse_v1": {"instance_ids": ["scene-00-inst-00"], "code": "E_RLE_OUT_OF_RANGE", "corrupt_file": "scene-00.v1.bin", "mode": "rle_overrun"},
        "parse_v2_length": {"instance_ids": ["scene-01-inst-00"], "code": "E_MASK_LENGTH_MISMATCH", "corrupt_file": "scene-01.v2.json", "mode": "length_mismatch"},
        "parse_v2_palette": {"instance_ids": ["scene-02-inst-05"], "code": "E_PALETTE_MISSING", "corrupt_file": "scene-02.v2.json", "mode": "palette_missing"},
        "snapshot_hard_crash": {"after_instance": "scene-01-inst-03", "code": "E_HARD_CRASH_SIMULATED"},
    }
    (out_dir / "fault_injection.json").write_text(
        json.dumps(fault_injection, indent=2, sort_keys=True), encoding="utf-8"
    )

    # Materialize the corrupt validation packages in a dedicated directory so
    # clean inputs remain available for incremental/full consistency runs.
    corrupt_dir = out_dir / "corrupt"
    corrupt_dir.mkdir(exist_ok=True)
    import shutil

    shutil.copy(out_dir / "scene-00.v1.bin", corrupt_dir / "scene-00.v1.bin")
    _inject_v1_rle_overrun(corrupt_dir / "scene-00.v1.bin")
    shutil.copy(out_dir / "scene-01.v2.json", corrupt_dir / "scene-01.v2.json")
    _inject_v2_length_mismatch(corrupt_dir / "scene-01.v2.json")
    shutil.copy(out_dir / "scene-02.v2.json", corrupt_dir / "scene-02.v2.json")
    _inject_v2_palette_missing(corrupt_dir / "scene-02.v2.json")

    summary = {
        "seed": SEED,
        "scenes": {scene_id: {"width": s["width"], "height": s["height"], "instances": len(s["instances"])} for scene_id, s in all_scenes.items()},
        "boundary": {
            "iou_intersection": boundary["iou"]["intersection"],
            "iou_union": boundary["iou"]["union"],
            "iou_threshold": 0.7,
            "policy_pixels": 1,
            "policy_total": 100,
            "policy_min_coverage": 0.01,
        },
    }
    (out_dir / "sample_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
