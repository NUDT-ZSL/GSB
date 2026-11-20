from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from .config import expand_per_image, validate_config
from .errors import PipelineError
from .utils import round_float


COCO17 = [
    ("nose", 0.500, 0.100),
    ("left_eye", 0.468, 0.078),
    ("right_eye", 0.532, 0.078),
    ("left_ear", 0.438, 0.094),
    ("right_ear", 0.562, 0.094),
    ("left_shoulder", 0.402, 0.250),
    ("right_shoulder", 0.598, 0.250),
    ("left_elbow", 0.336, 0.400),
    ("right_elbow", 0.664, 0.400),
    ("left_wrist", 0.286, 0.548),
    ("right_wrist", 0.714, 0.548),
    ("left_hip", 0.438, 0.552),
    ("right_hip", 0.562, 0.552),
    ("left_knee", 0.414, 0.724),
    ("right_knee", 0.586, 0.724),
    ("left_ankle", 0.392, 0.904),
    ("right_ankle", 0.608, 0.904),
]

COCO17_EDGES = [
    (0, 1),
    (0, 2),
    (1, 3),
    (2, 4),
    (0, 5),
    (0, 6),
    (5, 7),
    (7, 9),
    (6, 8),
    (8, 10),
    (5, 11),
    (6, 12),
    (11, 13),
    (13, 15),
    (12, 14),
    (14, 16),
    (11, 12),
]


def _template_for(num_keypoints: int) -> list[tuple[str, float, float]]:
    if num_keypoints == len(COCO17):
        return list(COCO17)
    if num_keypoints == 1:
        return [("kp_00", 0.5, 0.5)]
    return [
        (
            f"kp_{index:02d}",
            0.5 + 0.22 * math.sin(index * 1.61803398875),
            0.08 + 0.84 * index / (num_keypoints - 1),
        )
        for index in range(num_keypoints)
    ]


def _edges_for(num_keypoints: int) -> list[list[int]]:
    if num_keypoints == len(COCO17):
        return [[left, right] for left, right in COCO17_EDGES]
    return [[index - 1, index] for index in range(1, num_keypoints)]


def _metadata_for(num_keypoints: int) -> list[dict[str, Any]]:
    template = _template_for(num_keypoints)
    metadata = []
    for index, (name, _, _) in enumerate(template):
        sigma = 0.040 + 0.025 * ((index * 7) % 11) / 10.0
        metadata.append(
            {
                "index": index,
                "name": name,
                "oks_sigma": round_float(sigma),
            }
        )
    return metadata


def _array_sha256(array: np.ndarray) -> str:
    digest = hashlib.sha256()
    digest.update(str(array.dtype).encode("ascii"))
    digest.update(str(tuple(array.shape)).encode("ascii"))
    digest.update(np.ascontiguousarray(array).tobytes())
    return digest.hexdigest()


def synthesize_heatmaps(sample: dict[str, Any], heatmap_sigma: float) -> np.ndarray:
    height, width = sample["canvas_shape"]
    num_keypoints = sample["num_keypoints"]
    skeletons = sample["skeletons"]
    channels = len(skeletons) * num_keypoints
    truth = np.zeros((channels, height, width), dtype=np.float32)
    target = np.zeros((channels, height, width), dtype=np.float32)
    rng = np.random.default_rng(sample["heatmap_seed"])
    ys, xs = np.mgrid[0:height, 0:width]
    sigma2 = float(heatmap_sigma) ** 2

    for skeleton_index, skeleton in enumerate(skeletons):
        keypoints = {point["index"]: point for point in skeleton["keypoints"]}
        for keypoint_index in range(num_keypoints):
            point = keypoints[keypoint_index]
            channel = skeleton_index * num_keypoints + keypoint_index
            amplitude = 0.72 + 0.28 * float(point["confidence"])
            if not point["visible"]:
                amplitude *= 0.82
            x_truth = float(point["x"])
            y_truth = float(point["y"])
            truth_gaussian = np.exp(
                -((xs - x_truth) ** 2 + (ys - y_truth) ** 2) / (2.0 * sigma2)
            )
            truth[channel] = (amplitude * truth_gaussian).astype(np.float32)

            dx = float(rng.uniform(-0.30, 0.30))
            dy = float(rng.uniform(-0.30, 0.30))
            x_target = min(width - 1.0, max(0.0, x_truth + dx))
            y_target = min(height - 1.0, max(0.0, y_truth + dy))
            target_gaussian = np.exp(
                -((xs - x_target) ** 2 + (ys - y_target) ** 2) / (2.0 * sigma2)
            )
            target[channel] = (amplitude * target_gaussian).astype(np.float32)

    return np.stack([truth, target], axis=0)


