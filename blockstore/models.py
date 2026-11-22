from __future__ import annotations

import base64
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .canonical import canonical_json, sha256_bytes, sha256_text
from .errors import BlockStoreError, ErrorCode


def _as_int(value: Any, name: str, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BlockStoreError(
            ErrorCode.INVALID_INPUT,
            f"{name} must be an integer",
            {"context": context, "value": value},
        )
    return value


def _as_str(value: Any, name: str, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BlockStoreError(
            ErrorCode.INVALID_INPUT,
            f"{name} must be a non-empty string",
            {"context": context, "value": value},
        )
    return value.strip()


def _as_bool(value: Any, name: str, context: str) -> bool:
    if not isinstance(value, bool):
        raise BlockStoreError(
            ErrorCode.INVALID_INPUT,
            f"{name} must be a boolean",
            {"context": context, "value": value},
        )
    return value


def _as_dict(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BlockStoreError(
            ErrorCode.INVALID_INPUT,
            "expected a JSON object",
            {"context": context},
        )
    return value


def base_snapshot_id(volume_id: str) -> str:
    return f"{volume_id}@base"


@dataclass(frozen=True, slots=True)
class BlockData:
    block_no: int
    data: bytes
    digest: str
    encoding: str = "utf8"
    encoded_data: str = ""


@dataclass(frozen=True, slots=True)
class VolumeSpec:
    volume_id: str
    block_size: int
    block_count: int
    created_clock: int
    blocks: dict[int, BlockData]

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "volume_id": self.volume_id,
            "block_size": self.block_size,
            "block_count": self.block_count,
            "created_clock": self.created_clock,
            "blocks": [
                {
                    "block_no": block_no,
                    "digest": self.blocks[block_no].digest,
                }
                for block_no in sorted(self.blocks)
            ],
        }

    @property
    def digest(self) -> str:
        return sha256_text(canonical_json(self.canonical_dict()))


@dataclass(frozen=True, slots=True)
class SnapshotSpec:
    snapshot_id: str
    seq: int
    volume_id: str
    parent_snapshot_id: str
    created_clock: int
    covered_start: int
    covered_end: int
    blocks: dict[int, BlockData]

    @property
    def manifest(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "seq": self.seq,
            "volume_id": self.volume_id,
            "parent_snapshot_id": self.parent_snapshot_id,
            "created_clock": self.created_clock,
            "covered_start": self.covered_start,
            "covered_end": self.covered_end,
            "blocks": [
                {"block_no": block_no, "digest": self.blocks[block_no].digest}
                for block_no in sorted(self.blocks)
            ],
        }

    @property
    def manifest_json(self) -> str:
        return canonical_json(self.manifest)

    @property
    def manifest_digest(self) -> str:
        return sha256_text(self.manifest_json)

    def spec_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "seq": self.seq,
            "volume_id": self.volume_id,
            "parent_snapshot_id": self.parent_snapshot_id,
            "created_clock": self.created_clock,
            "covered_start": self.covered_start,
            "covered_end": self.covered_end,
            "blocks": [
                {
                    "block_no": block_no,
                    "encoding": "base64",
                    "data": base64.b64encode(block.data).decode("ascii"),
                    "sha256": block.digest,
                }
                for block_no, block in sorted(self.blocks.items())
            ],
        }


@dataclass(frozen=True, slots=True)
class RollbackRequest:
    request_id: str
    target_snapshot_id: str
    rollback_clock: int
    keep_after_target: bool

    def payload(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "target_snapshot_id": self.target_snapshot_id,
            "rollback_clock": self.rollback_clock,
            "keep_after_target": self.keep_after_target,
        }

    @property
    def payload_json(self) -> str:
        return canonical_json(self.payload())


@dataclass(frozen=True, slots=True)
class PlanBundle:
    volume: VolumeSpec
    snapshots: list[SnapshotSpec] = field(default_factory=list)
    rollbacks: list[RollbackRequest] = field(default_factory=list)


def _parse_block_entry(
    raw: Any,
    *,
    block_size: int,
    context: str,
) -> BlockData:
    entry = _as_dict(raw, context)
    block_no = _as_int(entry.get("block_no"), "block_no", context)
    data = entry.get("data", entry.get("content"))
    if not isinstance(data, str):
        raise BlockStoreError(
            ErrorCode.INVALID_BLOCK_DATA,
            "block data must be a string",
            {"context": context, "block_no": block_no},
        )
    encoding = entry.get("encoding", "utf8")
    if not isinstance(encoding, str):
        raise BlockStoreError(
            ErrorCode.INVALID_BLOCK_DATA,
            "block encoding must be a string",
            {"context": context, "block_no": block_no},
        )
    from .canonical import decode_block_data

    content = decode_block_data(
        {"data": data, "encoding": encoding},
        block_size,
        context,
    )
    actual = sha256_bytes(content)
    supplied = entry.get("sha256", entry.get("digest"))
    if supplied is not None:
        if not isinstance(supplied, str):
            raise BlockStoreError(
                ErrorCode.DIGEST_MISMATCH,
                "block sha256 must be a string",
                {"context": context, "block_no": block_no},
            )
        if supplied.lower() != actual:
            raise BlockStoreError(
                ErrorCode.DIGEST_MISMATCH,
                "block digest does not match its content",
                {
                    "context": context,
                    "block_no": block_no,
                    "expected": supplied.lower(),
                    "actual": actual,
                },
            )
    return BlockData(
        block_no=block_no,
        data=content,
        digest=actual,
        encoding=encoding,
        encoded_data=data,
    )


def parse_volume_document(raw: Any, context: str = "volume") -> VolumeSpec:
    document = _as_dict(raw, context)
    volume_id = _as_str(document.get("volume_id", document.get("id")), "volume_id", context)
    block_size = _as_int(document.get("block_size"), "block_size", context)
    if block_size <= 0:
        raise BlockStoreError(
            ErrorCode.INVALID_BLOCK_SIZE,
            "block_size must be positive",
            {"context": context, "block_size": block_size},
        )
    block_count = _as_int(document.get("block_count"), "block_count", context)
    if block_count <= 0:
        raise BlockStoreError(
            ErrorCode.INVALID_BLOCK_COUNT,
            "block_count must be positive",
            {"context": context, "block_count": block_count},
        )
    created_clock = _as_int(document.get("created_clock", 0), "created_clock", context)
    raw_blocks = document.get("blocks")
    if not isinstance(raw_blocks, list):
        raise BlockStoreError(
            ErrorCode.INVALID_BLOCK_DATA,
            "volume blocks must be an array",
            {"context": context},
        )
    blocks: dict[int, BlockData] = {}
    for index, raw_block in enumerate(raw_blocks):
        block = _parse_block_entry(
            raw_block,
            block_size=block_size,
            context=f"{context}.blocks[{index}]",
        )
        if block.block_no < 0 or block.block_no >= block_count:
            raise BlockStoreError(
                ErrorCode.INVALID_BLOCK_NUMBER,
                "initial block number is outside the volume",
                {"block_no": block.block_no, "block_count": block_count},
            )
        if block.block_no in blocks:
            raise BlockStoreError(
                ErrorCode.INVALID_BLOCK_DATA,
                "duplicate initial block number",
                {"block_no": block.block_no},
            )
        blocks[block.block_no] = block
    missing = [number for number in range(block_count) if number not in blocks]
    if missing:
        raise BlockStoreError(
            ErrorCode.INVALID_BLOCK_DATA,
            "volume must provide every initial block exactly once",
            {"missing_blocks": missing},
        )
    return VolumeSpec(
        volume_id=volume_id,
        block_size=block_size,
        block_count=block_count,
        created_clock=created_clock,
        blocks=blocks,
    )


def _snapshot_entries(raw: Any, context: str) -> list[Any]:
    if isinstance(raw, list):
        return raw
    document = _as_dict(raw, context)
    if "snapshots" in document:
        entries = document["snapshots"]
        if not isinstance(entries, list):
            raise BlockStoreError(
                ErrorCode.INVALID_INPUT,
                "snapshots must be an array",
                {"context": context},
            )
        return entries
    return [document]


def parse_snapshot_document(
    raw: Any,
    volume: VolumeSpec,
    *,
    context: str = "snapshots",
) -> list[SnapshotSpec]:
    if isinstance(raw, dict) and "volume_id" in raw:
        document_volume_id = _as_str(raw.get("volume_id"), "volume_id", context)
        if document_volume_id != volume.volume_id:
            raise BlockStoreError(
                ErrorCode.VOLUME_MISMATCH,
                "snapshot document belongs to another volume",
                {
                    "expected_volume_id": volume.volume_id,
                    "actual_volume_id": document_volume_id,
                },
            )
    specs: list[SnapshotSpec] = []
    for index, entry in enumerate(_snapshot_entries(raw, context)):
        item_context = f"{context}[{index}]"
        item = _as_dict(entry, item_context)
        if "volume_id" in item and _as_str(
            item.get("volume_id"), "volume_id", item_context
        ) != volume.volume_id:
            raise BlockStoreError(
                ErrorCode.VOLUME_MISMATCH,
                "snapshot belongs to another volume",
                {
                    "context": item_context,
                    "expected_volume_id": volume.volume_id,
                    "actual_volume_id": item.get("volume_id"),
                },
            )
        snapshot_id = _as_str(
            item.get("snapshot_id", item.get("id")),
            "snapshot_id",
            item_context,
        )
        if snapshot_id == base_snapshot_id(volume.volume_id):
            raise BlockStoreError(
                ErrorCode.DUPLICATE_SNAPSHOT_ID,
                "snapshot_id is reserved for the base snapshot",
                {"snapshot_id": snapshot_id},
            )
        seq = _as_int(item.get("seq", item.get("snapshot_seq")), "seq", item_context)
        if seq <= 0:
            raise BlockStoreError(
                ErrorCode.INVALID_INPUT,
                "snapshot seq must be positive",
                {"context": item_context, "seq": seq},
            )
        parent = _as_str(
            item.get("parent_snapshot_id", item.get("parent")),
            "parent_snapshot_id",
            item_context,
        )
        created_clock = _as_int(
            item.get("created_clock", item.get("logical_clock")),
            "created_clock",
            item_context,
        )
        covered = item.get("covered_blocks")
        if covered is not None:
            if not isinstance(covered, list) or not covered:
                raise BlockStoreError(
                    ErrorCode.BLOCK_RANGE_INVALID,
                    "covered_blocks must be a non-empty array",
                    {"context": item_context},
                )
            covered_numbers = [
                _as_int(number, "covered_blocks[]", item_context) for number in covered
            ]
        else:
            start = _as_int(item.get("covered_start"), "covered_start", item_context)
            end = _as_int(item.get("covered_end"), "covered_end", item_context)
            if start < 0 or end < start or end >= volume.block_count:
                raise BlockStoreError(
                    ErrorCode.BLOCK_RANGE_INVALID,
                    "covered range is outside the volume",
                    {
                        "context": item_context,
                        "covered_start": start,
                        "covered_end": end,
                        "block_count": volume.block_count,
                    },
                )
            covered_numbers = list(range(start, end + 1))
        if not covered_numbers:
            raise BlockStoreError(
                ErrorCode.BLOCK_RANGE_INVALID,
                "snapshot must cover at least one block",
                {"context": item_context},
            )
        if covered_numbers != list(range(covered_numbers[0], covered_numbers[-1] + 1)):
            raise BlockStoreError(
                ErrorCode.BLOCK_RANGE_INVALID,
                "covered blocks must form a contiguous range",
                {"context": item_context, "covered_blocks": covered_numbers},
            )
        if covered_numbers[0] < 0 or covered_numbers[-1] >= volume.block_count:
            raise BlockStoreError(
                ErrorCode.BLOCK_RANGE_INVALID,
                "covered blocks are outside the volume",
                {
                    "context": item_context,
                    "covered_start": covered_numbers[0],
                    "covered_end": covered_numbers[-1],
                    "block_count": volume.block_count,
                },
            )
        raw_blocks = item.get("blocks")
        if not isinstance(raw_blocks, list):
            raise BlockStoreError(
                ErrorCode.COVERAGE_MISMATCH,
                "snapshot blocks must be an array",
                {"context": item_context},
            )
        blocks: dict[int, BlockData] = {}
        for block_index, raw_block in enumerate(raw_blocks):
            block = _parse_block_entry(
                raw_block,
                block_size=volume.block_size,
                context=f"{item_context}.blocks[{block_index}]",
            )
            if block.block_no in blocks:
                raise BlockStoreError(
                    ErrorCode.COVERAGE_MISMATCH,
                    "duplicate copy-on-write block",
                    {"context": item_context, "block_no": block.block_no},
                )
            blocks[block.block_no] = block
        expected = set(covered_numbers)
        actual = set(blocks)
        if actual != expected:
            raise BlockStoreError(
                ErrorCode.COVERAGE_MISMATCH,
                "copy-on-write blocks do not match the covered range",
                {
                    "context": item_context,
                    "missing_blocks": sorted(expected - actual),
                    "extra_blocks": sorted(actual - expected),
                },
            )
        specs.append(
            SnapshotSpec(
                snapshot_id=snapshot_id,
                seq=seq,
                volume_id=volume.volume_id,
                parent_snapshot_id=parent,
                created_clock=created_clock,
                covered_start=covered_numbers[0],
                covered_end=covered_numbers[-1],
                blocks=blocks,
            )
        )
    return specs


def _rollback_entries(raw: Any, context: str) -> list[Any]:
    if isinstance(raw, list):
        return raw
    document = _as_dict(raw, context)
    if "rollback_requests" in document:
        entries = document["rollback_requests"]
    elif "requests" in document:
        entries = document["requests"]
    else:
        return [document]
    if not isinstance(entries, list):
        raise BlockStoreError(
            ErrorCode.INVALID_INPUT,
            "rollback requests must be an array",
            {"context": context},
        )
    return entries


def parse_rollback_document(
    raw: Any,
    volume: VolumeSpec,
    *,
    context: str = "rollbacks",
) -> list[RollbackRequest]:
    if isinstance(raw, dict) and "volume_id" in raw:
        document_volume_id = _as_str(raw.get("volume_id"), "volume_id", context)
        if document_volume_id != volume.volume_id:
            raise BlockStoreError(
                ErrorCode.VOLUME_MISMATCH,
                "rollback document belongs to another volume",
                {
                    "expected_volume_id": volume.volume_id,
                    "actual_volume_id": document_volume_id,
                },
            )
    requests: list[RollbackRequest] = []
    for index, entry in enumerate(_rollback_entries(raw, context)):
        item_context = f"{context}[{index}]"
        item = _as_dict(entry, item_context)
        if "volume_id" in item and _as_str(
            item.get("volume_id"), "volume_id", item_context
        ) != volume.volume_id:
            raise BlockStoreError(
                ErrorCode.VOLUME_MISMATCH,
                "rollback request belongs to another volume",
                {
                    "context": item_context,
                    "expected_volume_id": volume.volume_id,
                    "actual_volume_id": item.get("volume_id"),
                },
            )
        request_id = _as_str(
            item.get("request_id", item.get("id")),
            "request_id",
            item_context,
        )
        target = _as_str(
            item.get("target_snapshot_id", item.get("target")),
            "target_snapshot_id",
            item_context,
        )
        clock = _as_int(
            item.get("rollback_clock", item.get("logical_clock")),
            "rollback_clock",
            item_context,
        )
        keep = _as_bool(
            item.get("keep_after_target", False),
            "keep_after_target",
            item_context,
        )
        requests.append(
            RollbackRequest(
                request_id=request_id,
                target_snapshot_id=target,
                rollback_clock=clock,
                keep_after_target=keep,
            )
        )
    return requests


def validate_plan_bundle(bundle: PlanBundle) -> None:
    volume = bundle.volume
    known: dict[str, int] = {base_snapshot_id(volume.volume_id): volume.created_clock}
    seen_ids: set[str] = set()
    seen_seqs: set[int] = set()
    by_id: dict[str, SnapshotSpec] = {}
    for spec in bundle.snapshots:
        if spec.volume_id != volume.volume_id:
            raise BlockStoreError(
                ErrorCode.VOLUME_MISMATCH,
                "snapshot belongs to another volume",
                {
                    "snapshot_id": spec.snapshot_id,
                    "expected_volume_id": volume.volume_id,
                    "actual_volume_id": spec.volume_id,
                },
            )
        if spec.snapshot_id in seen_ids:
            raise BlockStoreError(
                ErrorCode.DUPLICATE_SNAPSHOT_ID,
                "snapshot_id must be unique",
                {"snapshot_id": spec.snapshot_id},
            )
        if spec.seq in seen_seqs:
            raise BlockStoreError(
                ErrorCode.DUPLICATE_SNAPSHOT_SEQ,
                "snapshot seq must be unique",
                {"seq": spec.seq, "snapshot_id": spec.snapshot_id},
            )
        seen_ids.add(spec.snapshot_id)
        seen_seqs.add(spec.seq)
        by_id[spec.snapshot_id] = spec
    for spec in bundle.snapshots:
        parent = spec.parent_snapshot_id
        if parent != base_snapshot_id(volume.volume_id) and parent not in by_id:
            raise BlockStoreError(
                ErrorCode.PARENT_NOT_FOUND,
                "parent snapshot does not exist",
                {
                    "snapshot_id": spec.snapshot_id,
                    "parent_snapshot_id": spec.parent_snapshot_id,
                },
            )
    validating: set[str] = set()
    validated: set[str] = set()

    def validate_clock(snapshot_id: str) -> None:
        if snapshot_id in validated:
            return
        if snapshot_id in validating:
            raise BlockStoreError(
                ErrorCode.PARENT_NOT_FOUND,
                "snapshot parent relation contains a cycle",
                {"snapshot_id": snapshot_id},
            )
        validating.add(snapshot_id)
        spec = by_id[snapshot_id]
        parent = spec.parent_snapshot_id
        if parent != base_snapshot_id(volume.volume_id):
            if parent not in by_id:
                raise BlockStoreError(
                    ErrorCode.PARENT_NOT_FOUND,
                    "parent snapshot does not exist",
                    {
                        "snapshot_id": spec.snapshot_id,
                        "parent_snapshot_id": parent,
                    },
                )
            validate_clock(parent)
            parent_clock = by_id[parent].created_clock
        else:
            parent_clock = known[parent]
        if spec.created_clock <= parent_clock:
            raise BlockStoreError(
                ErrorCode.CLOCK_NOT_MONOTONIC,
                "snapshot clock must be strictly later than its parent",
                {
                    "snapshot_id": spec.snapshot_id,
                    "created_clock": spec.created_clock,
                    "parent_clock": parent_clock,
                },
            )
        known[spec.snapshot_id] = spec.created_clock
        validating.remove(snapshot_id)
        validated.add(snapshot_id)

    for spec in bundle.snapshots:
        validate_clock(spec.snapshot_id)
    seen_requests: set[str] = set()
    for request in bundle.rollbacks:
        if request.request_id in seen_requests:
            raise BlockStoreError(
                ErrorCode.DUPLICATE_REQUEST_ID,
                "rollback request_id must be unique",
                {"request_id": request.request_id},
            )
        seen_requests.add(request.request_id)
        if request.target_snapshot_id not in known:
            raise BlockStoreError(
                ErrorCode.ROLLBACK_TARGET_NOT_FOUND,
                "rollback target snapshot does not exist",
                {
                    "request_id": request.request_id,
                    "target_snapshot_id": request.target_snapshot_id,
                },
            )
        if request.rollback_clock <= known[request.target_snapshot_id]:
            raise BlockStoreError(
                ErrorCode.CLOCK_NOT_MONOTONIC,
                "rollback clock must be strictly later than the target snapshot",
                {
                    "request_id": request.request_id,
                    "rollback_clock": request.rollback_clock,
                    "target_clock": known[request.target_snapshot_id],
                },
            )


def load_bundle(
    volume_path: str | Path,
    snapshots_path: str | Path | None = None,
    rollbacks_path: str | Path | None = None,
) -> PlanBundle:
    from .canonical import read_json_file

    volume = parse_volume_document(read_json_file(volume_path), context=str(volume_path))
    snapshots: list[SnapshotSpec] = []
    if snapshots_path is not None:
        snapshots = parse_snapshot_document(
            read_json_file(snapshots_path),
            volume,
            context=str(snapshots_path),
        )
    rollbacks: list[RollbackRequest] = []
    if rollbacks_path is not None:
        rollbacks = parse_rollback_document(
            read_json_file(rollbacks_path),
            volume,
            context=str(rollbacks_path),
        )
    bundle = PlanBundle(volume=volume, snapshots=snapshots, rollbacks=rollbacks)
    validate_plan_bundle(bundle)
    return bundle
