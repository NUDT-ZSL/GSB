"""Load and validate generated local sample inputs."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
from PIL import Image

from .errors import (
    E_DATABASE,
    E_IMAGE_DECODE,
    E_IMAGE_MISSING,
    E_IMAGE_SIZE,
    E_LAYOUT_COVERAGE,
    E_MANIFEST,
    E_MASK_DOMAIN,
    E_MASK_SHAPE,
    E_OFFSET_DOMAIN,
    E_OFFSET_FINITE,
    E_OVERLAP,
    E_SUBPIXEL_DOMAIN,
    E_TIME_CONVERSION,
    E_V1_DESCRIPTOR,
    E_V2_DESCRIPTOR,
    PipelineError,
    fail,
)
from .geometry import intersection
from .hashing import digest_canonical, iter_deterministic_sqlite_rows, sha256_file
from .masking import validate_weight_array


@dataclass
class LoadedSample:
    root: Path
    project: Dict[str, Any]
    tiles: List[Dict[str, Any]]
    truth: Dict[str, Any]
    truth_by_id: Dict[str, Dict[str, Any]]
    v1: Dict[str, Any]
    v2_manifest: Dict[str, Any]
    mask_records: List[Dict[str, Any]]
    version_id: str
    image_by_id: Dict[str, np.ndarray] = field(default_factory=dict)


def _read_json(path: Path, code: str) -> Any:
    if not path.exists():
        raise fail(code, f"required JSON file is missing: {path}", path=str(path))
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise fail(code, f"invalid JSON: {exc.msg}", path=str(path), line=exc.lineno)


def _resolve(root: Path, value: str) -> Path:
    path = Path(str(value))
    return path if path.is_absolute() else (root / path).resolve()


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and np.isfinite(value)


def _validate_offset_pair(
    value: Any,
    code_finite: str,
    code_domain: str,
    domain: float,
    source: str,
) -> List[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise fail(code_finite, "offset must be a [dx, dy] pair", source=source)
    if not all(_finite_number(item) for item in value):
        raise fail(code_finite, "offset components must be finite numbers", source=source, value=value)
    offsets = [float(value[0]), float(value[1])]
    if max(abs(offsets[0]), abs(offsets[1])) > domain:
        raise fail(
            code_domain,
            "offset falls outside declared domain",
            source=source,
            value=offsets,
            domain_px=domain,
        )
    return offsets


def _load_image(path: Path, tile: Dict[str, Any]) -> np.ndarray:
    if not path.exists():
        raise fail(E_IMAGE_MISSING, "tile image is missing", path=str(path), tile_id=tile.get("tile_id"))
    try:
        with Image.open(path) as image:
            image.load()
            if image.mode != "L":
                image = image.convert("L")
            array = np.asarray(image, dtype=np.uint8)
    except PipelineError:
        raise
    except Exception as exc:
        raise fail(E_IMAGE_DECODE, f"cannot decode grayscale image: {exc}", path=str(path))
    expected = (int(tile["height"]), int(tile["width"]))
    if array.shape != expected:
        raise fail(
            E_IMAGE_SIZE,
            "image dimensions do not match tile declaration",
            tile_id=tile.get("tile_id"),
            expected_height_width=list(expected),
            got_height_width=list(array.shape),
        )
    return array


def _validate_layout(project: Dict[str, Any], tiles: List[Dict[str, Any]]) -> None:
    canvas = project.get("canvas")
    if not isinstance(canvas, dict) or not _is_int(canvas.get("width")) or _is_int(canvas.get("height")) is False:
        raise fail(E_MANIFEST, "canvas.width and canvas.height must be integers")
    width = int(canvas["width"])
    height = int(canvas["height"])
    if width <= 0 or height <= 0:
        raise fail(E_MANIFEST, "canvas dimensions must be positive")

    coverage = np.zeros((height, width), dtype=np.uint8)
    for tile in tiles:
        for key in ("tile_id", "image", "x", "y", "width", "height"):
            if key not in tile:
                raise fail(E_MANIFEST, f"tile is missing {key}", tile=tile)
        if not all(_is_int(tile[key]) for key in ("x", "y", "width", "height")):
            raise fail(E_MANIFEST, "tile x/y/width/height must be integers", tile_id=tile.get("tile_id"))
        if tile["width"] <= 0 or tile["height"] <= 0:
            raise fail(E_MANIFEST, "tile dimensions must be positive", tile_id=tile["tile_id"])
        x0, y0 = int(tile["x"]), int(tile["y"])
        x1, y1 = x0 + int(tile["width"]), y0 + int(tile["height"])
        if x0 < 0 or y0 < 0 or x1 > width or y1 > height:
            raise fail(
                E_LAYOUT_COVERAGE,
                "tile extends outside declared canvas",
                tile_id=tile["tile_id"],
                canvas=[width, height],
                tile_bounds=[x0, y0, x1, y1],
            )
        coverage[y0:y1, x0:x1] += 1

    if np.any(coverage == 0):
        missing_y, missing_x = np.where(coverage == 0)
        first = [int(missing_x[0]), int(missing_y[0])]
        raise fail(
            E_LAYOUT_COVERAGE,
            "tile arrangement does not cover the whole canvas",
            uncovered_pixels=int(np.sum(coverage == 0)),
            first_canvas_xy=first,
        )

    ids = [tile["tile_id"] for tile in tiles]
    if len(ids) != len(set(ids)):
        raise fail(E_MANIFEST, "tile_id values must be unique", tile_ids=ids)


def _validate_overlaps(project: Dict[str, Any], tiles_by_id: Dict[str, Dict[str, Any]]) -> None:
    overlaps = project.get("overlaps")
    if not isinstance(overlaps, list) or not overlaps:
        raise fail(E_OVERLAP, "project must declare at least one overlap")
    for overlap in overlaps:
        tile_ids = overlap.get("tile_ids")
        bbox = overlap.get("bbox")
        if not isinstance(tile_ids, list) or len(tile_ids) != 2:
            raise fail(E_OVERLAP, "overlap must name exactly two tile_ids", overlap=overlap)
        if tile_ids[0] not in tiles_by_id or tile_ids[1] not in tiles_by_id:
            raise fail(E_OVERLAP, "overlap references unknown tile", tile_ids=tile_ids)
        expected = intersection(tiles_by_id[tile_ids[0]], tiles_by_id[tile_ids[1]])
        if expected is None:
            raise fail(E_OVERLAP, "declared tiles have no geometric intersection", overlap_id=overlap.get("overlap_id"))
        ex0, ey0, ex1, ey1 = expected
        if not isinstance(bbox, dict):
            raise fail(E_OVERLAP, "overlap bbox must be an object", overlap_id=overlap.get("overlap_id"))
        actual = (
            bbox.get("x"),
            bbox.get("y"),
            None if bbox.get("width") is None else int(bbox["x"]) + int(bbox["width"]),
            None if bbox.get("height") is None else int(bbox["y"]) + int(bbox["height"]),
        )
        if actual != (ex0, ey0, ex1, ey1):
            raise fail(
                E_OVERLAP,
                "overlap declaration does not match tile geometry",
                overlap_id=overlap.get("overlap_id"),
                expected_bbox={"x": ex0, "y": ey0, "width": ex1 - ex0, "height": ey1 - ey0},
                got_bbox=bbox,
            )


def _load_v1(
    root: Path,
    project: Dict[str, Any],
    tiles_by_id: Dict[str, Dict[str, Any]],
    offset_domain: float,
) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    relative = project.get("v1_descriptor")
    if not relative:
        raise fail(E_V1_DESCRIPTOR, "project is missing v1_descriptor")
    path = _resolve(root, str(relative))
    raw = _read_json(path, E_V1_DESCRIPTOR)
    if int(raw.get("generation", 0)) != 1:
        raise fail(E_V1_DESCRIPTOR, "v1 descriptor generation must equal 1")
    origin = raw.get("time_origin_ms")
    if not _finite_number(origin) or float(origin) < 0:
        raise fail(E_TIME_CONVERSION, "v1 time_origin_ms must be a non-negative finite number")
    if not isinstance(raw.get("records"), list):
        raise fail(E_V1_DESCRIPTOR, "v1 records must be a list")

    records: List[Dict[str, Any]] = []
    for index, record in enumerate(raw["records"]):
        source = f"{relative}#/records/{index}"
        tile_id = record.get("tile_id")
        tile = tiles_by_id.get(str(tile_id))
        if tile is None:
            raise fail(E_V1_DESCRIPTOR, "v1 record references unknown tile", source=source, tile_id=tile_id)
        rel_seconds = record.get("captured_at_rel_s")
        if not _finite_number(rel_seconds):
            raise fail(E_TIME_CONVERSION, "v1 relative second timestamp must be finite", source=source)
        milliseconds = float(origin) + float(rel_seconds) * 1000.0
        if not np.isfinite(milliseconds):
            raise fail(E_TIME_CONVERSION, "v1 timestamp cannot be converted to milliseconds", source=source)
        if abs(float(rel_seconds) * 1000.0 - round(float(rel_seconds) * 1000.0)) > 1e-9:
            raise fail(
                E_TIME_CONVERSION,
                "v1 relative seconds must resolve to integer absolute milliseconds",
                source=source,
                rel_s=rel_seconds,
            )

        flat_offset = record.get("flat_integer_offset", record.get("integer_offset_xy"))
        if not isinstance(flat_offset, list) or len(flat_offset) != 2 or not all(_is_int(v) for v in flat_offset):
            raise fail(E_OFFSET_FINITE, "v1 flat_integer_offset must contain two integers", source=source)
        _validate_offset_pair(flat_offset, E_OFFSET_FINITE, E_OFFSET_DOMAIN, offset_domain, source)

        mask = record.get("mask")
        if not isinstance(mask, dict):
            raise fail(E_MASK_SHAPE, "v1 record must contain a pixel mask", source=source)
        width, height = int(tile["width"]), int(tile["height"])
        if _is_int(mask.get("width")) and _is_int(mask.get("height")):
            if int(mask["width"]) != width or int(mask["height"]) != height:
                raise fail(E_MASK_SHAPE, "v1 mask dimensions must match tile", source=source)
        pixels = mask.get("pixels")
        if not isinstance(pixels, list) or len(pixels) != width * height:
            raise fail(E_MASK_SHAPE, "v1 hard mask has wrong pixel count", source=source, expected=width * height)
        if any((not _is_int(value)) or value not in (0, 255) for value in pixels):
            raise fail(E_MASK_DOMAIN, "v1 hard mask pixels must be 0 or 255", source=source)
        weights = (np.asarray(pixels, dtype=np.float32) / 255.0).reshape(height, width)
        records.append(
            {
                "generation": 1,
                "record_id": str(record.get("record_id", f"v1-{index}")),
                "mask_id": str(record.get("mask_id", record.get("record_id", f"mask-{tile_id}"))),
                "tile_id": str(tile_id),
                "effective_at_ms": int(round(milliseconds)),
                "tombstone": False,
                "integer_offset_xy": [int(flat_offset[0]), int(flat_offset[1])],
                "weights": weights,
                "source": source,
                "source_index": index,
            }
        )
    return raw, records


def _load_v2(
    root: Path,
    project: Dict[str, Any],
    tiles_by_id: Dict[str, Dict[str, Any]],
    offset_domain: float,
) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    relative = project.get("v2_manifest")
    if not relative:
        raise fail(E_V2_DESCRIPTOR, "project is missing v2_manifest")
    manifest_path = _resolve(root, str(relative))
    manifest = _read_json(manifest_path, E_V2_DESCRIPTOR)
    v2_dir = manifest_path.parent
    database_path = v2_dir / str(manifest.get("database", "registry.sqlite3"))
    if not database_path.exists():
        raise fail(E_DATABASE, "v2 SQLite metadata database is missing", path=str(database_path))

    shard_payloads: Dict[str, Any] = {}
    for shard in manifest.get("shards", []):
        shard_path = v2_dir / str(shard["path"])
        if not shard_path.exists():
            raise fail(E_V2_DESCRIPTOR, "v2 shard is missing", path=str(shard_path))
        expected_sha = shard.get("sha256")
        actual_sha = sha256_file(shard_path)
        if expected_sha and expected_sha != actual_sha:
            raise fail(
                E_V2_DESCRIPTOR,
                "v2 shard checksum mismatch",
                shard_id=shard.get("shard_id"),
                expected=expected_sha,
                actual=actual_sha,
            )
        shard_payloads[str(shard.get("shard_id"))] = _read_json(shard_path, E_V2_DESCRIPTOR)

    try:
        database_uri = database_path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(database_uri, uri=True)
        connection.row_factory = sqlite3.Row
        table_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if not {"shards", "records"}.issubset(table_names):
            raise fail(E_DATABASE, "v2 database must contain shards and records tables", tables=sorted(table_names))
        db_rows = connection.execute(
            "SELECT * FROM records ORDER BY tile_id, effective_at_ms, record_id, record_index"
        ).fetchall()
    except sqlite3.Error as exc:
        raise fail(E_DATABASE, f"cannot read v2 SQLite metadata: {exc}", path=str(database_path))
    finally:
        try:
            connection.close()
        except Exception:
            pass

    records: List[Dict[str, Any]] = []
    for row in db_rows:
        row = dict(row)
        source = f"{relative}::{row['shard_id']}#/records/{row['record_index']}"
        tile_id = str(row["tile_id"])
        tile = tiles_by_id.get(tile_id)
        if tile is None:
            raise fail(E_V2_DESCRIPTOR, "v2 database references unknown tile", source=source, tile_id=tile_id)
        effective_ms = row["effective_at_ms"]
        if not _is_int(effective_ms) or effective_ms < 0:
            raise fail(E_TIME_CONVERSION, "v2 absolute timestamp must be a non-negative integer milliseconds", source=source)
        offsets = [row["offset_dx"], row["offset_dy"]]
        _validate_offset_pair(offsets, E_OFFSET_FINITE, E_OFFSET_DOMAIN, offset_domain, source)

        shard = shard_payloads.get(row["shard_id"])
        if shard is None or not isinstance(shard.get("records"), list):
            raise fail(E_V2_DESCRIPTOR, "v2 database row points to unknown shard", source=source)
        index = int(row["record_index"])
        if index < 0 or index >= len(shard["records"]):
            raise fail(E_V2_DESCRIPTOR, "v2 database record_index is out of range", source=source)
        payload = shard["records"][index]
        if payload.get("record_id") != row["record_id"]:
            raise fail(E_DATABASE, "v2 database/shard record_id mismatch", source=source)

        tombstone = bool(row["tombstone"])
        weights = None
        if not tombstone:
            offset_value = payload.get("subpixel_offset_xy", payload.get("subpixel_offset_flat"))
            _validate_offset_pair(offset_value, E_OFFSET_FINITE, E_SUBPIXEL_DOMAIN, offset_domain, source)
            soft_mask = payload.get("soft_mask")
            if not isinstance(soft_mask, dict):
                raise fail(E_MASK_SHAPE, "active v2 record must contain a soft_mask", source=source)
            width, height = int(tile["width"]), int(tile["height"])
            if int(soft_mask.get("width", width)) != width or int(soft_mask.get("height", height)) != height:
                raise fail(E_MASK_SHAPE, "v2 soft mask dimensions must match tile", source=source)
            weights = validate_weight_array(
                soft_mask.get("weights"), width, height, E_MASK_DOMAIN
            )

        records.append(
            {
                "generation": 2,
                "record_id": str(row["record_id"]),
                "mask_id": str(row["mask_id"] or payload.get("mask_id") or row["record_id"]),
                "tile_id": tile_id,
                "effective_at_ms": int(effective_ms),
                "tombstone": tombstone,
                "subpixel_offset_xy": [float(offsets[0]), float(offsets[1])],
                "weights": weights,
                "source": source,
                "source_index": index,
            }
        )
    return manifest, records


def compute_version_id(sample: "LoadedSample") -> str:
    image_digests: List[Dict[str, Any]] = []
    for tile in sorted(sample.tiles, key=lambda item: item["tile_id"]):
        array = sample.image_by_id[tile["tile_id"]]
        image_digests.append(
            {
                "tile_id": tile["tile_id"],
                "width": tile["width"],
                "height": tile["height"],
                "pixels_sha256": __import__("hashlib").sha256(array.tobytes()).hexdigest(),
            }
        )

    v2_dir = (sample.root / str(sample.project["v2_manifest"])).parent
    shards = []
    for shard in sample.v2_manifest.get("shards", []):
        shard_path = v2_dir / str(shard["path"])
        shards.append(
            {
                "shard_id": shard["shard_id"],
                "path": shard["path"],
                "canonical": _read_json(shard_path, E_V2_DESCRIPTOR),
            }
        )

    database_path = v2_dir / str(sample.v2_manifest.get("database", "registry.sqlite3"))
    database_uri = database_path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(database_uri, uri=True)
    try:
        db_dump = list(iter_deterministic_sqlite_rows(connection))
    finally:
        connection.close()

    version_payload = {
        "images": image_digests,
        "truth": sample.truth,
        "v1": sample.v1,
        "v2_manifest": sample.v2_manifest,
        "v2_shards": sorted(shards, key=lambda item: item["shard_id"]),
        "v2_sqlite_canonical": db_dump,
    }
    return digest_canonical(version_payload)


def load_project(sample_dir: str | Path) -> LoadedSample:
    root = Path(sample_dir).resolve()
    project_path = root / "project.json"
    project = _read_json(project_path, E_MANIFEST)
    if not isinstance(project, dict):
        raise fail(E_MANIFEST, "project.json must contain an object")

    raw_tiles = project.get("tiles")
    if not isinstance(raw_tiles, list) or not raw_tiles:
        raise fail(E_MANIFEST, "project.tiles must be a non-empty list")
    tiles = sorted(raw_tiles, key=lambda tile: str(tile["tile_id"]))
    _validate_layout(project, tiles)
    tiles_by_id = {str(tile["tile_id"]): tile for tile in tiles}
    _validate_overlaps(project, tiles_by_id)

    image_by_id: Dict[str, np.ndarray] = {}
    for tile in tiles:
        image_path = _resolve(root, str(tile["image"]))
        image_by_id[str(tile["tile_id"])] = _load_image(image_path, tile)

    truth_relative = project.get("truth", "truth_offsets.json")
    truth = _read_json(_resolve(root, str(truth_relative)), E_MANIFEST)
    truth_by_id = {str(item["tile_id"]): item for item in truth.get("tiles", [])}
    if set(truth_by_id) != set(tiles_by_id):
        raise fail(
            E_MANIFEST,
            "truth offset tile ids do not match project tiles",
            truth_ids=sorted(truth_by_id),
            project_ids=sorted(tiles_by_id),
        )

    max_shift = float(project.get("max_shift_px", 32))
    offset_domain = max(64.0, max_shift * 2.0)
    for tile_id, truth_tile in truth_by_id.items():
        _validate_offset_pair(
            truth_tile.get("offset_xy"),
            E_OFFSET_FINITE,
            E_OFFSET_DOMAIN,
            offset_domain,
            f"{truth_relative}#/tiles/{tile_id}",
        )

    v1, v1_records = _load_v1(root, project, tiles_by_id, offset_domain)
    v2_manifest, v2_records = _load_v2(root, project, tiles_by_id, offset_domain)

    sample = LoadedSample(
        root=root,
        project=project,
        tiles=tiles,
        truth=truth,
        truth_by_id=truth_by_id,
        v1=v1,
        v2_manifest=v2_manifest,
        mask_records=v1_records + v2_records,
        version_id="",
        image_by_id=image_by_id,
    )
    sample.version_id = compute_version_id(sample)
    return sample
