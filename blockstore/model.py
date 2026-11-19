"""Parsing and validation for deterministic snapshot samples."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .errors import BusinessError


SUPPORTED_FORMAT_VERSION = 1


@dataclass(frozen=True)
class BlockImage:
    block_index: int
    data: bytes
    digest: str


@dataclass(frozen=True)
class RangeSpec:
    start: int
    end: int
    blocks: tuple[BlockImage, ...]


@dataclass(frozen=True)
class SnapshotSpec:
    sequence: int
    snapshot_id: str
    parent_id: str
    clock_ms: int
    ranges: tuple[RangeSpec, ...]
    writes: dict[int, BlockImage] = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        payload = {
            "sequence": self.sequence,
            "snapshot_id": self.snapshot_id,
            "parent_id": self.parent_id,
            "clock_ms": self.clock_ms,
            "ranges": [
                {
                    "start": item.start,
                    "end": item.end,
                    "blocks": [
                        {"block": block.block_index, "digest": block.digest}
                        for block in item.blocks
                    ],
                }
                for item in self.ranges
            ],
        }
        canonical = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RollbackSpec:
    request_id: str
    target_snapshot_id: str
    clock_ms: int
    retain_after: bool


@dataclass(frozen=True)
class VolumeSpec:
    volume_id: str
    block_size: int
    block_count: int
    base_snapshot_id: str
    initial_clock_ms: int
    blocks: tuple[BlockImage, ...]

    @property
    def block_map(self) -> dict[int, BlockImage]:
        return {block.block_index: block for block in self.blocks}


@dataclass(frozen=True)
class SampleSpec:
    format_version: int
    volume: VolumeSpec
    snapshots: tuple[SnapshotSpec, ...]
    rollbacks: tuple[RollbackSpec, ...]
    sample_hash: str


def _error(code: str, message: str, **details: Any) -> BusinessError:
    return BusinessError(code, message, details=details)


def _require_mapping(value: Any, description: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _error("INVALID_SCHEMA", f"{description} must be a JSON object")
    return value


def _require_list(value: Any, description: str) -> list[Any]:
    if not isinstance(value, list):
        raise _error("INVALID_SCHEMA", f"{description} must be a JSON array")
    return value


def _require_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _error(
            "INVALID_SCHEMA",
            f"{field_name} must be a non-empty string",
            field=field_name,
        )
    return value


def _require_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _error(
            "INVALID_SCHEMA",
            f"{field_name} must be an integer",
            field=field_name,
        )
    return value


def _require_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise _error(
            "INVALID_SCHEMA",
            f"{field_name} must be a boolean",
            field=field_name,
        )
    return value


def normalize_digest(value: Any, *, field_name: str = "digest") -> str:
    if not isinstance(value, str):
        raise _error("INVALID_DIGEST", f"{field_name} must be a SHA-256 string")
    digest = value.lower()
    if digest.startswith("sha256:"):
        digest = digest[7:]
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise _error(
            "INVALID_DIGEST",
            f"{field_name} must be 64 hexadecimal characters",
            field=field_name,
        )
    return digest


def decode_block_data(value: Any, field_name: str) -> bytes:
    if not isinstance(value, str):
        raise _error(
            "INVALID_BLOCK_DATA",
            f"{field_name} must be a base64 string",
            field=field_name,
        )
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise _error(
            "INVALID_BLOCK_DATA",
            f"{field_name} is not valid base64",
            field=field_name,
        ) from exc


def parse_block(
    raw: Any,
    *,
    block_size: int,
    block_count: int,
    description: str,
) -> BlockImage:
    item = _require_mapping(raw, description)
    block_index = _require_int(item.get("block"), f"{description}.block")
    if block_index < 0 or block_index >= block_count:
        raise _error(
            "BLOCK_RANGE_OUT_OF_BOUNDS",
            f"{description}.block is outside the volume",
            block=block_index,
            block_count=block_count,
        )
    data = decode_block_data(item.get("data_base64"), f"{description}.data_base64")
    if len(data) != block_size:
        raise _error(
            "INVALID_BLOCK_SIZE",
            f"{description} data must contain exactly {block_size} bytes",
            block=block_index,
            expected=block_size,
            actual=len(data),
        )
    digest = normalize_digest(item.get("digest"), field_name=f"{description}.digest")
    actual_digest = hashlib.sha256(data).hexdigest()
    if digest != actual_digest:
        raise _error(
            "DIGEST_MISMATCH",
            f"{description} digest does not match its data",
            block=block_index,
            expected=actual_digest,
            actual=digest,
        )
    return BlockImage(block_index=block_index, data=data, digest=digest)


def _parse_volume(raw: Any) -> VolumeSpec:
    volume = _require_mapping(raw, "volume")
    volume_id = _require_string(volume.get("id"), "volume.id")
    block_size = _require_int(volume.get("block_size"), "volume.block_size")
    block_count = _require_int(volume.get("block_count"), "volume.block_count")
    if block_size <= 0:
        raise _error(
            "INVALID_BLOCK_SIZE",
            "volume.block_size must be a positive integer",
            value=block_size,
        )
    if block_count <= 0:
        raise _error(
            "INVALID_BLOCK_COUNT",
            "volume.block_count must be a positive integer",
            value=block_count,
        )
    base_snapshot_id = _require_string(
        volume.get("base_snapshot_id", "base"), "volume.base_snapshot_id"
    )
    initial_clock_ms = _require_int(
        volume.get("initial_clock_ms", 0), "volume.initial_clock_ms"
    )
    if initial_clock_ms < 0:
        raise _error(
            "INVALID_CLOCK",
            "volume.initial_clock_ms must be non-negative",
            value=initial_clock_ms,
        )

    raw_blocks = _require_list(volume.get("blocks"), "volume.blocks")
    blocks = [
        parse_block(
            item,
            block_size=block_size,
            block_count=block_count,
            description=f"volume.blocks[{index}]",
        )
        for index, item in enumerate(raw_blocks)
    ]
    seen: set[int] = set()
    for block in blocks:
        if block.block_index in seen:
            raise _error(
                "DUPLICATE_BLOCK",
                "volume contains duplicate block indexes",
                block=block.block_index,
            )
        seen.add(block.block_index)
    missing = sorted(set(range(block_count)) - seen)
    if missing:
        raise _error(
            "MISSING_BLOCK",
            "volume does not describe every block",
            missing_blocks=missing,
        )
    blocks.sort(key=lambda item: item.block_index)
    return VolumeSpec(
        volume_id=volume_id,
        block_size=block_size,
        block_count=block_count,
        base_snapshot_id=base_snapshot_id,
        initial_clock_ms=initial_clock_ms,
        blocks=tuple(blocks),
    )


def _parse_ranges(
    raw_ranges: Any,
    *,
    block_size: int,
    block_count: int,
    description: str,
) -> tuple[RangeSpec, ...]:
    ranges = _require_list(raw_ranges, description)
    parsed: list[RangeSpec] = []
    globally_seen: set[int] = set()
    for range_index, raw_range in enumerate(ranges):
        range_obj = _require_mapping(raw_range, f"{description}[{range_index}]")
        start = _require_int(range_obj.get("start"), f"{description}[{range_index}].start")
        end = _require_int(range_obj.get("end"), f"{description}[{range_index}].end")
        if start < 0 or end < start or end >= block_count:
            raise _error(
                "BLOCK_RANGE_OUT_OF_BOUNDS",
                "snapshot range is outside the volume",
                start=start,
                end=end,
                block_count=block_count,
            )
        raw_blocks = _require_list(
            range_obj.get("blocks"), f"{description}[{range_index}].blocks"
        )
        blocks = [
            parse_block(
                item,
                block_size=block_size,
                block_count=block_count,
                description=f"{description}[{range_index}].blocks[{item_index}]",
            )
            for item_index, item in enumerate(raw_blocks)
        ]
        block_indexes = [item.block_index for item in blocks]
        expected = list(range(start, end + 1))
        if block_indexes != expected:
            raise _error(
                "INVALID_RANGE_COVERAGE",
                "each range must list every covered block exactly once in order",
                start=start,
                end=end,
                expected_blocks=expected,
                actual_blocks=block_indexes,
            )
        overlap = globally_seen.intersection(block_indexes)
        if overlap:
            raise _error(
                "DUPLICATE_WRITE_BLOCK",
                "a block is written more than once by one snapshot",
                blocks=sorted(overlap),
            )
        globally_seen.update(block_indexes)
        parsed.append(RangeSpec(start=start, end=end, blocks=tuple(blocks)))
    return tuple(parsed)


def _parse_snapshots(
    raw_snapshots: Any,
    *,
    volume: VolumeSpec,
) -> tuple[SnapshotSpec, ...]:
    snapshots_raw = _require_list(raw_snapshots, "snapshots")
    sequence_set: set[int] = set()
    base = SnapshotSpec(
        sequence=0,
        snapshot_id=volume.base_snapshot_id,
        parent_id="",
        clock_ms=volume.initial_clock_ms,
        ranges=(),
        writes={},
    )
    by_id: dict[str, SnapshotSpec] = {base.snapshot_id: base}

    for index, raw_snapshot in enumerate(snapshots_raw):
        item = _require_mapping(raw_snapshot, f"snapshots[{index}]")
        sequence = _require_int(item.get("sequence"), f"snapshots[{index}].sequence")
        if sequence <= 0:
            raise _error(
                "INVALID_SNAPSHOT_SEQUENCE",
                "snapshot sequence must be positive",
                sequence=sequence,
            )
        if sequence in sequence_set:
            raise _error(
                "DUPLICATE_SNAPSHOT_SEQUENCE",
                "snapshot sequence must be unique",
                sequence=sequence,
            )
        snapshot_id = _require_string(
            item.get("id"), f"snapshots[{index}].id"
        )
        if snapshot_id in by_id:
            raise _error(
                "DUPLICATE_SNAPSHOT_ID",
                "snapshot id must be unique",
                snapshot_id=snapshot_id,
            )
        parent_id = _require_string(
            item.get("parent_id"), f"snapshots[{index}].parent_id"
        )
        clock_ms = _require_int(item.get("clock_ms"), f"snapshots[{index}].clock_ms")
        ranges = _parse_ranges(
            item.get("ranges", []),
            block_size=volume.block_size,
            block_count=volume.block_count,
            description=f"snapshots[{index}].ranges",
        )
        writes = {block.block_index: block for item_range in ranges for block in item_range.blocks}
        snapshot = SnapshotSpec(
            sequence=sequence,
            snapshot_id=snapshot_id,
            parent_id=parent_id,
            clock_ms=clock_ms,
            ranges=ranges,
            writes=writes,
        )
        sequence_set.add(sequence)
        by_id[snapshot_id] = snapshot

    for snapshot in by_id.values():
        if snapshot.snapshot_id == base.snapshot_id:
            continue
        parent = by_id.get(snapshot.parent_id)
        if parent is None:
            raise _error(
                "PARENT_NOT_FOUND",
                "snapshot parent must be the base or another snapshot",
                snapshot_id=snapshot.snapshot_id,
                parent_id=snapshot.parent_id,
            )
        if snapshot.clock_ms <= parent.clock_ms:
            raise _error(
                "CLOCK_NOT_MONOTONIC",
                "snapshot clock must be strictly later than its parent",
                snapshot_id=snapshot.snapshot_id,
                clock_ms=snapshot.clock_ms,
                parent_clock_ms=parent.clock_ms,
            )

    for snapshot in by_id.values():
        seen: set[str] = set()
        current = snapshot
        while current.parent_id:
            if current.snapshot_id in seen:
                raise _error(
                    "SNAPSHOT_PARENT_CYCLE",
                    "snapshot parent links contain a cycle",
                    snapshot_id=current.snapshot_id,
                )
            seen.add(current.snapshot_id)
            current = by_id[current.parent_id]

    return tuple(
        sorted(
            (item for item in by_id.values() if item.snapshot_id != base.snapshot_id),
            key=lambda item: item.sequence,
        )
    )


def _parse_rollbacks(
    raw_rollbacks: Any,
    *,
    known_snapshot_ids: Iterable[str],
) -> tuple[RollbackSpec, ...]:
    rollbacks_raw = _require_list(raw_rollbacks, "rollbacks")
    known = set(known_snapshot_ids)
    seen_ids: set[str] = set()
    result: list[RollbackSpec] = []
    for index, raw_rollback in enumerate(rollbacks_raw):
        item = _require_mapping(raw_rollback, f"rollbacks[{index}]")
        request_id = _require_string(
            item.get("request_id"), f"rollbacks[{index}].request_id"
        )
        if request_id in seen_ids:
            raise _error(
                "DUPLICATE_ROLLBACK_ID",
                "rollback request id must be unique",
                request_id=request_id,
            )
        seen_ids.add(request_id)
        target = _require_string(
            item.get("target_snapshot_id"),
            f"rollbacks[{index}].target_snapshot_id",
        )
        if target not in known:
            raise _error(
                "ROLLBACK_TARGET_NOT_FOUND",
                "rollback target snapshot does not exist",
                request_id=request_id,
                target_snapshot_id=target,
            )
        clock_ms = _require_int(
            item.get("clock_ms"), f"rollbacks[{index}].clock_ms"
        )
        if clock_ms < 0:
            raise _error(
                "INVALID_CLOCK",
                "rollback clock must be non-negative",
                request_id=request_id,
                clock_ms=clock_ms,
            )
        retain_after = _require_bool(
            item.get("retain_after", False),
            f"rollbacks[{index}].retain_after",
        )
        result.append(
            RollbackSpec(
                request_id=request_id,
                target_snapshot_id=target,
                clock_ms=clock_ms,
                retain_after=retain_after,
            )
        )
    return tuple(result)


def parse_sample(raw: Any, *, sample_hash: str | None = None) -> SampleSpec:
    root = _require_mapping(raw, "sample")
    format_version = _require_int(root.get("format_version"), "format_version")
    if format_version != SUPPORTED_FORMAT_VERSION:
        raise _error(
            "UNSUPPORTED_FORMAT",
            f"only sample format_version {SUPPORTED_FORMAT_VERSION} is supported",
            actual=format_version,
        )
    volume = _parse_volume(root.get("volume"))
    snapshots = _parse_snapshots(root.get("snapshots", []), volume=volume)
    known_ids = {volume.base_snapshot_id}
    known_ids.update(item.snapshot_id for item in snapshots)
    rollbacks = _parse_rollbacks(root.get("rollbacks", []), known_snapshot_ids=known_ids)
    if sample_hash is None:
        canonical = json.dumps(
            raw, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        sample_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return SampleSpec(
        format_version=format_version,
        volume=volume,
        snapshots=snapshots,
        rollbacks=rollbacks,
        sample_hash=sample_hash,
    )


def load_sample(path: str | Path) -> SampleSpec:
    sample_path = Path(path)
    try:
        raw_bytes = sample_path.read_bytes()
    except OSError as exc:
        raise _error(
            "SAMPLE_READ_ERROR",
            "could not read sample file",
            path=str(sample_path),
        ) from exc
    try:
        raw = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _error(
            "INVALID_JSON",
            "sample file is not valid UTF-8 JSON",
            path=str(sample_path),
        ) from exc
    canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    sample_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return parse_sample(raw, sample_hash=sample_hash)


def canonical_visible_hash(block_map: dict[int, str]) -> str:
    payload = [[index, block_map[index]] for index in sorted(block_map)]
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
