"""Input parsing and normalization for regions and JSONL traces."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from .codes import (
    E_ACCESS_KIND,
    E_ACCESS_RANGE_OVERFLOW,
    E_ACCESS_SEQ,
    E_ACCESS_SIZE,
    E_ARENA_SIZE,
    E_ARENA_SIZE_MISMATCH,
    E_INPUT_EMPTY,
    E_INPUT_JSON,
    E_INPUT_PATH,
    E_INPUT_READ,
    E_OFFSET,
    E_REGION_ID_DUPLICATE,
    E_REGION_SCHEMA,
    E_REGION_UNKNOWN,
    E_TIMESTAMP,
    E_TRACE_ID_DUPLICATE,
    E_TRACE_SCHEMA,
    LEDGER_SCHEMA_VERSION,
    POLICY_VERSION,
)
from .models import (
    Manifest,
    Region,
    Rejection,
    TraceRecord,
    TraceRef,
    nullable_interval,
)
from .util import canonical_digest, file_sha256

UINT64_MAX = (1 << 64) - 1
TRACE_KINDS = frozenset({"read", "write"})


def _is_int(value: Any) -> bool:
    return type(value) is int


def _reject(
    path: str,
    line_number: Optional[int],
    code: str,
    message: str,
    detail: Optional[Mapping[str, Any]] = None,
) -> Rejection:
    return Rejection(path, line_number, code, message, dict(detail or {}))


def _extract_region_items(path: Path, text: str) -> tuple[list[tuple[Any, int]], Optional[Rejection]]:
    if not text.strip():
        return [], _reject(str(path), None, E_INPUT_EMPTY, "region declaration file is empty")

    if path.suffix.lower() in {".jsonl", ".ndjson"}:
        items: list[tuple[Any, int]] = []
        for line_number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                items.append((json.loads(line), line_number))
            except (ValueError, TypeError) as exc:
                return [], _reject(
                    str(path),
                    line_number,
                    E_INPUT_JSON,
                    "invalid region JSON line",
                    {"error": str(exc)},
                )
        return items, None

    try:
        document = json.loads(text)
    except (ValueError, TypeError) as exc:
        return [], _reject(str(path), 1, E_INPUT_JSON, "invalid region JSON", {"error": str(exc)})

    if isinstance(document, dict):
        if "regions" not in document:
            return [], _reject(
                str(path),
                1,
                E_REGION_SCHEMA,
                "region document must contain a regions array",
            )
        document = document["regions"]
    if not isinstance(document, list):
        return [], _reject(
            str(path),
            1,
            E_REGION_SCHEMA,
            "region declarations must be an array",
        )
    return [(item, 1) for item in document], None


def parse_region_object(
    value: Any, source_file: str, line_number: int
) -> tuple[Optional[Region], Optional[Rejection]]:
    if not isinstance(value, dict):
        return None, _reject(
            source_file, line_number, E_REGION_SCHEMA, "region declaration must be an object"
        )

    required = (
        "region_id",
        "arena_id",
        "arena_size",
        "start_offset",
        "end_offset",
        "lifetime_start_ms",
        "lifetime_end_ms",
        "generation",
    )
    missing = [key for key in required if key not in value]
    if missing:
        return None, _reject(
            source_file,
            line_number,
            E_REGION_SCHEMA,
            "region declaration is missing required fields",
            {"missing": missing},
        )

    region_id = value["region_id"]
    arena_id = value["arena_id"]
    parent = value.get("parent_region_id")
    if not isinstance(region_id, str) or not region_id:
        return None, _reject(
            source_file, line_number, E_REGION_SCHEMA, "region_id must be a non-empty string"
        )
    if not isinstance(arena_id, str) or not arena_id:
        return None, _reject(
            source_file, line_number, E_REGION_SCHEMA, "arena_id must be a non-empty string"
        )
    if parent is not None and (not isinstance(parent, str) or not parent):
        return None, _reject(
            source_file,
            line_number,
            E_REGION_SCHEMA,
            "parent_region_id must be null or a non-empty string",
        )

    numeric = (
        "arena_size",
        "start_offset",
        "end_offset",
        "lifetime_start_ms",
        "lifetime_end_ms",
        "generation",
    )
    for field in numeric:
        if not _is_int(value[field]):
            return None, _reject(
                source_file,
                line_number,
                E_REGION_SCHEMA,
                "%s must be an integer" % field,
                {"field": field, "value": value[field]},
            )

    arena_size = value["arena_size"]
    start = value["start_offset"]
    end = value["end_offset"]
    lifetime_start = value["lifetime_start_ms"]
    lifetime_end = value["lifetime_end_ms"]
    generation = value["generation"]

    if arena_size <= 0 or arena_size > UINT64_MAX:
        return None, _reject(
            source_file,
            line_number,
            E_ARENA_SIZE,
            "arena_size must be a positive 64-bit integer",
            {"arena_size": arena_size},
        )
    if start < 0 or end <= start or end > arena_size:
        return None, _reject(
            source_file,
            line_number,
            E_REGION_SCHEMA,
            "region interval must be a non-empty subrange of the arena",
            {"start_offset": start, "end_offset": end, "arena_size": arena_size},
        )
    if lifetime_start < 0 or lifetime_end < lifetime_start:
        return None, _reject(
            source_file,
            line_number,
            E_TIMESTAMP,
            "region lifetime must be a non-negative ordered interval",
            {"lifetime_start_ms": lifetime_start, "lifetime_end_ms": lifetime_end},
        )
    if generation < 0:
        return None, _reject(
            source_file,
            line_number,
            E_REGION_SCHEMA,
            "region generation must be non-negative",
            {"generation": generation},
        )

    region = Region(
        region_id=region_id,
        arena_id=arena_id,
        arena_size=arena_size,
        start_offset=start,
        end_offset=end,
        lifetime_start_ms=lifetime_start,
        lifetime_end_ms=lifetime_end,
        generation=generation,
        parent_region_id=parent,
        source_file=source_file,
        line_number=line_number,
    )
    return region, None


def parse_region_files(
    paths: Iterable[Path],
) -> tuple[dict[str, Region], list[Rejection], list[str]]:
    regions: dict[str, Region] = {}
    rejections: list[Rejection] = []
    used_files: list[str] = []
    for path in sorted((Path(item) for item in paths), key=lambda item: str(item)):
        path_text = str(path)
        used_files.append(path_text)
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            rejections.append(_reject(path_text, None, E_INPUT_READ, "cannot read region file", {"error": str(exc)}))
            continue
        items, file_rejection = _extract_region_items(path, text)
        if file_rejection is not None:
            rejections.append(file_rejection)
            continue
        for value, line_number in items:
            region, rejection = parse_region_object(value, path_text, line_number)
            if rejection is not None:
                rejections.append(rejection)
                continue
            assert region is not None
            if region.region_id in regions:
                previous = regions[region.region_id]
                rejections.append(
                    _reject(
                        path_text,
                        line_number,
                        E_REGION_ID_DUPLICATE,
                        "duplicate region_id",
                        {
                            "region_id": region.region_id,
                            "first_source_file": previous.source_file,
                            "first_line_number": previous.line_number,
                        },
                    )
                )
                continue
            regions[region.region_id] = region
    return regions, rejections, used_files


def parse_trace_object(
    value: Any,
    source_file: str,
    line_number: int,
    regions: Mapping[str, Region],
) -> tuple[Optional[TraceRecord], Optional[Rejection]]:
    if not isinstance(value, dict):
        return None, _reject(source_file, line_number, E_TRACE_SCHEMA, "trace record must be an object")

    required = (
        "trace_id",
        "access_seq",
        "region_id",
        "base_offset",
        "access_size",
        "access_kind",
        "arena_size",
        "timestamp_ms",
    )
    missing = [key for key in required if key not in value]
    if missing:
        return None, _reject(
            source_file,
            line_number,
            E_TRACE_SCHEMA,
            "trace record is missing required fields",
            {"missing": missing},
        )

    trace_id = value["trace_id"]
    region_id = value["region_id"]
    if not isinstance(trace_id, str) or not trace_id:
        return None, _reject(
            source_file, line_number, E_TRACE_SCHEMA, "trace_id must be a non-empty string"
        )
    if not isinstance(region_id, str) or not region_id:
        return None, _reject(
            source_file, line_number, E_TRACE_SCHEMA, "region_id must be a non-empty string"
        )
    if not _is_int(value["access_seq"]) or value["access_seq"] < 0:
        return None, _reject(
            source_file,
            line_number,
            E_ACCESS_SEQ,
            "access_seq must be a non-negative integer",
            {"access_seq": value["access_seq"]},
        )
    if value["access_seq"] > UINT64_MAX:
        return None, _reject(
            source_file,
            line_number,
            E_ACCESS_SEQ,
            "access_seq exceeds unsigned 64-bit",
            {"access_seq": value["access_seq"]},
        )
    if not _is_int(value["base_offset"]) or value["base_offset"] < 0:
        return None, _reject(
            source_file,
            line_number,
            E_OFFSET,
            "base_offset must be a non-negative integer",
            {"base_offset": value["base_offset"]},
        )
    if not _is_int(value["access_size"]) or value["access_size"] < 0:
        return None, _reject(
            source_file,
            line_number,
            E_ACCESS_SIZE,
            "access_size must be a non-negative integer",
            {"access_size": value["access_size"]},
        )
    base_offset = value["base_offset"]
    access_size = value["access_size"]
    if base_offset > UINT64_MAX or access_size > UINT64_MAX or base_offset + access_size > UINT64_MAX:
        return None, _reject(
            source_file,
            line_number,
            E_ACCESS_RANGE_OVERFLOW,
            "base_offset + access_size overflows unsigned 64-bit",
            {"base_offset": base_offset, "access_size": access_size},
        )
    access_kind = value["access_kind"]
    if access_kind not in TRACE_KINDS:
        return None, _reject(
            source_file,
            line_number,
            E_ACCESS_KIND,
            "access_kind must be read or write",
            {"access_kind": access_kind},
        )
    if not _is_int(value["arena_size"]) or value["arena_size"] <= 0:
        return None, _reject(
            source_file,
            line_number,
            E_ARENA_SIZE,
            "arena_size must be a positive integer",
            {"arena_size": value["arena_size"]},
        )
    if value["arena_size"] > UINT64_MAX:
        return None, _reject(
            source_file,
            line_number,
            E_ARENA_SIZE,
            "arena_size exceeds unsigned 64-bit",
            {"arena_size": value["arena_size"]},
        )
    if not _is_int(value["timestamp_ms"]) or value["timestamp_ms"] < 0:
        return None, _reject(
            source_file,
            line_number,
            E_TIMESTAMP,
            "timestamp_ms must be a non-negative integer",
            {"timestamp_ms": value["timestamp_ms"]},
        )

    region = regions.get(region_id)
    if region is None:
        return None, _reject(
            source_file,
            line_number,
            E_REGION_UNKNOWN,
            "trace references an undeclared region",
            {"region_id": region_id},
        )
    if value["arena_size"] != region.arena_size:
        return None, _reject(
            source_file,
            line_number,
            E_ARENA_SIZE_MISMATCH,
            "trace arena_size does not match the region declaration",
            {
                "region_id": region_id,
                "trace_arena_size": value["arena_size"],
                "declared_arena_size": region.arena_size,
            },
        )

    record = TraceRecord(
        trace_id=trace_id,
        access_seq=value["access_seq"],
        region_id=region_id,
        base_offset=base_offset,
        access_size=access_size,
        access_kind=access_kind,
        arena_size=value["arena_size"],
        timestamp_ms=value["timestamp_ms"],
        source_file=source_file,
        line_number=line_number,
        sender_interval=nullable_interval(value.get("sender_interval")),
        sync_marker=value.get("sync_marker") if isinstance(value.get("sync_marker"), str) else None,
        expected_interval=nullable_interval(value.get("expected_interval")),
        expected_business_code=(
            value.get("expected_business_code")
            if isinstance(value.get("expected_business_code"), str)
            else None
        ),
    )
    return record, None


def _digest_region_layout(regions: Mapping[str, Region]) -> str:
    canonical = [regions[key].canonical_dict() for key in sorted(regions)]
    return canonical_digest(canonical)


def scan_inputs(region_paths: Iterable[Path], trace_paths: Iterable[Path]) -> Manifest:
    region_files_input = [Path(item) for item in region_paths]
    trace_files_input = [Path(item) for item in trace_paths]
    regions, rejections, region_files = parse_region_files(region_files_input)
    region_layout_digest = _digest_region_layout(regions)

    refs_by_arena: dict[str, list[TraceRef]] = {}
    trace_file_digests: list[dict[str, str]] = []
    seen_trace_ids: dict[str, tuple[str, int]] = {}
    trace_files: list[str] = []
    seen_records = 0

    for path in sorted(trace_files_input, key=lambda item: str(item)):
        path_text = str(path)
        trace_files.append(path_text)
        try:
            digest = hashlib.sha256()
            file_bytes = 0
            with path.open("rb") as handle:
                offset = 0
                line_number = 0
                while True:
                    raw = handle.readline()
                    if not raw:
                        break
                    line_number += 1
                    digest.update(raw)
                    file_bytes += len(raw)
                    byte_offset = offset
                    offset += len(raw)
                    if not raw.strip():
                        continue
                    try:
                        value = json.loads(raw)
                    except (UnicodeError, ValueError, TypeError) as exc:
                        rejections.append(
                            _reject(
                                path_text,
                                line_number,
                                E_INPUT_JSON,
                                "invalid trace JSON line",
                                {"error": str(exc)},
                            )
                        )
                        continue
                    record, rejection = parse_trace_object(
                        value, path_text, line_number, regions
                    )
                    if rejection is not None:
                        rejections.append(rejection)
                        continue
                    assert record is not None
                    seen_records += 1
                    first = seen_trace_ids.get(record.trace_id)
                    if first is not None:
                        rejections.append(
                            _reject(
                                path_text,
                                line_number,
                                E_TRACE_ID_DUPLICATE,
                                "duplicate trace_id",
                                {
                                    "trace_id": record.trace_id,
                                    "first_source_file": first[0],
                                    "first_line_number": first[1],
                                },
                            )
                        )
                        continue
                    seen_trace_ids[record.trace_id] = (path_text, line_number)
                    region = regions[record.region_id]
                    refs_by_arena.setdefault(region.arena_id, []).append(
                        TraceRef(
                            source_file=path_text,
                            line_number=line_number,
                            byte_offset=byte_offset,
                            byte_length=len(raw),
                            arena_id=region.arena_id,
                            access_seq=record.access_seq,
                            region_id=record.region_id,
                            access_kind=record.access_kind,
                            trace_id=record.trace_id,
                        )
                    )
            if file_bytes == 0:
                rejections.append(
                    _reject(path_text, None, E_INPUT_EMPTY, "trace file is empty")
                )
            trace_file_digests.append({"path": path_text, "sha256": digest.hexdigest()})
        except OSError as exc:
            rejections.append(
                _reject(path_text, None, E_INPUT_READ, "cannot read trace file", {"error": str(exc)})
            )
            trace_file_digests.append({"path": path_text, "sha256": ""})

    trace_input_digest = canonical_digest(trace_file_digests)
    layout_key = canonical_digest(
        {
            "policy_version": POLICY_VERSION,
            "ledger_schema_version": LEDGER_SCHEMA_VERSION,
            "region_layout_digest": region_layout_digest,
            "trace_input_digest": trace_input_digest,
        }
    )

    for arena in refs_by_arena:
        refs_by_arena[arena].sort(
            key=lambda ref: (ref.source_file, ref.line_number, ref.trace_id)
        )

    rejections.sort(key=lambda item: item.sort_key)
    return Manifest(
        regions=regions,
        refs_by_arena=refs_by_arena,
        rejections=rejections,
        region_files=sorted(region_files),
        trace_files=sorted(trace_files),
        region_layout_digest=region_layout_digest,
        trace_input_digest=trace_input_digest,
        layout_key=layout_key,
        trace_records_seen=seen_records,
    )


def load_trace_refs(refs: Iterable[TraceRef], regions: Mapping[str, Region]) -> tuple[list[TraceRecord], list[Rejection], int]:
    """Load exactly the scheduled byte ranges for one arena."""

    records: list[TraceRecord] = []
    rejections: list[Rejection] = []
    loaded_bytes = 0
    handles: dict[str, Any] = {}
    try:
        for ref in sorted(refs, key=lambda item: (item.source_file, item.line_number)):
            handle = handles.get(ref.source_file)
            if handle is None:
                handle = open(ref.source_file, "rb")
                handles[ref.source_file] = handle
            handle.seek(ref.byte_offset)
            raw = handle.read(ref.byte_length)
            loaded_bytes += len(raw)
            if len(raw) != ref.byte_length:
                rejections.append(
                    _reject(
                        ref.source_file,
                        ref.line_number,
                        E_INPUT_READ,
                        "scheduled trace line changed or was truncated",
                        {"expected_bytes": ref.byte_length, "actual_bytes": len(raw)},
                    )
                )
                continue
            try:
                value = json.loads(raw)
            except (UnicodeError, ValueError, TypeError) as exc:
                rejections.append(
                    _reject(
                        ref.source_file,
                        ref.line_number,
                        E_INPUT_JSON,
                        "scheduled trace JSON line became invalid",
                        {"error": str(exc)},
                    )
                )
                continue
            record, rejection = parse_trace_object(
                value, ref.source_file, ref.line_number, regions
            )
            if rejection is not None:
                rejections.append(rejection)
                continue
            assert record is not None
            if (
                record.trace_id != ref.trace_id
                or record.access_seq != ref.access_seq
                or record.region_id != ref.region_id
                or record.access_kind != ref.access_kind
            ):
                rejections.append(
                    _reject(
                        ref.source_file,
                        ref.line_number,
                        E_INPUT_READ,
                        "scheduled trace line no longer matches the manifest",
                    )
                )
                continue
            records.append(record)
    finally:
        for handle in handles.values():
            handle.close()
    return records, rejections, loaded_bytes


def region_file_hashes(paths: Iterable[Path]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for path in sorted((Path(item) for item in paths), key=lambda item: str(item)):
        try:
            result.append({"path": str(path), "sha256": file_sha256(path)})
        except OSError:
            result.append({"path": str(path), "sha256": ""})
    return result


def path_input_rejection(path: Path) -> Rejection:
    return _reject(str(path), None, E_INPUT_PATH, "input path does not exist or is not a file")
