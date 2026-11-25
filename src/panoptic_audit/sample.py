from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .groundtruth import IDENTITY_BASE, IGNORE_CATEGORY, IGNORE_IDENTITY
from .hashing import canonical_checksum, file_checksum
from .pngio import write_rgb_png

SEED = 20261126
HEIGHT = 16
WIDTH = 16


@dataclass(frozen=True)
class Entity:
    category_id: int
    instance_id: int
    name: str
    is_thing: bool


ENTITIES = [
    Entity(0, 0, "background", False),
    Entity(3, 0, "road", False),
    Entity(1, 1, "person_1", True),
    Entity(1, 2, "person_2", True),
    Entity(2, 1, "vehicle_1", True),
]


def _rect(array: np.ndarray, y0: int, y1: int, x0: int, x1: int, value: Any) -> None:
    array[y0:y1, x0:x1] = value


def build_scene(seed: int = SEED) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    category = np.full((HEIGHT, WIDTH), 3, dtype=np.int64)
    instance = np.zeros((HEIGHT, WIDTH), dtype=np.int64)
    ignore = np.zeros((HEIGHT, WIDTH), dtype=bool)

    ignore[0:2, :] = True
    category[ignore] = IGNORE_CATEGORY

    _rect(category, 3, 7, 2, 5, 1)
    _rect(instance, 3, 7, 2, 5, 1)
    _rect(category, 8, 12, 10, 14, 1)
    _rect(instance, 8, 12, 10, 14, 2)
    _rect(category, 12, 15, 1, 7, 2)
    _rect(instance, 12, 15, 1, 7, 1)

    confidence = np.full((HEIGHT, WIDTH), 0.94, dtype=np.float64)
    low_confidence_points = [(12, 10), (5, 13), (4, 6), (6, 14)]
    for index, (x, y) in enumerate(low_confidence_points):
        confidence[y, x] = (0.42, 0.38, 0.35, 0.30)[index]

    rng = np.random.default_rng(seed)
    palette = {
        IGNORE_CATEGORY: np.array([48, 48, 52], dtype=np.int16),
        0: np.array([35, 70, 45], dtype=np.int16),
        1: np.array([220, 72, 72], dtype=np.int16),
        2: np.array([64, 96, 220], dtype=np.int16),
        3: np.array([128, 128, 120], dtype=np.int16),
    }
    rgb = np.zeros((HEIGHT, WIDTH, 3), dtype=np.int16)
    for cat, color in palette.items():
        rgb[category == cat] = color
    rgb[instance > 0] = rgb[instance > 0] + (instance[instance > 0, None] * 18)
    noise = rng.integers(-10, 11, size=rgb.shape, dtype=np.int16)
    rgb = np.clip(rgb + noise, 0, 255).astype(np.uint8)
    return rgb, category, instance, confidence


def _identity_matrix(category: np.ndarray, instance: np.ndarray, ignore: np.ndarray) -> np.ndarray:
    identity = category * IDENTITY_BASE + instance
    identity[ignore] = IGNORE_IDENTITY
    return identity.astype(np.int64)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def _encode_runs(array: np.ndarray) -> list[list[int]]:
    flat = array.flatten(order="F")
    runs: list[list[int]] = []
    for value in flat:
        value_int = int(value)
        if runs and runs[-1][0] == value_int:
            runs[-1][1] += 1
        else:
            runs.append([value_int, 1])
    return runs


def _write_v1(out_dir: Path, category: np.ndarray, instance: np.ndarray, ignore: np.ndarray) -> None:
    v1_cat = category.copy()
    v1_inst = instance.copy()
    v1_ignore = ignore.copy()
    # Injected isolated semantic deviations.
    v1_cat[1, 15] = 3
    v1_inst[1, 15] = 0
    v1_ignore[1, 15] = False
    v1_cat[7, 7] = 1
    v1_inst[7, 7] = 1
    v1_ignore[7, 7] = False

    identities = _identity_matrix(v1_cat, v1_inst, v1_ignore)
    rows = []
    for y in range(HEIGHT):
        identity_row = [int(value) for value in identities[y]]
        checksum = canonical_checksum({"y": y, "identities": identity_row})
        rows.append({"y": y, "identities": identity_row, "checksum": checksum})
    payload = {
        "format": "v1_row_identity",
        "version": "v1",
        "height": HEIGHT,
        "width": WIDTH,
        "identity_base": IDENTITY_BASE,
        "ignore_identity": IGNORE_IDENTITY,
        "reliability_weight": 0.80,
        "rows": rows,
    }
    _write_json(out_dir / "gt_v1.json", payload)


