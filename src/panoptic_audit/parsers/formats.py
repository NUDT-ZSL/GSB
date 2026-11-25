from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from ..errors import AuditError, ErrorCode
from ..groundtruth import (
    IDENTITY_BASE,
    IGNORE_CATEGORY,
    IGNORE_IDENTITY,
    ParsedTruth,
    ensure_version,
    require_fields,
    validate_label_arrays,
)
from ..hashing import canonical_checksum


def load_json(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError as exc:
        raise AuditError(ErrorCode.FORMAT_CORRUPT, "ground truth file is missing", {"path": str(path)}) from exc
    except json.JSONDecodeError as exc:
        raise AuditError(
            ErrorCode.FORMAT_CORRUPT,
            "ground truth JSON is corrupt",
            {"path": str(path), "line": exc.lineno, "column": exc.colno},
        ) from exc
    if not isinstance(payload, dict):
        raise AuditError(ErrorCode.FORMAT_CORRUPT, "ground truth payload must be a JSON object", {"path": str(path)})
    return payload


def _check_declared_checksum(payload: dict[str, Any], body: dict[str, Any], context: dict[str, Any]) -> None:
    declared = payload.get("checksum")
    if not isinstance(declared, str):
        raise AuditError(ErrorCode.FORMAT_CORRUPT, "block checksum is missing", context)
    actual = canonical_checksum(body)
    if declared != actual:
        raise AuditError(
            ErrorCode.FORMAT_CORRUPT,
            "block checksum mismatch",
            {**context, "declared": declared, "actual": actual},
        )


def parse_v1(path: str | Path, entities: set[tuple[int, int]] | None = None) -> ParsedTruth:
    path = Path(path)
    payload = load_json(path)
    require_fields(payload, ["format", "version", "height", "width", "rows"], "v1")
    ensure_version(payload["version"], {"v1", "1.0", "v1-row-identity"}, "v1")
    if payload["format"] != "v1_row_identity":
        raise AuditError(ErrorCode.FORMAT_UNSUPPORTED, "unsupported v1 format marker", {"format": payload["format"]})

    height = int(payload["height"])
    width = int(payload["width"])
    rows = payload["rows"]
    if not isinstance(rows, list) or len(rows) != height:
        raise AuditError(ErrorCode.FORMAT_CORRUPT, "v1 row count mismatch", {"expected": height, "actual": len(rows) if isinstance(rows, list) else None})

    category = np.zeros((height, width), dtype=np.int64)
    instance = np.zeros((height, width), dtype=np.int64)
    ignore = np.zeros((height, width), dtype=bool)
    confidence = np.full((height, width), 0.80, dtype=np.float64)
    weights = np.full((height, width), float(payload.get("reliability_weight", 0.80)), dtype=np.float64)
    raw_by_pixel: dict[str, dict[str, Any]] = {}
    blocks: list[dict[str, str]] = []

    for row_index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise AuditError(ErrorCode.FORMAT_CORRUPT, "v1 row must be an object", {"row": row_index})
        require_fields(row, ["y", "identities", "checksum"], "v1-row")
        y = int(row["y"])
        identities = row["identities"]
        if y != row_index:
            raise AuditError(ErrorCode.FORMAT_CORRUPT, "v1 row order is invalid", {"expected_y": row_index, "actual_y": y})
        if not isinstance(identities, list) or len(identities) != width:
            raise AuditError(ErrorCode.FORMAT_CORRUPT, "v1 row width mismatch", {"y": y, "expected": width})
        _check_declared_checksum(row, {"y": y, "identities": identities}, {"format": "v1", "block_id": f"v1:row:{y}"})
        blocks.append({"block_id": f"v1:row:{y}", "checksum": str(row["checksum"])})

        for x, raw_identity in enumerate(identities):
            if not isinstance(raw_identity, int) or raw_identity < 0:
                raise AuditError(ErrorCode.FORMAT_CORRUPT, "v1 identity must be a non-negative integer", {"x": x, "y": y})
            identity = int(raw_identity)
            if identity == IGNORE_IDENTITY:
                cat, inst, is_ignore = IGNORE_CATEGORY, 0, True
            else:
                cat, inst = divmod(identity, IDENTITY_BASE)
                is_ignore = cat == IGNORE_CATEGORY
                if is_ignore:
                    inst = 0
            if cat > IGNORE_CATEGORY or inst >= IDENTITY_BASE:
                raise AuditError(ErrorCode.FORMAT_CORRUPT, "v1 entity number is invalid", {"x": x, "y": y, "identity": identity})
            category[y, x] = cat
            instance[y, x] = inst
            ignore[y, x] = is_ignore
            raw_by_pixel[f"{x},{y}"] = {
                "format": "v1",
                "identity": identity,
                "x": x,
                "y": y,
                "block_id": f"v1:row:{y}",
                "block_checksum": row["checksum"],
            }

    validate_label_arrays(category, instance, ignore, confidence, height, width, "v1", entities)
    return ParsedTruth("v1", str(payload["version"]), height, width, category, instance, ignore, confidence, weights, blocks, raw_by_pixel, [str(path)])


def _open_metadata(path: Path) -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.execute("PRAGMA query_only=ON")
        return conn
    except sqlite3.Error as exc:
        raise AuditError(ErrorCode.FORMAT_CORRUPT, "v2 SQLite metadata cannot be opened", {"path": str(path)}) from exc


def parse_v2(directory: str | Path, metadata_path: str | Path | None = None) -> ParsedTruth:
    directory = Path(directory)
    metadata_path = Path(metadata_path) if metadata_path else directory / "gt_v2_meta.sqlite"
    if not metadata_path.exists():
        raise AuditError(ErrorCode.FORMAT_CORRUPT, "v2 metadata database is missing", {"path": str(metadata_path)})

    conn = _open_metadata(metadata_path)
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        required_tables = {"format_meta", "shards", "blocks", "entities"}
        missing = sorted(required_tables - tables)
        if missing:
            raise AuditError(ErrorCode.FORMAT_CORRUPT, "v2 metadata tables are missing", {"missing": missing})

        meta = {key: value for key, value in conn.execute("SELECT key, value FROM format_meta")}
        if meta.get("format") != "v2_category_instance_confidence":
            raise AuditError(ErrorCode.FORMAT_UNSUPPORTED, "unsupported v2 format marker", {"format": meta.get("format")})
        ensure_version(meta.get("version"), {"v2", "2.0", "v2-shard"}, "v2")
        height = int(meta["height"])
        width = int(meta["width"])
        shard_rows = conn.execute(
            "SELECT shard_id, path, row_start, row_end, checksum FROM shards ORDER BY shard_id"
        ).fetchall()
        expected_start = 0
        for shard_id, _path, row_start, row_end, _checksum in shard_rows:
            if int(shard_id) < 0 or int(row_start) != expected_start or int(row_end) <= int(row_start):
                raise AuditError(
                    ErrorCode.FORMAT_CORRUPT,
                    "v2 shard ranges are not contiguous from row zero",
                    {"shard_id": shard_id, "row_start": row_start, "row_end": row_end, "expected_start": expected_start},
                )
            expected_start = int(row_end)
        if expected_start != height:
            raise AuditError(
                ErrorCode.FORMAT_CORRUPT,
                "v2 shard rows do not cover configured image height",
                {"covered_end": expected_start, "height": height},
            )
        block_rows = conn.execute(
            "SELECT block_id, shard_id, y, checksum FROM blocks ORDER BY y"
        ).fetchall()
        entity_rows = conn.execute("SELECT category_id, instance_id FROM entities").fetchall()
    except sqlite3.Error as exc:
        raise AuditError(ErrorCode.FORMAT_CORRUPT, "v2 metadata query failed", {"error": str(exc)}) from exc
    except KeyError as exc:
        raise AuditError(ErrorCode.FORMAT_CORRUPT, "v2 metadata key is missing", {"key": str(exc)}) from exc
    finally:
        conn.close()

    entities = {(int(cat), int(inst)) for cat, inst in entity_rows}
    metadata_blocks = {block_id: checksum for block_id, _sid, _y, checksum in block_rows}

    category = np.zeros((height, width), dtype=np.int64)
    instance = np.zeros((height, width), dtype=np.int64)
    ignore = np.zeros((height, width), dtype=bool)
    confidence = np.zeros((height, width), dtype=np.float64)
    raw_by_pixel: dict[str, dict[str, Any]] = {}
    blocks: list[dict[str, str]] = []
    files = [str(metadata_path)]

    if not shard_rows:
        raise AuditError(ErrorCode.FORMAT_CORRUPT, "v2 metadata has no shards")

    for shard_id, rel_path, row_start, row_end, declared_shard_checksum in shard_rows:
        shard_path = directory / str(rel_path)
        payload = load_json(shard_path)
        files.append(str(shard_path))
        require_fields(payload, ["format", "version", "shard_id", "rows", "checksum"], "v2-shard")
        ensure_version(payload["version"], {"v2", "2.0", "v2-shard"}, "v2")
        if int(payload["shard_id"]) != int(shard_id):
            raise AuditError(ErrorCode.FORMAT_CORRUPT, "v2 shard id mismatch", {"metadata": shard_id, "payload": payload["shard_id"]})

        shard_body = {key: value for key, value in payload.items() if key != "checksum"}
        actual_shard_checksum = canonical_checksum(shard_body)
        if actual_shard_checksum != declared_shard_checksum or actual_shard_checksum != payload["checksum"]:
            raise AuditError(
                ErrorCode.FORMAT_CORRUPT,
                "v2 shard checksum mismatch",
                {"shard_id": shard_id, "declared": payload["checksum"], "metadata": declared_shard_checksum, "actual": actual_shard_checksum},
            )
        blocks.append({"block_id": f"v2:shard:{shard_id}", "checksum": actual_shard_checksum})

        rows = payload["rows"]
        if not isinstance(rows, list) or len(rows) != int(row_end) - int(row_start):
            raise AuditError(ErrorCode.FORMAT_CORRUPT, "v2 shard row count mismatch", {"shard_id": shard_id})

        for offset, row in enumerate(rows):
            y = int(row_start) + offset
            require_fields(row, ["y", "category", "instance", "confidence", "checksum"], "v2-block")
            if int(row["y"]) != y:
                raise AuditError(ErrorCode.FORMAT_CORRUPT, "v2 block y coordinate mismatch", {"expected": y, "actual": row["y"]})
            cat_row = row["category"]
            inst_row = row["instance"]
            conf_row = row["confidence"]
            if not all(isinstance(value, list) and len(value) == width for value in (cat_row, inst_row, conf_row)):
                raise AuditError(ErrorCode.FORMAT_CORRUPT, "v2 row channel width mismatch", {"y": y})

            body = {"y": y, "category": cat_row, "instance": inst_row, "confidence": conf_row}
            _check_declared_checksum(row, body, {"format": "v2", "block_id": f"v2:row:{y}"})
            block_id = f"v2:row:{y}"
            if metadata_blocks.get(block_id) != row["checksum"]:
                raise AuditError(ErrorCode.FORMAT_CORRUPT, "v2 metadata block checksum mismatch", {"block_id": block_id})
            blocks.append({"block_id": block_id, "checksum": str(row["checksum"])})

            category[y, :] = np.asarray(cat_row, dtype=np.int64)
            instance[y, :] = np.asarray(inst_row, dtype=np.int64)
            confidence[y, :] = np.asarray(conf_row, dtype=np.float64)
            for x in range(width):
                cat = int(cat_row[x])
                is_ignore = cat == IGNORE_CATEGORY
                ignore[y, x] = is_ignore
                raw_by_pixel[f"{x},{y}"] = {
                    "format": "v2",
                    "x": x,
                    "y": y,
                    "category": cat,
                    "instance": int(inst_row[x]),
                    "confidence": float(conf_row[x]),
                    "shard_id": int(shard_id),
                    "block_id": block_id,
                    "block_checksum": row["checksum"],
                }

    validate_label_arrays(category, instance, ignore, confidence, height, width, "v2", entities)
    return ParsedTruth("v2", str(meta["version"]), height, width, category, instance, ignore, confidence, confidence.copy(), blocks, raw_by_pixel, files)


def _decode_rle_channel(channel: str, payload: dict[str, Any], height: int, width: int) -> tuple[np.ndarray, dict[int, int], list[dict[str, Any]]]:
    require_fields(payload, ["runs", "checksum"], f"rle-{channel}")
    runs = payload["runs"]
    if not isinstance(runs, list) or not runs:
        raise AuditError(ErrorCode.FORMAT_CORRUPT, f"rle {channel} runs must be a non-empty list")

    values: list[int] = []
    run_index_by_position: dict[int, int] = {}
    raw_runs: list[dict[str, Any]] = []
    total = 0
    for index, run in enumerate(runs):
        if not isinstance(run, list) or len(run) != 2:
            raise AuditError(ErrorCode.FORMAT_CORRUPT, f"rle {channel} run must be [value, count]", {"run_index": index})
        value, count = run
        if not isinstance(value, int) or not isinstance(count, int) or count <= 0:
            raise AuditError(ErrorCode.FORMAT_CORRUPT, f"rle {channel} run value/count invalid", {"run_index": index})
        if channel == "ignore" and value not in (0, 1):
            raise AuditError(ErrorCode.FORMAT_CORRUPT, "rle ignore channel must be binary", {"value": value})
        if channel != "ignore" and not 0 <= value <= max(IGNORE_CATEGORY, IDENTITY_BASE - 1):
            raise AuditError(ErrorCode.FORMAT_CORRUPT, f"rle {channel} value out of range", {"value": value})
        for offset in range(count):
            run_index_by_position[total + offset] = index
        values.extend([value] * count)
        raw_runs.append({"run_index": index, "value": value, "count": count, "start_position": total})
        total += count

    if total != height * width:
        raise AuditError(
            ErrorCode.FORMAT_CORRUPT,
            f"rle {channel} total run length mismatch",
            {"expected": height * width, "actual": total},
        )

    array = np.asarray(values, dtype=np.int64).reshape((height, width), order="F")
    return array, run_index_by_position, raw_runs


def parse_rle(path: str | Path, entities: set[tuple[int, int]] | None = None) -> ParsedTruth:
    path = Path(path)
    payload = load_json(path)
    require_fields(payload, ["format", "version", "height", "width", "order", "channels"], "rle")
    ensure_version(payload["version"], {"rle-v1", "v3", "coco-rle-1.0"}, "rle")
    if payload["format"] != "coco_rle_panoptic":
        raise AuditError(ErrorCode.FORMAT_UNSUPPORTED, "unsupported RLE format marker", {"format": payload["format"]})
    if payload["order"] != "F":
        raise AuditError(ErrorCode.FORMAT_UNSUPPORTED, "only COCO/Fortran-order RLE is supported", {"order": payload["order"]})

    height, width = int(payload["height"]), int(payload["width"])
    channels = payload["channels"]
    require_fields(channels, ["category", "instance", "ignore"], "rle-channels")

    category, cat_pos_to_run, cat_runs = _decode_rle_channel("category", channels["category"], height, width)
    instance, inst_pos_to_run, inst_runs = _decode_rle_channel("instance", channels["instance"], height, width)
    ignore_values, ignore_pos_to_run, ignore_runs = _decode_rle_channel("ignore", channels["ignore"], height, width)
    ignore = ignore_values.astype(bool)

    blocks: list[dict[str, str]] = []
    for name in ("category", "instance", "ignore"):
        channel_payload = channels[name]
        declared = channel_payload.get("checksum")
        body = {"runs": channel_payload.get("runs")}
        actual = canonical_checksum(body)
        if declared != actual:
            raise AuditError(ErrorCode.FORMAT_CORRUPT, "rle block checksum mismatch", {"block_id": f"rle:{name}", "declared": declared, "actual": actual})
        blocks.append({"block_id": f"rle:{name}", "checksum": str(declared)})

    raw_by_pixel: dict[str, dict[str, Any]] = {}
    for y in range(height):
        for x in range(width):
            position = x * height + y  # Fortran-order flattened position
            raw_by_pixel[f"{x},{y}"] = {
                "format": "RLE",
                "x": x,
                "y": y,
                "fortran_position": position,
                "category": int(category[y, x]),
                "instance": int(instance[y, x]),
                "ignore": bool(ignore[y, x]),
                "category_run": cat_runs[cat_pos_to_run[position]],
                "instance_run": inst_runs[inst_pos_to_run[position]],
                "ignore_run": ignore_runs[ignore_pos_to_run[position]],
                "block_ids": ["rle:category", "rle:instance", "rle:ignore"],
            }

    confidence = np.full((height, width), 0.90, dtype=np.float64)
    weights = np.full((height, width), float(payload.get("reliability_weight", 0.90)), dtype=np.float64)
    validate_label_arrays(category, instance, ignore, confidence, height, width, "RLE", entities)
    return ParsedTruth("RLE", str(payload["version"]), height, width, category, instance, ignore, confidence, weights, blocks, raw_by_pixel, [str(path)])


def iter_source_checksums(truths: Iterable[ParsedTruth]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for truth in truths:
        for block in truth.blocks:
            result.append({"format": truth.format, **block})
    return result
