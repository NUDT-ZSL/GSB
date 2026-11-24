from __future__ import annotations

import json
import math
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .canonical import canonical_json, digest_of_body, sha256_value, verify_digest, with_digest
from .errors import DigestError, SchemaError
from .math3d import is_finite_number
from .models import CHANNELS


@dataclass
class SnapshotRecord:
    canonical_clip_id: str
    logical_ms: int
    state_generation: int
    tombstone: bool
    bones: dict[str, list[float]]
    snapshot_generation: int
    source_version: int
    source_path: str
    location: str
    shard_id: str | None = None
    record_index: int | None = None

    def location_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "version": "v2" if self.source_version == 2 else "v1",
            "path": self.source_path,
            "location": self.location,
            "snapshot_generation": self.snapshot_generation,
            "state_generation": self.state_generation,
            "tombstone": self.tombstone,
        }
        if self.shard_id is not None:
            value["shard_id"] = self.shard_id
        if self.record_index is not None:
            value["record_index"] = self.record_index
        return value

    def public_record(self) -> dict[str, Any]:
        return {
            "canonical_clip_id": self.canonical_clip_id,
            "logical_ms": self.logical_ms,
            "state_generation": self.state_generation,
            "tombstone": self.tombstone,
            "bones": self.bones,
            "source": self.location_dict(),
        }


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def default_canonical_clip_id(clip_id: str) -> str:
    return "anim://clip/" + clip_id


def _validate_bones_flat(value: Any, location: str) -> dict[str, list[float]]:
    if not isinstance(value, dict):
        raise SchemaError("snapshot bones must be an object", location=location)
    result: dict[str, list[float]] = {}
    for key, item in value.items():
        if not isinstance(key, str) or "." not in key:
            raise SchemaError("snapshot bone channel key must be 'bone.channel'", location=location)
        bone, channel = key.rsplit(".", 1)
        if not bone or channel not in CHANNELS:
            raise SchemaError("snapshot bone channel is malformed", location=location)
        if not isinstance(item, list):
            raise SchemaError("snapshot channel value must be an array", location=location)
        expected = 4 if channel == "rotation" else 3
        if len(item) != expected:
            raise SchemaError("snapshot channel has the wrong component count", location=location)
        result[key] = [float(component) for component in item]
        if not all(is_finite_number(component) for component in item):
            raise SchemaError("snapshot channel value is not finite", location=location)
        if channel == "rotation":
            norm = math.sqrt(sum(component * component for component in result[key]))
            if norm <= 1e-12:
                raise SchemaError("snapshot rotation has zero norm", location=location)
    return result


def create_v1_snapshot(
    frames: Iterable[Mapping[str, Any]],
    *,
    canonical_clip_map: Mapping[str, str],
    clip_origins_ms: Mapping[str, int],
    output_path: str | Path,
    created_generation: int = 0,
) -> dict[str, Any]:
    materialized_frames = [dict(frame) for frame in frames]
    body = {
        "format": "anim-replay-snapshot-v1",
        "created_generation": created_generation,
        "canonical_clip_map": dict(sorted(canonical_clip_map.items())),
        "clip_origins_ms": dict(sorted(clip_origins_ms.items())),
        "frames": materialized_frames,
        "frame_count": len(materialized_frames),
    }
    document = with_digest(body)
    _atomic_json(Path(output_path), document)
    return document