def generate_sample(config: dict[str, Any], image_index: int) -> dict[str, Any]:
    seed = int(config["seed"])
    prefix = str(config["image_prefix"])
    canvas_shape = [int(value) for value in config["canvas_shape"]]
    height, width = canvas_shape
    num_keypoints = int(expand_per_image(config, "num_keypoints")[image_index])
    num_skeletons = int(expand_per_image(config, "skeletons_per_image")[image_index])
    num_candidates = int(expand_per_image(config, "candidates_per_image")[image_index])
    occlusion_ratio = float(expand_per_image(config, "occlusion_ratio")[image_index])
    sample_seed = seed + image_index * 1_000_003
    heatmap_seed = seed + image_index * 130_363 + 17
    rng = np.random.default_rng(sample_seed)
    image_id = f"{prefix}-{image_index:04d}"
    metadata = _metadata_for(num_keypoints)
    template = _template_for(num_keypoints)
    edges = _edges_for(num_keypoints)
    skeleton_sources = list(config["skeleton_sources"])
    candidate_sources = list(config["candidate_sources"])

    skeletons: list[dict[str, Any]] = []
    skeleton_truth: list[dict[int, dict[str, Any]]] = []
    margin = 5.0
    usable_width = max(1.0, width - 2.0 * margin)
    usable_height = max(1.0, height - 2.0 * margin)
    pose_scale = min(usable_width / max(1.0, num_skeletons * 0.52), usable_height * 0.90)

    for skeleton_index in range(num_skeletons):
        center_fraction = (skeleton_index + 1.0) / (num_skeletons + 1.0)
        center_x = margin + center_fraction * usable_width
        center_y = margin + 0.52 * usable_height
        hidden_count = min(num_keypoints - 1, int(math.floor(num_keypoints * occlusion_ratio)))
        hidden = set(
            int(value)
            for value in rng.choice(num_keypoints, size=hidden_count, replace=False)
        )
        keypoints = []
        by_index: dict[int, dict[str, Any]] = {}
        for keypoint_index, (_, tx, ty) in enumerate(template):
            jitter_x = float(rng.normal(0.0, 0.32))
            jitter_y = float(rng.normal(0.0, 0.32))
            x = center_x + (tx - 0.5) * pose_scale + jitter_x
            y = center_y + (ty - 0.5) * pose_scale + jitter_y
            x = min(width - 1.0, max(0.0, x))
            y = min(height - 1.0, max(0.0, y))
            confidence = 0.65 + 0.35 * float(rng.random())
            point = {
                "index": keypoint_index,
                "x": round_float(x),
                "y": round_float(y),
                "visible": keypoint_index not in hidden,
                "confidence": round_float(confidence),
            }
            keypoints.append(point)
            by_index[keypoint_index] = point
        skeleton = {
            "skeleton_id": skeleton_index,
            "source": skeleton_sources[skeleton_index % len(skeleton_sources)],
            "keypoints": keypoints,
            "edges": edges,
        }
        skeletons.append(skeleton)
        skeleton_truth.append(by_index)

    candidates = []
    for candidate_index in range(num_candidates):
        skeleton_index = candidate_index % num_skeletons
        source = candidate_sources[candidate_index % len(candidate_sources)]
        candidate_keypoints = []
        score_sum = 0.0
        for keypoint_index in range(num_keypoints):
            truth_point = skeleton_truth[skeleton_index][keypoint_index]
            x = truth_point["x"] + float(rng.normal(0.0, 0.85))
            y = truth_point["y"] + float(rng.normal(0.0, 0.85))
            x = min(width - 1.0, max(0.0, x))
            y = min(height - 1.0, max(0.0, y))
            point_score = 0.45 + 0.55 * math.exp(
                -((x - truth_point["x"]) ** 2 + (y - truth_point["y"]) ** 2) / 8.0
            )
            point_score = min(1.0, max(0.0, point_score))
            score_sum += point_score
            candidate_keypoints.append(
                {
                    "index": keypoint_index,
                    "x": round_float(x),
                    "y": round_float(y),
                    "score": round_float(point_score),
                }
            )
        candidates.append(
            {
                "candidate_id": f"{image_id}-c{candidate_index:04d}",
                "source": source,
                "score": round_float(score_sum / num_keypoints),
                "keypoints": candidate_keypoints,
            }
        )

    sample = {
        "image_id": image_id,
        "canvas_shape": canvas_shape,
        "num_keypoints": num_keypoints,
        "keypoint_meta": metadata,
        "heatmap_seed": heatmap_seed,
        "occlusion_ratio": round_float(occlusion_ratio),
        "skeletons": skeletons,
        "candidates": candidates,
        "num_channels": len(skeletons) * num_keypoints,
    }
    heatmaps = synthesize_heatmaps(sample, float(config["heatmap_sigma"]))
    sample["heatmap_file"] = f"heatmaps/{image_id}.npy"
    sample["heatmap_sha256"] = _array_sha256(heatmaps)
    sample["truth_heatmap_sha256"] = _array_sha256(heatmaps[0])
    sample["target_heatmap_sha256"] = _array_sha256(heatmaps[1])
    return sample


