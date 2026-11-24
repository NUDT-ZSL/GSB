"""Fixed-seed local samples; no network, scanner, or external dataset is used."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import numpy as np

from .pointcloud import (
    dataset_digest,
    encode_pointcloud,
    parse_pointcloud_bytes,
)
from .util import atomic_write_bytes, atomic_write_json


SAMPLE_SEED = 20261125
SAMPLE_CACHE_KEY = "sample_scene_seed_20261125"
SAMPLE_WORLD_SCALE = 0.01
SAMPLE_ORIGIN_OFFSET = np.asarray([0.02, -0.01, 0.03], dtype=np.float64)


def _normalize(vectors: np.ndarray) -> np.ndarray:
    lengths = np.linalg.norm(vectors, axis=1)
    return vectors / lengths[:, None]


def _build_point_records() -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(SAMPLE_SEED)
    sphere_count = 5400
    plane_count = 744
    total = sphere_count + plane_count

    u = rng.random(sphere_count)
    v = rng.random(sphere_count)
    theta = 2.0 * np.pi * u
    phi = np.arccos(1.0 - 2.0 * v)
    radius = 0.72 * (1.0 + 0.045 * (rng.random(sphere_count) - 0.5))
    sphere = np.column_stack(
        [
            radius * np.sin(phi) * np.cos(theta),
            radius * np.cos(phi),
            radius * np.sin(phi) * np.sin(theta),
        ]
    )
    sphere_normals = _normalize(sphere)

    plane_x = rng.uniform(-0.58, 0.58, plane_count)
    plane_y = rng.uniform(-0.39, 0.39, plane_count)
    plane_z = 0.92 + rng.normal(0.0, 0.006, plane_count)
    plane = np.column_stack([plane_x, plane_y, plane_z])
    plane_normals = np.tile(np.asarray([0.0, 0.0, -1.0]), (plane_count, 1))

    world = np.vstack([sphere, plane])
    normals = np.vstack([sphere_normals, plane_normals])
    gray = np.empty(total, dtype=np.float64)
    gray[:sphere_count] = 0.24 + 0.66 * rng.random(sphere_count)
    gray[sphere_count:] = 0.36 + 0.58 * rng.random(plane_count)
    radius_world = np.empty(total, dtype=np.float64)
    radius_world[:sphere_count] = 0.012 + 0.018 * rng.random(sphere_count)
    radius_world[sphere_count:] = 0.008 + 0.014 * rng.random(plane_count)

    raw = np.empty((total, 8), dtype=np.float32)
    raw[:, 0:3] = (world - SAMPLE_ORIGIN_OFFSET[None, :]) / SAMPLE_WORLD_SCALE
    raw[:, 3] = gray
    raw[:, 4:7] = normals
    raw[:, 7] = radius_world / SAMPLE_WORLD_SCALE
    return raw, world


def _edit_records(base_raw: np.ndarray) -> tuple[np.ndarray, list[dict[str, Any]]]:
    edited = np.array(base_raw, dtype=np.float32, copy=True)
    region_specs = [
        (900, 32, np.asarray([0.19, 0.12, 0.24]), "rescan patch A"),
        (2600, 24, np.asarray([-0.22, 0.08, 0.19]), "rescan patch B"),
        (5800, 36, np.asarray([0.05, -0.06, 0.07]), "occluder rescan"),
    ]
    regions: list[dict[str, Any]] = []
    for region_index, (start, count, displacement, reason) in enumerate(region_specs):
        old_chunk = np.array(edited[start : start + count], copy=True)
        edited[start : start + count, 0:3] += displacement.astype(np.float32) / SAMPLE_WORLD_SCALE
        edited[start : start + count, 3] = np.clip(
            0.15 + 0.8 * (1.0 - edited[start : start + count, 3]),
            0.0,
            1.0,
        )
        if start < 5400:
            edited_positions = (
                edited[start : start + count, 0:3].astype(np.float64) * SAMPLE_WORLD_SCALE
                + SAMPLE_ORIGIN_OFFSET[None, :]
            )
            edited[start : start + count, 4:7] = _normalize(edited_positions).astype(np.float32)
        new_chunk = edited[start : start + count]
        regions.append(
            {
                "id": f"edit_{region_index:02d}",
                "start": start,
                "count": count,
                "reason": reason,
                "old_digest": hashlib.sha256(
                    np.asarray(old_chunk, dtype="<f4").tobytes(order="C")
                ).hexdigest(),
                "new_digest": hashlib.sha256(
                    np.asarray(new_chunk, dtype="<f4").tobytes(order="C")
                ).hexdigest(),
            }
        )
    return edited, regions


def _cloud_from_raw(raw: np.ndarray, name: str):
    data = encode_pointcloud(raw)
    return parse_pointcloud_bytes(data, name, SAMPLE_WORLD_SCALE, SAMPLE_ORIGIN_OFFSET)


def generate_samples(out_dir: Path) -> dict[str, Any]:
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    base_raw, _ = _build_point_records()
    edited_raw, regions = _edit_records(base_raw)
    base_bytes = encode_pointcloud(base_raw)
    edited_bytes = encode_pointcloud(edited_raw)
    base_cloud = _cloud_from_raw(base_raw, "points_base.bin")
    edited_cloud = _cloud_from_raw(edited_raw, "points_edited.bin")
    base_dataset_digest = dataset_digest(base_cloud)
    edited_dataset_digest = dataset_digest(edited_cloud)

    atomic_write_bytes(out_dir / "points_base.bin", base_bytes)
    atomic_write_bytes(out_dir / "points_edited.bin", edited_bytes)

    cameras = [
        {
            "id": "cam_front",
            "position": [0.15, 0.25, 3.10],
            "target": [0.0, 0.0, 0.0],
            "up": [0.0, 1.0, 0.0],
            "vertical_fov_deg": 50.0,
            "aspect_ratio": 1.5,
        },
        {
            "id": "cam_left",
            "position": [-2.70, 0.55, 1.35],
            "target": [0.0, 0.0, 0.0],
            "up": [0.0, 1.0, 0.0],
            "vertical_fov_deg": 52.0,
            "aspect_ratio": 1.5,
        },
        {
            "id": "cam_top",
            "position": [2.05, 1.75, 2.05],
            "target": [0.0, 0.0, 0.0],
            "up": [0.0, 1.0, 0.0],
            "vertical_fov_deg": 55.0,
            "aspect_ratio": 1.5,
        },
    ]
    block_config = {"width": 16, "height": 16}
    render = {
        "width": 96,
        "height": 64,
        "supersample": 2,
        "blocks": block_config,
        "max_workers": 4,
        "memory_budget_bytes": 4 * 16 * 16 * 2 * 2 * 16,
        "round_budget_units": 150.0,
    }

    def task_document(edit_manifest: str | None) -> dict[str, Any]:
        document: dict[str, Any] = {
            "schema_version": 1,
            "task_id": "sparse_scan_art_review",
            "cache_key": SAMPLE_CACHE_KEY,
            "pointcloud": {
                "path": "points_edited.bin" if edit_manifest else "points_base.bin",
                "world_scale": SAMPLE_WORLD_SCALE,
                "origin_offset": [float(value) for value in SAMPLE_ORIGIN_OFFSET],
            },
            "render": render,
            "cameras": cameras,
        }
        if edit_manifest is not None:
            document["edit_manifest"] = edit_manifest
        return document

    atomic_write_json(out_dir / "task_base.json", task_document(None))
    atomic_write_json(out_dir / "task_edited.json", task_document("edits.json"))
    edit_document = {
        "schema_version": 1,
        "base_pointcloud": "points_base.bin",
        "edited_pointcloud": "points_edited.bin",
        "base_dataset_digest": base_dataset_digest,
        "edited_dataset_digest": edited_dataset_digest,
        "base_point_count": base_cloud.point_count,
        "edited_point_count": edited_cloud.point_count,
        "regions": regions,
    }
    atomic_write_json(out_dir / "edits.json", edit_document)
    atomic_write_json(out_dir / "camera_params.json", {"cameras": cameras})
    atomic_write_json(
        out_dir / "block_config.json",
        {
            "output": {"width": render["width"], "height": render["height"]},
            "supersample": render["supersample"],
            "blocks": block_config,
        },
    )
    manifest = {
        "schema_version": 1,
        "seed": SAMPLE_SEED,
        "cache_key": SAMPLE_CACHE_KEY,
        "point_count": base_cloud.point_count,
        "base_dataset_digest": base_dataset_digest,
        "edited_dataset_digest": edited_dataset_digest,
        "edit_manifest": "edits.json",
        "task_base": "task_base.json",
        "task_edited": "task_edited.json",
        "points_base_sha256": hashlib.sha256(base_bytes).hexdigest(),
        "points_edited_sha256": hashlib.sha256(edited_bytes).hexdigest(),
        "regions": regions,
    }
    atomic_write_json(out_dir / "sample_manifest.json", manifest)
    return manifest