def _write_v2(out_dir: Path, category: np.ndarray, instance: np.ndarray, confidence: np.ndarray) -> None:
    v2_cat = category.copy()
    v2_inst = instance.copy()
    v2_conf = confidence.copy()
    v2_cat[2, 10] = 2
    v2_inst[2, 10] = 1
    v2_conf[2, 10] = 0.41

    v2_dir = out_dir / "gt_v2"
    v2_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = v2_dir / "gt_v2_meta.sqlite"
    if metadata_path.exists():
        metadata_path.unlink()

    shard_metadata: list[tuple[int, str, int, int, str]] = []
    block_metadata: list[tuple[str, int, int, str]] = []
    split = 8
    for shard_id, (start, end) in enumerate(((0, split), (split, HEIGHT))):
        rows = []
        for y in range(start, end):
            body = {
                "y": y,
                "category": [int(v) for v in v2_cat[y]],
                "instance": [int(v) for v in v2_inst[y]],
                "confidence": [round(float(v), 6) for v in v2_conf[y]],
            }
            checksum = canonical_checksum(body)
            rows.append({**body, "checksum": checksum})
            block_metadata.append((f"v2:row:{y}", shard_id, y, checksum))

        body = {
            "format": "v2_category_instance_confidence",
            "version": "v2",
            "shard_id": shard_id,
            "row_start": start,
            "row_end": end,
            "rows": rows,
        }
        shard_checksum = canonical_checksum(body)
        payload = {**body, "checksum": shard_checksum}
        rel_path = f"shard_{shard_id:03d}.json"
        _write_json(v2_dir / rel_path, payload)
        shard_metadata.append((shard_id, rel_path, start, end, shard_checksum))

    conn = sqlite3.connect(metadata_path)
    try:
        conn.executescript(
            """
            CREATE TABLE format_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE shards (
                shard_id INTEGER PRIMARY KEY,
                path TEXT NOT NULL,
                row_start INTEGER NOT NULL,
                row_end INTEGER NOT NULL,
                checksum TEXT NOT NULL
            );
            CREATE TABLE blocks (
                block_id TEXT PRIMARY KEY,
                shard_id INTEGER NOT NULL,
                y INTEGER NOT NULL,
                checksum TEXT NOT NULL
            );
            CREATE TABLE entities (
                category_id INTEGER NOT NULL,
                instance_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                is_thing INTEGER NOT NULL,
                PRIMARY KEY(category_id, instance_id)
            );
            """
        )
        conn.executemany("INSERT INTO format_meta(key, value) VALUES(?, ?)", [
            ("format", "v2_category_instance_confidence"),
            ("version", "v2"),
            ("height", str(HEIGHT)),
            ("width", str(WIDTH)),
        ])
        conn.executemany("INSERT INTO shards VALUES(?, ?, ?, ?, ?)", shard_metadata)
        conn.executemany("INSERT INTO blocks VALUES(?, ?, ?, ?)", block_metadata)
        conn.executemany("INSERT INTO entities VALUES(?, ?, ?, ?)", [
            (e.category_id, e.instance_id, e.name, int(e.is_thing)) for e in ENTITIES
        ])
        conn.commit()
    finally:
        conn.close()


def _write_rle(out_dir: Path, category: np.ndarray, instance: np.ndarray, ignore: np.ndarray) -> None:
    rle_cat = category.copy()
    rle_inst = instance.copy()
    rle_ignore = ignore.copy()
    rle_cat[15, 8] = 1
    rle_inst[15, 8] = 2
    rle_ignore[15, 8] = False

    ignore_values = rle_ignore.astype(np.int64)
    channels = {}
    for name, array in (("category", rle_cat), ("instance", rle_inst), ("ignore", ignore_values)):
        runs = _encode_runs(array)
        channels[name] = {"runs": runs, "checksum": canonical_checksum({"runs": runs})}

    payload = {
        "format": "coco_rle_panoptic",
        "version": "rle-v1",
        "height": HEIGHT,
        "width": WIDTH,
        "order": "F",
        "encoding_note": "COCO-style run lengths in Fortran/column-major order; each run is [symbol, count].",
        "reliability_weight": 0.90,
        "channels": channels,
    }
    _write_json(out_dir / "gt_rle.json", payload)


