"""Deterministic local sample generation for WSL/Linux validation."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np

from .config_writer import write_v1_config, write_v2_config
from .hashing import array_digest, content_key
from .models import Category, InstanceMapping, UnifiedConfig


DEFAULT_SEED = 20261127
HEIGHT = 16
WIDTH = 16
TIME_ORIGIN_MS = 1_700_000_000_000
EFFECTIVE_SECONDS = 42.0


def _templates(height: int, width: int) -> tuple[np.ndarray, tuple[int, ...], tuple[int, ...], np.ndarray, np.ndarray]:
    yy, xx = np.mgrid[0:height, 0:width]
    rectangle = (yy >= 2) & (yy < 8) & (xx >= 2) & (xx < 8)
    disk = ((yy - 9.0) ** 2 + (xx - 9.0) ** 2) <= 12.0
    triangle = (
        (yy >= 11)
        & (yy <= 14)
        & (xx >= 2 + (yy - 11))
        & (xx <= 6 - (yy - 11))
    )
    masks = np.stack([rectangle, disk, triangle]).astype(np.uint8)
    instance_ids = (101, 201, 301)
    category_ids = (1, 2, 3)

    gt_instance = np.zeros((height, width), dtype=np.uint16)
    gt_category = np.zeros((height, width), dtype=np.uint8)
    for mask, instance_id, category_id in zip(masks, instance_ids, category_ids):
        occupied = mask.astype(bool) & (gt_instance == 0)
        gt_instance[occupied] = instance_id
        gt_category[occupied] = category_id
    return masks, instance_ids, category_ids, gt_instance, gt_category


def _reference_score(template_union: np.ndarray) -> np.ndarray:
    binary = template_union.astype(np.float64)
    padded = np.pad(binary, 1, mode="constant", constant_values=0.0)
    score = np.zeros_like(binary, dtype=np.float64)
    for dy in range(3):
        for dx in range(3):
            score += padded[dy : dy + binary.shape[0], dx : dx + binary.shape[1]]
    return score / 9.0


def _write_ppm(path: Path, image: np.ndarray) -> None:
    height, width, channels = image.shape
    header = f"P6\n{width} {height}\n255\n".encode("ascii")
    path.write_bytes(header + np.ascontiguousarray(image, dtype=np.uint8).tobytes())


def build_sample_bundle(seed: int = DEFAULT_SEED) -> dict[str, Any]:
    rng = np.random.default_rng(seed)
    templates, instance_ids, category_ids, gt_instance, gt_category = _templates(HEIGHT, WIDTH)
    image = rng.integers(0, 256, size=(HEIGHT, WIDTH, 3), dtype=np.uint8)
    reference_score = _reference_score(templates.max(axis=0))

    v1_instance = gt_instance.copy()
    v1_instance[2, 2] = 0
    v1_instance[2, 3] = 0
    v1_instance[3, 2] = 0
    v1_instance[1, 4] = 101
    v1_instance[4, 1] = 101
    v1_instance[8, 8] = 101
    v1_instance[14, 4] = 0

    v2_instance = gt_instance.copy()
    v2_instance[7, 7] = 201 if v2_instance[7, 7] == 101 else v2_instance[7, 7]
    v2_instance[10, 12] = 0
    v2_instance[13, 13] = 301
    category_by_instance = {0: 0, 101: 1, 201: 2, 301: 3}
    v2_category = np.zeros_like(gt_category)
    for instance_id, category_id in category_by_instance.items():
        v2_category[v2_instance == instance_id] = category_id

    v2_confidence = np.zeros((HEIGHT, WIDTH), dtype=np.float64)
    foreground = v2_instance > 0
    v2_confidence[foreground] = 0.82 + 0.15 * rng.random(int(foreground.sum()))
    v2_confidence[~foreground] = 0.02 + 0.10 * rng.random(int((~foreground).sum()))
    # Fixed boundary values make threshold diagnostics reproducible.
    for y, x, value in [
        (2, 4, 0.84),
        (5, 5, 0.91),
        (9, 9, 0.96),
        (10, 10, 0.88),
        (12, 3, 0.86),
        (13, 4, 0.93),
    ]:
        if v2_instance[y, x] > 0:
            v2_confidence[y, x] = value

    v1_score = reference_score.copy()
    v1_score[v1_instance > 0] += rng.normal(0.0, 0.035, size=int((v1_instance > 0).sum()))
    v1_score[v1_instance == 0] += rng.normal(0.0, 0.055, size=int((v1_instance == 0).sum()))
    v1_score = np.clip(v1_score, 0.0, 1.0).astype(np.float64)

    v2_score = np.clip(v2_confidence + rng.normal(0.0, 0.012, size=v2_confidence.shape), 0.0, 1.0)
    v2_score = v2_score.astype(np.float64)

    categories = (
        Category(0, "background"),
        Category(1, "rectangle_panel"),
        Category(2, "circular_aperture"),
        Category(3, "triangle_marker"),
    )
    mappings = tuple(
        InstanceMapping(
            instance_id=instance_id,
            category_id=category_id,
            confidence_threshold=1.0 if instance_id == 0 else 0.75,
            effective_at_ms=TIME_ORIGIN_MS + int(EFFECTIVE_SECONDS * 1000),
            confidence_scope="implicit_one" if instance_id == 0 else "explicit",
        )
        for instance_id, category_id in [(0, 0), *zip(instance_ids, category_ids)]
    )
    v2_config = UnifiedConfig(
        source_version=2,
        revision=1,
        categories=categories,
        mappings=mappings,
        default_confidence_threshold=0.75,
        effective_at_ms=TIME_ORIGIN_MS + int(EFFECTIVE_SECONDS * 1000),
        relative_effective_seconds=EFFECTIVE_SECONDS,
        time_origin_ms=TIME_ORIGIN_MS,
        downgrade_policy="flatten",
    )
    v2_config.checksum = content_key(v2_config.canonical_payload())

    v1_mappings = tuple(
        InstanceMapping(
            instance_id=mapping.instance_id,
            category_id=mapping.category_id,
            confidence_threshold=1.0,
            effective_at_ms=mapping.effective_at_ms,
            confidence_scope="implicit_one",
        )
        for mapping in mappings
    )
    v1_config = UnifiedConfig(
        source_version=1,
        revision=1,
        categories=categories,
        mappings=v1_mappings,
        default_confidence_threshold=1.0,
        effective_at_ms=TIME_ORIGIN_MS + int(EFFECTIVE_SECONDS * 1000),
        relative_effective_seconds=EFFECTIVE_SECONDS,
        time_origin_ms=TIME_ORIGIN_MS,
        downgrade_policy="flatten",
    )
    v1_config.checksum = content_key(v1_config.canonical_payload())

    return {
        "seed": seed,
        "height": HEIGHT,
        "width": WIDTH,
        "image": image,
        "templates": templates,
        "template_instance_ids": np.asarray(instance_ids, dtype=np.uint16),
        "template_category_ids": np.asarray(category_ids, dtype=np.uint8),
        "gt_instance": gt_instance,
        "gt_category": gt_category,
        "v1_instance": v1_instance,
        "v2_category": v2_category,
        "v2_instance": v2_instance,
        "v2_confidence": v2_confidence,
        "reference_score": reference_score,
        "v1_score": v1_score,
        "v2_score": v2_score,
        "v1_config": v1_config,
        "v2_config": v2_config,
    }


def generate_sample_directory(root: str | Path, seed: int = DEFAULT_SEED, *, force: bool = False) -> Path:
    root = Path(root)
    if root.exists() and any(root.iterdir()) and not force:
        return root
    root.mkdir(parents=True, exist_ok=True)
    bundle = build_sample_bundle(seed)

    _write_ppm(root / "image.ppm", bundle["image"])
    np.save(root / "image_rgb.npy", bundle["image"])
    np.savez_compressed(
        root / "truth_templates.npz",
        templates=bundle["templates"],
        instance_ids=bundle["template_instance_ids"],
        category_ids=bundle["template_category_ids"],
        gt_instance=bundle["gt_instance"],
        gt_category=bundle["gt_category"],
        reference_score=bundle["reference_score"],
    )
    np.savez_compressed(
        root / "solver_masks.npz",
        v1_instance=bundle["v1_instance"],
        v2_category=bundle["v2_category"],
        v2_instance=bundle["v2_instance"],
        v2_confidence=bundle["v2_confidence"],
    )
    np.savez_compressed(
        root / "solver_float64.npz",
        v1_score=bundle["v1_score"],
        v2_score=bundle["v2_score"],
        reference_score=bundle["reference_score"],
    )

    write_v1_config(root / "config_v1.json", bundle["v1_config"], include_mappings=False)
    write_v2_config(root / "config_v2", bundle["v2_config"])

    files = [
        "image.ppm",
        "image_rgb.npy",
        "truth_templates.npz",
        "solver_masks.npz",
        "solver_float64.npz",
        "config_v1.json",
        "config_v2/shard_000.json",
        "config_v2/shard_001.json",
        "config_v2/metadata.sqlite3",
    ]
    manifest = {
        "seed": seed,
        "height": HEIGHT,
        "width": WIDTH,
        "color_channels": 3,
        "time_origin_ms": TIME_ORIGIN_MS,
        "solver_sources": [
            {
                "id": "truth_template_solver",
                "outputs": ["templates", "gt_category", "gt_instance", "reference_score"],
                "priority": 0,
            },
            {
                "id": "v1_instance_solver",
                "outputs": ["config_v1.json", "v1_instance", "v1_score"],
                "priority": 2,
            },
            {
                "id": "v2_category_instance_confidence_solver",
                "outputs": ["config_v2", "v2_category", "v2_instance", "v2_confidence", "v2_score"],
                "priority": 1,
            },
        ],
        "files": files,
        "array_checksums": {
            name: array_digest(bundle[name])
            for name in [
                "image",
                "templates",
                "gt_instance",
                "gt_category",
                "v1_instance",
                "v2_category",
                "v2_instance",
                "v2_confidence",
                "reference_score",
                "v1_score",
                "v2_score",
            ]
        },
        "v1_config_checksum": bundle["v1_config"].checksum,
        "v2_config_checksum": bundle["v2_config"].checksum,
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return root


def load_sample_arrays(root: str | Path) -> dict[str, Any]:
    root = Path(root)
    truth = np.load(root / "truth_templates.npz")
    masks = np.load(root / "solver_masks.npz")
    scores = np.load(root / "solver_float64.npz")
    return {
        "image": np.load(root / "image_rgb.npy"),
        "templates": truth["templates"],
        "template_instance_ids": truth["instance_ids"],
        "template_category_ids": truth["category_ids"],
        "gt_instance": truth["gt_instance"],
        "gt_category": truth["gt_category"],
        "reference_score": truth["reference_score"],
        "v1_instance": masks["v1_instance"],
        "v2_category": masks["v2_category"],
        "v2_instance": masks["v2_instance"],
        "v2_confidence": masks["v2_confidence"],
        "v1_score": scores["v1_score"],
        "v2_score": scores["v2_score"],
    }