def create_v2_snapshot(
    output_dir: str | Path,
    *,
    shard_documents: Sequence[Mapping[str, Any]],
    metadata_extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create sharded JSON plus a checksummed SQLite metadata database."""

    directory = Path(output_dir)
    shards_dir = directory / "shards"
    shards_dir.mkdir(parents=True, exist_ok=True)
    metadata_rows: list[dict[str, Any]] = []
    for index, shard in enumerate(shard_documents):
        shard_id = str(shard["shard_id"])
        path = shards_dir / f"{shard_id}.json"
        document = with_digest(dict(shard))
        _atomic_json(path, document)
        records = document["records"]
        metadata_rows.append(
            {
                "shard_id": shard_id,
                "path": str(path.relative_to(directory)).replace("\\", "/"),
                "snapshot_generation": int(document["snapshot_generation"]),
                "digest": document["content_digest"],
                "frame_count": int(document["frame_count"]),
                "start_ms": int(min((record["logical_ms"] for record in records), default=0)),
                "end_ms": int(max((record["logical_ms"] for record in records), default=0)),
                "kind": str(document.get("kind", "live")),
                "canonical_clip_id": str(document.get("canonical_clip_id", "")),
            }
        )

    meta_body = {
        "schema_version": 1,
        "extra": dict(metadata_extra or {}),
        "shards": sorted(metadata_rows, key=lambda item: item["shard_id"]),
    }
    meta_digest = sha256_value(meta_body)
    db_path = directory / "meta.sqlite"
    if db_path.exists():
        db_path.unlink()
    connection = sqlite3.connect(str(db_path))
    try:
        connection.execute(
            """
            CREATE TABLE shards(
                shard_id TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                snapshot_generation INTEGER NOT NULL,
                digest TEXT NOT NULL,
                frame_count INTEGER NOT NULL,
                start_ms INTEGER NOT NULL,
                end_ms INTEGER NOT NULL,
                kind TEXT NOT NULL,
                canonical_clip_id TEXT NOT NULL
            )
            """
        )
        connection.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.executemany(
            """
            INSERT INTO shards(
                shard_id, path, snapshot_generation, digest, frame_count,
                start_ms, end_ms, kind, canonical_clip_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row["shard_id"],
                    row["path"],
                    row["snapshot_generation"],
                    row["digest"],
                    row["frame_count"],
                    row["start_ms"],
                    row["end_ms"],
                    row["kind"],
                    row["canonical_clip_id"],
                )
                for row in meta_body["shards"]
            ],
        )
        connection.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?)",
            ("meta_digest", meta_digest),
        )
        connection.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?)",
            ("meta_body", canonical_json(meta_body)),
        )
        connection.commit()
    finally:
        connection.close()
    return {"meta_digest": meta_digest, "shards": metadata_rows}


