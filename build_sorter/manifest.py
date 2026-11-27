"""Atomic JSON manifest handling and segment integrity checks."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .codec import HEADER_SIZE, scan_segment, segment_path
from .errors import SegmentIntegrityError, SorterError

MANIFEST_FILENAME = "sort_manifest.json"
MANIFEST_VERSION = 1

_SEGMENT_FIELDS = {
    "segment_id",
    "name",
    "record_count",
    "size_bytes",
    "first_trigger_ts",
    "last_trigger_ts",
    "min_input_seq",
    "max_input_seq",
    "first_record_bytes",
    "max_record_bytes",
    "sha256",
    "consumed_records",
    "consumed_offset",
    "deleted",
}


def manifest_path(work_dir: Path) -> Path:
    return work_dir / MANIFEST_FILENAME


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(
                value,
                file,
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
                sort_keys=True,
            )
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def save_manifest(work_dir: Path, manifest: dict[str, Any]) -> None:
    atomic_write_json(manifest_path(work_dir), manifest)


def load_manifest(work_dir: Path) -> dict[str, Any]:
    path = manifest_path(work_dir)
    if not path.is_file():
        raise SorterError(
            "MANIFEST_NOT_FOUND",
            f"sort manifest not found: {path}",
            details={"path": str(path)},
        )
    try:
        with open(path, encoding="utf-8") as file:
            manifest = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        raise SegmentIntegrityError(
            "unable to parse the sort manifest",
            path=str(path),
            error=str(exc),
        ) from exc
    if not isinstance(manifest, dict) or manifest.get("version") != MANIFEST_VERSION:
        raise SegmentIntegrityError(
            "unsupported or malformed sort manifest",
            path=str(path),
        )
    if not isinstance(manifest.get("segments"), list):
        raise SegmentIntegrityError("manifest segments must be a list")
    return manifest


def validate_segment_entries(
    work_dir: Path,
    segments: list[dict[str, Any]],
    *,
    verify_sha256: bool = True,
    allow_deleted: bool = False,
) -> None:
    seen_ids: set[int] = set()
    seen_names: set[str] = set()
    for entry in segments:
        if not isinstance(entry, dict) or not _SEGMENT_FIELDS.issubset(entry):
            raise SegmentIntegrityError("manifest segment entry has missing fields")
        segment_id = entry["segment_id"]
        name = entry["name"]
        if not isinstance(segment_id, int) or isinstance(segment_id, bool) or segment_id < 0:
            raise SegmentIntegrityError("manifest contains an invalid segment id")
        if not isinstance(name, str) or Path(name).name != name or not name.endswith(".seg"):
            raise SegmentIntegrityError(
                "manifest contains an unsafe segment file name",
                segment_id=segment_id,
                name=name,
            )
        if segment_id in seen_ids or name in seen_names:
            raise SegmentIntegrityError("manifest contains duplicate segments")
        seen_ids.add(segment_id)
        seen_names.add(name)
        deleted = bool(entry.get("deleted", False))
        path = segment_path(work_dir, name)
        if deleted and allow_deleted:
            if (
                entry.get("consumed_records") != entry["record_count"]
                or entry.get("consumed_offset") != entry["size_bytes"]
            ):
                raise SegmentIntegrityError(
                    "deleted segment checkpoint does not cover the whole segment",
                    segment_id=segment_id,
                    name=name,
                )
            if path.exists():
                actual = scan_segment(path)
                if (
                    actual["record_count"] != entry["record_count"]
                    or actual["size_bytes"] != entry["size_bytes"]
                    or actual["sha256"] != entry["sha256"]
                ):
                    raise SegmentIntegrityError(
                        "deleted segment file does not match the manifest",
                        segment_id=segment_id,
                        name=name,
                    )
            continue
        if not path.is_file():
            raise SegmentIntegrityError(
                "segment file referenced by the manifest is missing",
                segment_id=segment_id,
                name=name,
            )
        actual_size = path.stat().st_size
        if actual_size != entry["size_bytes"]:
            raise SegmentIntegrityError(
                "segment file size differs from the manifest",
                segment_id=segment_id,
                expected_bytes=entry["size_bytes"],
                actual_bytes=actual_size,
            )
        if verify_sha256:
            actual = scan_segment(path)
            for field in (
                "record_count",
                "size_bytes",
                "first_trigger_ts",
                "last_trigger_ts",
                "min_input_seq",
                "max_input_seq",
                "first_record_bytes",
                "max_record_bytes",
                "sha256",
            ):
                if actual[field] != entry[field]:
                    raise SegmentIntegrityError(
                        "segment metadata differs from the manifest",
                        segment_id=segment_id,
                        field=field,
                        expected=entry[field],
                        actual=actual[field],
                    )
        consumed_records = entry.get("consumed_records", 0)
        consumed_offset = entry.get("consumed_offset", HEADER_SIZE)
        if (
            not isinstance(consumed_records, int)
            or not isinstance(consumed_offset, int)
            or consumed_records < 0
            or consumed_records > entry["record_count"]
            or consumed_offset < HEADER_SIZE
            or consumed_offset > entry["size_bytes"]
        ):
            raise SegmentIntegrityError(
                "segment checkpoint metadata is out of range",
                segment_id=segment_id,
            )


def cleanup_temp_files(work_dir: Path) -> None:
    """Remove all files owned by the sorter, but preserve the report."""

    if not work_dir.exists():
        return
    for path in work_dir.iterdir():
        if path.is_dir():
            continue
        if path.name == MANIFEST_FILENAME or path.name.endswith(".seg"):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        elif path.name.startswith(f".{MANIFEST_FILENAME}.") and path.name.endswith(".tmp"):
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def cleanup_unreferenced_segments(
    work_dir: Path,
    manifest: dict[str, Any],
) -> None:
    referenced = {entry["name"] for entry in manifest.get("segments", [])}
    for path in work_dir.glob("*.seg"):
        if path.name not in referenced:
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def delete_segment_files(
    work_dir: Path,
    entries: list[dict[str, Any]],
) -> None:
    for entry in entries:
        try:
            segment_path(work_dir, entry["name"]).unlink()
        except FileNotFoundError:
            pass


def temp_file_listing(work_dir: Path) -> list[dict[str, Any]]:
    if not work_dir.exists():
        return []
    result = []
    for path in sorted(work_dir.iterdir(), key=lambda item: item.name):
        if path.is_file() and (path.name.endswith(".seg") or path.name == MANIFEST_FILENAME):
            result.append({"name": path.name, "size_bytes": path.stat().st_size})
    return result