def generate_dataset(config: dict[str, Any], output_dir: str | Path) -> Path:
    validate_config(config)
    output = Path(output_dir)
    heatmap_dir = output / "heatmaps"
    heatmap_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output / "manifest.jsonl"
    lines = []
    for image_index in range(int(config["num_images"])):
        sample = generate_sample(config, image_index)
        heatmaps = synthesize_heatmaps(sample, float(config["heatmap_sigma"]))
        heatmap_path = output / sample["heatmap_file"]
        np.save(heatmap_path, heatmaps, allow_pickle=False)
        lines.append(
            json.dumps(
                sample,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
    manifest_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return manifest_path


def load_manifest(path: str | Path) -> tuple[list[dict[str, Any]], Path]:
    manifest_path = Path(path)
    try:
        lines = manifest_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise PipelineError(
            "E_CONFIG_INVALID",
            "manifest_path",
            f"manifest not found: {manifest_path}",
        ) from exc
    samples = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PipelineError(
                "E_CONFIG_INVALID",
                f"manifest.line[{line_number}]",
                f"invalid JSONL line: {exc}",
            ) from exc
        if not isinstance(value, dict):
            raise PipelineError(
                "E_CONFIG_INVALID",
                f"manifest.line[{line_number}]",
                "manifest line must be a JSON object",
            )
        samples.append(value)
    return samples, manifest_path.parent


def verify_saved_heatmaps(
    sample: dict[str, Any], manifest_dir: str | Path, heatmap_sigma: float
) -> np.ndarray:
    rebuilt = synthesize_heatmaps(sample, heatmap_sigma)
    rebuilt_hash = _array_sha256(rebuilt)
    expected_hash = sample.get("heatmap_sha256")
    if rebuilt_hash != expected_hash:
        raise PipelineError(
            "E_INCONSISTENT",
            "heatmap_sha256",
            "heatmap rebuilt from heatmap_seed does not match the manifest hash",
            image_id=sample.get("image_id"),
            category="HEATMAP_REBUILD_HASH",
            details={"expected": expected_hash, "actual": rebuilt_hash},
        )
    heatmap_path = Path(manifest_dir) / sample["heatmap_file"]
    if heatmap_path.exists():
        stored = np.load(heatmap_path, allow_pickle=False)
        stored_hash = _array_sha256(stored)
        if stored_hash != rebuilt_hash:
            raise PipelineError(
                "E_INCONSISTENT",
                "heatmap_file",
                "stored heatmap does not match seed-based reconstruction",
                image_id=sample.get("image_id"),
                category="HEATMAP_FILE_HASH",
                details={
                    "heatmap_file": sample["heatmap_file"],
                    "expected": rebuilt_hash,
                    "actual": stored_hash,
                },
            )
    return rebuilt