def _issue(
    *,
    source: str,
    path: str,
    reason: str,
    details: Mapping[str, Any] | None = None,
    generation: int | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {"source": source, "path": path, "reason": reason}
    if generation is not None:
        value["snapshot_generation"] = generation
    if details:
        value["details"] = dict(details)
    return value


def load_v1_snapshot(
    path: str | Path,
    *,
    bucket_ms: int,
) -> tuple[list[SnapshotRecord], list[dict[str, Any]], dict[str, Any]]:
    file_path = Path(path)
    issues: list[dict[str, Any]] = []
    meta: dict[str, Any] = {"valid": False, "path": str(file_path), "generation": None}
    try:
        body = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        issues.append(_issue(source="v1", path=str(file_path), reason="unreadable", details={"error": str(exc)}))
        return [], issues, meta
    if not isinstance(body, dict) or body.get("format") != "anim-replay-snapshot-v1":
        issues.append(_issue(source="v1", path=str(file_path), reason="unsupported_format"))
        return [], issues, meta
    try:
        verify_digest(body, location=str(file_path))
    except DigestError as exc:
        issues.append(
            _issue(
                source="v1",
                path=str(file_path),
                reason="digest_mismatch",
                details=exc.details or {"message": exc.message},
            )
        )
        return [], issues, meta
    frames = body.get("frames")
    if not isinstance(frames, list):
        issues.append(_issue(source="v1", path=str(file_path), reason="frames_not_array"))
        return [], issues, meta
    declared = body.get("frame_count")
    if not isinstance(declared, int) or isinstance(declared, bool) or declared != len(frames):
        issues.append(
            _issue(
                source="v1",
                path=str(file_path),
                reason="frame_count_mismatch",
                details={"declared": declared, "actual": len(frames)},
            )
        )
        return [], issues, meta
    canonical_map = body.get("canonical_clip_map", {})
    if not isinstance(canonical_map, dict):
        issues.append(_issue(source="v1", path=str(file_path), reason="canonical_map_not_object"))
        return [], issues, meta
    origins = body.get("clip_origins_ms", {})
    if not isinstance(origins, dict):
        issues.append(_issue(source="v1", path=str(file_path), reason="origins_not_object"))
        return [], issues, meta

    records: list[SnapshotRecord] = []
    try:
        for index, frame in enumerate(frames):
            location = f"{file_path}#frames[{index}]"
            if not isinstance(frame, dict):
                raise SchemaError("v1 frame is not an object", location=location)
            clip = frame.get("clip")
            if not isinstance(clip, str) or not clip:
                raise SchemaError("v1 frame clip is missing", location=location)
            relative = frame.get("relative_s")
            if not is_finite_number(relative) or float(relative) < 0.0:
                raise SchemaError("v1 relative_s must be a finite non-negative number", location=location)
            origin = origins.get(clip, 0)
            if not isinstance(origin, int) or isinstance(origin, bool) or origin < 0:
                raise SchemaError("v1 clip origin must be a non-negative integer", location=location)
            logical_ms = origin + int(round(float(relative) * 1000.0))
            state_generation = frame.get("state_generation", body.get("created_generation", 0))
            if not isinstance(state_generation, int) or isinstance(state_generation, bool):
                raise SchemaError("v1 state generation must be an integer", location=location)
            bones = _validate_bones_flat(frame.get("bones", {}), location)
            records.append(
                SnapshotRecord(
                    canonical_clip_id=canonical_map.get(clip, default_canonical_clip_id(clip)),
                    logical_ms=logical_ms,
                    state_generation=state_generation,
                    tombstone=False,
                    bones=bones,
                    snapshot_generation=int(body.get("created_generation", 0)),
                    source_version=1,
                    source_path=str(file_path),
                    location=location,
                )
            )
    except SchemaError as exc:
        issues.append(
            _issue(
                source="v1",
                path=str(file_path),
                reason="malformed_record",
                details={"message": exc.message, "location": exc.location},
            )
        )
        return [], issues, meta
    meta["valid"] = True
    meta["generation"] = int(body.get("created_generation", 0))
    meta["record_count"] = len(records)
    return records, issues, meta


def _read_sqlite_metadata(
    directory: Path,
) -> tuple[list[dict[str, Any]], int, list[dict[str, Any]], dict[str, Any]]:
    db_path = directory / "meta.sqlite"
    issues: list[dict[str, Any]] = []
    meta: dict[str, Any] = {"valid": False, "path": str(db_path)}
    if not db_path.exists():
        issues.append(_issue(source="v2", path=str(db_path), reason="metadata_missing"))
        return [], -1, issues, meta
    connection = None
    try:
        connection = sqlite3.connect(str(db_path))
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        shard_rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT shard_id, path, snapshot_generation, digest, frame_count,
                       start_ms, end_ms, kind, canonical_clip_id
                FROM shards ORDER BY shard_id
                """
            )
        ]
        meta_rows = {row["key"]: row["value"] for row in connection.execute("SELECT key, value FROM meta")}
    except sqlite3.Error as exc:
        issues.append(
            _issue(source="v2", path=str(db_path), reason="metadata_unreadable", details={"error": str(exc)})
        )
        return [], -1, issues, meta
    finally:
        if connection is not None:
            connection.close()

    recorded = meta_rows.get("meta_digest")
    if not isinstance(recorded, str) or not recorded.startswith("sha256:"):
        issues.append(_issue(source="v2", path=str(db_path), reason="metadata_digest_missing"))
        return shard_rows, -1, issues, meta
    body_text = meta_rows.get("meta_body")
    try:
        body = json.loads(body_text) if body_text is not None else {"schema_version": 1, "shards": shard_rows}
    except json.JSONDecodeError:
        issues.append(_issue(source="v2", path=str(db_path), reason="metadata_body_invalid"))
        return shard_rows, -1, issues, meta
    actual = sha256_value(body)
    if actual != recorded:
        issues.append(
            _issue(
                source="v2",
                path=str(db_path),
                reason="metadata_digest_mismatch",
                details={"recorded": recorded, "actual": actual},
            )
        )
        return shard_rows, -1, issues, meta
    expected_rows = body.get("shards", [])
    if canonical_json(expected_rows) != canonical_json(shard_rows):
        issues.append(_issue(source="v2", path=str(db_path), reason="metadata_rows_mismatch"))
        return shard_rows, -1, issues, meta
    meta["valid"] = True
    return shard_rows, int(body.get("schema_version", 1)), issues, meta


def load_v2_snapshot(
    directory: str | Path,
    *,
    bucket_ms: int,
) -> tuple[list[SnapshotRecord], list[dict[str, Any]], dict[str, Any]]:
    root = Path(directory)
    shard_rows, schema_version, issues, metadata = _read_sqlite_metadata(root)
    meta: dict[str, Any] = {
        "valid": False,
        "path": str(root),
        "valid_generations": [],
        "invalid_generations": [],
        "fallback_generation": None,
    }
    if not metadata.get("valid"):
        return [], issues, meta
    if schema_version != 1:
        issues.append(
            _issue(source="v2", path=str(root / "meta.sqlite"), reason="unsupported_metadata_schema")
        )
        return [], issues, meta

    all_records: list[SnapshotRecord] = []
    generation_bad: dict[int, list[str]] = {}
    generation_records: dict[int, list[SnapshotRecord]] = {}
    root_resolved = root.resolve()
    for row in shard_rows:
        generation = int(row["snapshot_generation"])
        shard_path = (root / str(row["path"])).resolve()
        try:
            shard_path.relative_to(root_resolved)
        except ValueError:
            generation_bad.setdefault(generation, []).append("path_outside_snapshot")
            issues.append(
                _issue(
                    source="v2",
                    path=str(shard_path),
                    reason="path_outside_snapshot",
                    generation=generation,
                )
            )
            continue
        try:
            body = json.loads(shard_path.read_text(encoding="utf-8"))
            if not isinstance(body, dict) or body.get("format") != "anim-replay-snapshot-v2-shard":
                raise SchemaError("unsupported v2 shard format", location=str(shard_path))
            if not isinstance(body.get("content_digest"), str):
                raise DigestError("v2 shard content digest is missing", location=str(shard_path))
            verify_digest(body, expected=row["digest"], location=str(shard_path))
            records_raw = body.get("records")
            if not isinstance(records_raw, list):
                raise SchemaError("v2 shard records must be an array", location=str(shard_path))
            declared = body.get("frame_count")
            if (
                not isinstance(declared, int)
                or isinstance(declared, bool)
                or declared != len(records_raw)
                or int(row["frame_count"]) != len(records_raw)
            ):
                raise DigestError(
                    "declared frame count does not match shard records",
                    location=str(shard_path),
                    details={
                        "shard_declared": declared,
                        "metadata_declared": row["frame_count"],
                        "actual": len(records_raw),
                    },
                )
            shard_records: list[SnapshotRecord] = []
            for index, record in enumerate(records_raw):
                location = f"{shard_path}#records[{index}]"
                if not isinstance(record, dict):
                    raise SchemaError("v2 record is not an object", location=location)
                canonical_id = record.get("canonical_clip_id")
                if not isinstance(canonical_id, str) or not canonical_id:
                    raise SchemaError("v2 canonical clip id is missing", location=location)
                logical_ms = record.get("logical_ms")
                if not isinstance(logical_ms, int) or isinstance(logical_ms, bool) or logical_ms < 0:
                    raise SchemaError("v2 logical_ms must be a non-negative integer", location=location)
                state_generation = record.get("state_generation", 0)
                if not isinstance(state_generation, int) or isinstance(state_generation, bool):
                    raise SchemaError("v2 state generation must be an integer", location=location)
                tombstone = record.get("tombstone", False)
                if not isinstance(tombstone, bool):
                    raise SchemaError("v2 tombstone must be boolean", location=location)
                bones = {} if tombstone else _validate_bones_flat(record.get("bones", {}), location)
                shard_records.append(
                    SnapshotRecord(
                        canonical_clip_id=canonical_id,
                        logical_ms=logical_ms,
                        state_generation=state_generation,
                        tombstone=tombstone,
                        bones=bones,
                        snapshot_generation=generation,
                        source_version=2,
                        source_path=str(shard_path),
                        location=location,
                        shard_id=str(row["shard_id"]),
                        record_index=index,
                    )
                )
            generation_records.setdefault(generation, []).extend(shard_records)
        except (OSError, json.JSONDecodeError, DigestError, SchemaError) as exc:
            generation_bad.setdefault(generation, []).append(shard_path.name)
            if isinstance(exc, DigestError):
                reason = "shard_digest_or_count_mismatch"
                details = exc.details or {"message": exc.message}
            elif isinstance(exc, SchemaError):
                reason = "shard_schema"
                details = {"message": exc.message, "location": exc.location}
            else:
                reason = "shard_unreadable"
                details = {"error": str(exc)}
            issues.append(
                _issue(
                    source="v2",
                    path=str(shard_path),
                    reason=reason,
                    generation=generation,
                    details=details,
                )
            )

    valid_generations = sorted(set(generation_records) - set(generation_bad))
    invalid_generations = sorted(set(generation_bad))
    for generation in valid_generations:
        all_records.extend(generation_records[generation])
    meta["valid"] = bool(valid_generations) or bool(shard_rows)
    meta["valid_generations"] = valid_generations
    meta["invalid_generations"] = invalid_generations
    meta["fallback_generation"] = max(valid_generations) if invalid_generations and valid_generations else None
    meta["record_count"] = len(all_records)
    return all_records, issues, meta


def _candidate_rank(record: SnapshotRecord) -> tuple[int, int, int, int, int, str, int]:
    return (
        record.state_generation,
        1 if record.tombstone else 0,
        record.logical_ms,
        record.source_version,
        record.snapshot_generation,
        record.source_path,
        record.record_index if record.record_index is not None else -1,
    )


def _difference_channels(winner: SnapshotRecord, loser: SnapshotRecord) -> list[str]:
    keys = sorted(set(winner.bones) | set(loser.bones))
    return [
        key
        for key in keys
        if winner.bones.get(key) != loser.bones.get(key)
    ]


def merge_snapshots(
    *,
    v1_path: str | Path | None,
    v2_dir: str | Path | None,
    bucket_ms: int = 16,
) -> dict[str, Any]:
    if bucket_ms <= 0:
        raise SchemaError("snapshot merge bucket must be positive", location="bucket_ms")
    records: list[SnapshotRecord] = []
    issues: list[dict[str, Any]] = []
    snapshots: dict[str, Any] = {}
    if v1_path is not None:
        v1_records, v1_issues, v1_meta = load_v1_snapshot(v1_path, bucket_ms=bucket_ms)
        records.extend(v1_records)
        issues.extend(v1_issues)
        snapshots["v1"] = v1_meta
    if v2_dir is not None:
        v2_records, v2_issues, v2_meta = load_v2_snapshot(v2_dir, bucket_ms=bucket_ms)
        records.extend(v2_records)
        issues.extend(v2_issues)
        snapshots["v2"] = v2_meta

    groups: dict[tuple[str, int], list[SnapshotRecord]] = {}
    for record in records:
        key = (record.canonical_clip_id, record.logical_ms // bucket_ms)
        groups.setdefault(key, []).append(record)

    merged: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for key in sorted(groups):
        candidates = sorted(groups[key], key=_candidate_rank, reverse=True)
        winner = candidates[0]
        merged.append(winner.public_record())
        for loser in candidates[1:]:
            different = _difference_channels(winner, loser)
            if not different and winner.tombstone == loser.tombstone:
                continue
            if winner.tombstone or loser.tombstone:
                different = different or sorted(set(winner.bones) | set(loser.bones)) or ["*"]
            resolution_parts: list[str] = []
            if winner.state_generation != loser.state_generation:
                resolution_parts.append("state_generation")
            elif winner.tombstone != loser.tombstone:
                resolution_parts.append("tombstone")
            elif winner.logical_ms != loser.logical_ms:
                resolution_parts.append("logical_ms")
            elif winner.snapshot_generation != loser.snapshot_generation:
                resolution_parts.append("snapshot_generation")
            else:
                resolution_parts.append("source_version")
            channel_details = [
                {
                    "bone_channel": channel,
                    "winner_value": None if winner.tombstone else winner.bones.get(channel),
                    "loser_value": None if loser.tombstone else loser.bones.get(channel),
                }
                for channel in different
            ]
            conflicts.append(
                {
                    "canonical_clip_id": winner.canonical_clip_id,
                    "bucket": key[1],
                    "logical_ms": winner.logical_ms,
                    "bone_channel": different[0] if different else "*",
                    "differing_channel_count": len(different),
                    "differing_channels": channel_details,
                    "winner": winner.location_dict(),
                    "loser": loser.location_dict(),
                    "resolution": "+".join(resolution_parts),
                }
            )

    records_sorted = sorted(
        merged,
        key=lambda item: (item["canonical_clip_id"], item["logical_ms"], item["state_generation"]),
    )
    result: dict[str, Any] = {
        "format": "anim-replay-merged-snapshot-v1",
        "bucket_ms": bucket_ms,
        "ok": not issues,
        "record_count": len(records_sorted),
        "records": records_sorted,
        "conflicts": conflicts,
        "snapshots": snapshots,
        "digest_issues": issues,
    }
    if issues:
        fallback_generation = None
        if snapshots.get("v2", {}).get("fallback_generation") is not None:
            fallback_generation = snapshots["v2"]["fallback_generation"]
        elif snapshots.get("v2", {}).get("valid_generations"):
            fallback_generation = max(snapshots["v2"]["valid_generations"])
        result["error"] = {
            "code": "E_DIGEST",
            "message": "one or more snapshots failed content or frame-count verification",
            "issue_count": len(issues),
            "issues": issues,
        }
        result["fallback"] = {
            "used": True,
            "generation": fallback_generation,
            "v1_valid": bool(snapshots.get("v1", {}).get("valid")),
            "v2_valid_generations": snapshots.get("v2", {}).get("valid_generations", []),
        }
    return result


def write_merge_outputs(result: Mapping[str, Any], output_dir: str | Path) -> None:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    _atomic_json(directory / "merged_snapshot.json", result)
    _atomic_json(
        directory / "merge_report.json",
        {
            "ok": result.get("ok"),
            "error": result.get("error"),
            "fallback": result.get("fallback"),
            "record_count": result.get("record_count"),
            "conflict_count": len(result.get("conflicts", [])),
            "conflicts": result.get("conflicts", []),
            "digest_issues": result.get("digest_issues", []),
        },
    )