def _probe_plan() -> dict[str, Any]:
    probes = [
        {"probe_id": "P-DUP-LOW", "x": 3, "y": 4, "expected_category": 1, "expected_instance": 1, "expected_ignore": False, "priority": 5, "time_budget_ms": 4.0},
        {"probe_id": "P-PERSON-1", "x": 3, "y": 4, "expected_category": 1, "expected_instance": 1, "expected_ignore": False, "priority": 9, "time_budget_ms": 4.0},
        {"probe_id": "P-BAD-OUT-OF-RANGE", "x": 99, "y": 4, "expected_category": 1, "expected_instance": 1, "priority": 1, "time_budget_ms": 2.0},
        {"probe_id": "P-BAD-MISSING", "x": 1, "y": 1, "expected_instance": 0, "priority": 2, "time_budget_ms": 2.0},
        {"probe_id": "P-IGNORE", "x": 0, "y": 0, "expected_category": IGNORE_CATEGORY, "expected_instance": 0, "expected_ignore": True, "priority": 10, "time_budget_ms": 3.0},
        {"probe_id": "P-PERSON-2", "x": 11, "y": 9, "expected_category": 1, "expected_instance": 2, "expected_ignore": False, "priority": 8, "time_budget_ms": 4.0},
        {"probe_id": "P-VEHICLE", "x": 3, "y": 13, "expected_category": 2, "expected_instance": 1, "expected_ignore": False, "priority": 7, "time_budget_ms": 4.0},
        {"probe_id": "P-MISMATCH", "x": 2, "y": 4, "expected_category": 2, "expected_instance": 99, "expected_ignore": False, "priority": 6, "time_budget_ms": 5.0},
        {"probe_id": "P-ROAD", "x": 5, "y": 5, "expected_category": 3, "expected_instance": 0, "expected_ignore": False, "priority": 5, "time_budget_ms": 3.0},
        {"probe_id": "P-LOW-1", "x": 12, "y": 10, "expected_category": 1, "expected_instance": 2, "expected_ignore": False, "priority": 4, "time_budget_ms": 8.0},
        {"probe_id": "P-LOW-2", "x": 5, "y": 13, "expected_category": 2, "expected_instance": 1, "expected_ignore": False, "priority": 3, "time_budget_ms": 8.0},
        {"probe_id": "P-LOW-3", "x": 4, "y": 6, "expected_category": 1, "expected_instance": 1, "expected_ignore": False, "priority": 2, "time_budget_ms": 8.0},
        {"probe_id": "P-LOW-4", "x": 6, "y": 14, "expected_category": 2, "expected_instance": 1, "expected_ignore": False, "priority": 2, "time_budget_ms": 8.0},
    ]
    return {
        "format": "active_probe_plan",
        "version": "plan-v1",
        "seed": SEED,
        "coordinate_convention": "x is column and y is row; origin is top-left.",
        "probes": probes,
    }


def generate_sample(out_dir: str | Path) -> dict[str, Any]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rgb, category, instance, confidence = build_scene()
    write_rgb_png(out_dir / "rgb.png", rgb)
    _write_v1(out_dir, category, instance, ignore=category == IGNORE_CATEGORY)
    _write_v2(out_dir, category, instance, confidence)
    _write_rle(out_dir, category, instance, ignore=category == IGNORE_CATEGORY)
    _write_json(out_dir / "probe_plan.json", _probe_plan())

    files = ["rgb.png", "gt_v1.json", "gt_v2/shard_000.json", "gt_v2/shard_001.json", "gt_v2/gt_v2_meta.sqlite", "gt_rle.json", "probe_plan.json"]
    manifest = {
        "format": "fixed_seed_sample_manifest",
        "version": "sample-v1",
        "seed": SEED,
        "height": HEIGHT,
        "width": WIDTH,
        "entities": [asdict(entity) for entity in ENTITIES],
        "files": {name: file_checksum(out_dir / name) for name in files},
    }
    _write_json(out_dir / "manifest.json", manifest)
    return manifest


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Generate deterministic panoptic audit sample")
    parser.add_argument("--out", default="samples/fixed_seed", help="output sample directory")
    args = parser.parse_args()
    manifest = generate_sample(args.out)
    print(json.dumps({"generated": args.out, "seed": manifest["seed"], "files": len(manifest["files"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
