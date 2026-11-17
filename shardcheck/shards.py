from __future__ import annotations

import os
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .config import PolicyRule, TrainingConfig
from .errors import (
    CheckpointCorrupt,
    InjectedWriteError,
    ShardConflict,
    ShardSizeExceeded,
    ShardWriteError,
)
from .io import fsync_directory, sha256_bytes
from .model import DeterministicModel, TensorInfo


@dataclass(frozen=True)
class PhysicalShard:
    descriptor: dict[str, Any]
    payload: bytes

    @property
    def shard_id(self) -> str:
        return self.descriptor["physical_shard_id"]

    @property
    def size_bytes(self) -> int:
        return len(self.payload)


def _segments_for_range(
    tensors: Iterable[TensorInfo], start: int, end: int
) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    for tensor in tensors:
        tensor_end = tensor.offset + tensor.size_bytes
        overlap_start = max(start, tensor.offset)
        overlap_end = min(end, tensor_end)
        if overlap_start >= overlap_end:
            continue
        segments.append(
            {
                "tensor": tensor.name,
                "tensor_offset": tensor.offset,
                "tensor_size_bytes": tensor.size_bytes,
                "shard_offset": overlap_start - start,
                "size_bytes": overlap_end - overlap_start,
            }
        )
    return segments


def build_logical_shards(
    model: DeterministicModel, round_number: int
) -> list[tuple[dict[str, Any], bytes]]:
    config = model.config
    payload = model.to_bytes()
    chunk_limit = config.shard_size_limit_bytes
    chunks: list[tuple[dict[str, Any], bytes]] = []
    for chunk_index, start in enumerate(range(0, len(payload), chunk_limit)):
        end = min(start + chunk_limit, len(payload))
        chunk = payload[start:end]
        worker = chunk_index % config.workers
        logical_id = f"r{round_number:04d}-w{worker:02d}-c{chunk_index:04d}"
        descriptor = {
            "logical_shard_id": logical_id,
            "worker": worker,
            "chunk_index": chunk_index,
            "file_offset": start,
            "size_bytes": len(chunk),
            "content_fingerprint": sha256_bytes(chunk),
            "tensor_segments": _segments_for_range(model.tensors, start, end),
        }
        chunks.append((descriptor, chunk))
    return chunks


def validate_logical_shards(
    items: list[tuple[dict[str, Any], bytes]], config: TrainingConfig
) -> list[tuple[dict[str, Any], bytes, str]]:
    failures: list[tuple[dict[str, Any], bytes, str]] = []
    for descriptor, payload in items:
        if len(payload) > config.shard_size_limit_bytes:
            failures.append((descriptor, payload, "logical shard exceeds configured size limit"))
        elif descriptor["size_bytes"] != len(payload):
            failures.append((descriptor, payload, "descriptor size does not match payload"))
        elif descriptor["content_fingerprint"] != sha256_bytes(payload):
            failures.append((descriptor, payload, "descriptor fingerprint does not match payload"))
    return failures


def plan_merge_groups(
    items: list[tuple[dict[str, Any], bytes]], rule: PolicyRule
) -> list[list[tuple[dict[str, Any], bytes]]]:
    ordered = sorted(items, key=lambda item: item[0]["chunk_index"])
    if rule.merge_mode == "none":
        return [[item] for item in ordered]

    groups: list[list[tuple[dict[str, Any], bytes]]] = []
    current: list[tuple[dict[str, Any], bytes]] = []
    current_size = 0
    for item in ordered:
        size = len(item[1])
        mergeable = (
            size <= rule.merge_small_threshold_bytes
            and rule.max_merged_bytes > 0
        )
        if not mergeable:
            if current:
                groups.append(current)
                current = []
                current_size = 0
            groups.append([item])
            continue
        if current and current_size + size > rule.max_merged_bytes:
            groups.append(current)
            current = []
            current_size = 0
        current.append(item)
        current_size += size
    if current:
        groups.append(current)
    return groups


def make_physical_shard(
    group: list[tuple[dict[str, Any], bytes]], round_number: int, group_index: int
) -> tuple[PhysicalShard, dict[str, Any] | None, dict[str, Any] | None]:
    if not group:
        raise ValueError("cannot build an empty physical shard")
    payload = b"".join(item[1] for item in group)
    members: list[dict[str, Any]] = []
    offset = 0
    max_tensor_slice = 0
    logical_ids: list[str] = []
    for descriptor, member_payload in group:
        member = dict(descriptor)
        member["offset_in_physical"] = offset
        member["size_bytes"] = len(member_payload)
        member["content_fingerprint"] = sha256_bytes(member_payload)
        members.append(member)
        logical_ids.append(str(descriptor["logical_shard_id"]))
        offset += len(member_payload)
        max_tensor_slice = max(
            [max_tensor_slice]
            + [int(segment["size_bytes"]) for segment in descriptor.get("tensor_segments", [])]
        )
    physical_id = f"r{round_number:04d}-g{group_index:04d}"
    descriptor = {
        "physical_shard_id": physical_id,
        "worker": min(int(member["worker"]) for member, _ in group),
        "member_count": len(group),
        "members": members,
        "size_bytes": len(payload),
        "content_fingerprint": sha256_bytes(payload),
        "max_tensor_slice_bytes": max_tensor_slice,
        "logical_shard_ids": logical_ids,
    }
    merge_op = None
    split_op = None
    if len(group) > 1:
        merge_op = {
            "operation_id": f"MERGE-r{round_number:04d}-g{group_index:04d}",
            "type": "merge",
            "round": round_number,
            "physical_shard_id": physical_id,
            "logical_shard_ids": logical_ids,
            "member_offsets": [member["offset_in_physical"] for member in members],
            "size_bytes": len(payload),
        }
        split_op = {
            "operation_id": f"SPLIT-r{round_number:04d}-g{group_index:04d}",
            "type": "split",
            "round": round_number,
            "physical_shard_id": physical_id,
            "logical_shard_ids": logical_ids,
            "member_offsets": [member["offset_in_physical"] for member in members],
            "lossless": True,
        }
    return PhysicalShard(descriptor, payload), merge_op, split_op


def sort_physical_shards(shards: list[PhysicalShard], priority: str) -> list[PhysicalShard]:
    if priority == "tensor_size_desc":
        return sorted(shards, key=lambda shard: (-int(shard.descriptor["max_tensor_slice_bytes"]), shard.shard_id))
    if priority == "shard_size_desc":
        return sorted(shards, key=lambda shard: (-shard.size_bytes, shard.shard_id))
    if priority == "worker_then_chunk":
        return sorted(shards, key=lambda shard: (int(shard.descriptor["worker"]), shard.shard_id))
    return sorted(shards, key=lambda shard: shard.shard_id)


def estimate_metadata_bytes(shards: list[PhysicalShard]) -> int:
    return sum(4096 + 128 * int(shard.descriptor["member_count"]) for shard in shards)


class AtomicShardWriter:
    def __init__(self, directory: Path, *, delay_seconds: float = 0.0):
        self.directory = directory
        self.delay_seconds = delay_seconds
        self.directory.mkdir(parents=True, exist_ok=True)
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def _path_lock(self, shard_id: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(shard_id, threading.Lock())

    def write(
        self,
        shard: PhysicalShard,
        *,
        payload_override: bytes | None = None,
        injected_write_error: bool = False,
    ) -> dict[str, Any]:
        payload = shard.payload if payload_override is None else payload_override
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        if injected_write_error:
            raise InjectedWriteError(f"injected write failure for {shard.shard_id}")
        target = self.directory / f"{shard.shard_id}.bin"
        lock = self._path_lock(shard.shard_id)
        with lock:
            fingerprint = sha256_bytes(payload)
            if target.exists():
                existing = target.read_bytes()
                existing_fingerprint = sha256_bytes(existing)
                if existing_fingerprint == fingerprint:
                    return {
                        "physical_shard_id": shard.shard_id,
                        "status": "idempotent_skip",
                        "content_fingerprint": fingerprint,
                    }
                raise ShardConflict(
                    f"content fingerprint conflict for {shard.shard_id}: "
                    f"existing={existing_fingerprint}, incoming={fingerprint}",
                    shard_id=shard.shard_id,
                    details={"existing_fingerprint": existing_fingerprint, "incoming_fingerprint": fingerprint},
                )
            fd, temp_name = tempfile.mkstemp(prefix=f".{shard.shard_id}.tmp-", dir=self.directory)
            temp_path = Path(temp_name)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_path, target)
                fsync_directory(self.directory)
            except BaseException as exc:
                temp_path.unlink(missing_ok=True)
                if isinstance(exc, (ShardConflict, ShardWriteError)):
                    raise
                if isinstance(exc, InjectedWriteError):
                    raise
                raise ShardWriteError(
                    f"failed to write {shard.shard_id}: {exc}", shard_id=shard.shard_id
                ) from exc
            return {
                "physical_shard_id": shard.shard_id,
                "status": "committed",
                "content_fingerprint": fingerprint,
            }


def _verify_member(descriptor: dict[str, Any], payload: bytes, physical_id: str) -> None:
    offset = int(descriptor["offset_in_physical"])
    size = int(descriptor["size_bytes"])
    if offset < 0 or size < 0 or offset + size > len(payload):
        raise CheckpointCorrupt(
            f"member {descriptor.get('logical_shard_id')} lies outside {physical_id}",
            shard_id=physical_id,
        )
    member_payload = payload[offset : offset + size]
    if sha256_bytes(member_payload) != descriptor["content_fingerprint"]:
        raise CheckpointCorrupt(
            f"fingerprint mismatch for logical shard {descriptor.get('logical_shard_id')}",
            shard_id=physical_id,
        )


def read_generation_payload(
    generation_dir: Path, generation: dict[str, Any]
) -> tuple[bytes, list[dict[str, Any]], list[dict[str, Any]]]:
    logical: list[tuple[dict[str, Any], bytes]] = []
    split_operations: list[dict[str, Any]] = []
    logical_ids: set[str] = set()
    shard_dir = generation_dir / "shards"
    for physical in generation.get("shards", []):
        physical_id = str(physical["physical_shard_id"])
        path = shard_dir / f"{physical_id}.bin"
        if not path.exists():
            raise CheckpointCorrupt(f"missing physical shard file {path}", shard_id=physical_id)
        payload = path.read_bytes()
        if len(payload) != int(physical["size_bytes"]):
            raise CheckpointCorrupt(f"size mismatch for {physical_id}", shard_id=physical_id)
        if sha256_bytes(payload) != physical["content_fingerprint"]:
            raise CheckpointCorrupt(f"fingerprint mismatch for {physical_id}", shard_id=physical_id)
        members = physical.get("members", [])
        expected_offset = 0
        member_payloads: list[tuple[dict[str, Any], bytes]] = []
        for member in members:
            if int(member["offset_in_physical"]) != expected_offset:
                raise CheckpointCorrupt(f"non-contiguous member layout in {physical_id}", shard_id=physical_id)
            _verify_member(member, payload, physical_id)
            member_payload = payload[
                int(member["offset_in_physical"]) : int(member["offset_in_physical"]) + int(member["size_bytes"])
            ]
            member_payloads.append((member, member_payload))
            expected_offset += int(member["size_bytes"])
            logical_id = str(member["logical_shard_id"])
            if logical_id in logical_ids:
                raise CheckpointCorrupt(f"duplicate logical shard {logical_id}", shard_id=logical_id)
            logical_ids.add(logical_id)
            logical.append((member, member_payload))
        if expected_offset != len(payload):
            raise CheckpointCorrupt(f"member layout does not cover {physical_id}", shard_id=physical_id)
        if len(members) > 1:
            split_operations.append(
                {
                    "operation_id": f"SPLIT-{physical_id}",
                    "type": "split",
                    "physical_shard_id": physical_id,
                    "logical_shard_ids": [str(member["logical_shard_id"]) for member in members],
                    "lossless": True,
                }
            )
    logical.sort(key=lambda item: int(item[0]["chunk_index"]))
    if not logical:
        raise CheckpointCorrupt("generation contains no logical shards")
    expected_file_offset = 0
    for descriptor, member_payload in logical:
        if int(descriptor["file_offset"]) != expected_file_offset:
            raise CheckpointCorrupt(
                f"logical shard {descriptor['logical_shard_id']} has non-contiguous file offset",
                shard_id=str(descriptor["logical_shard_id"]),
            )
        if sha256_bytes(member_payload) != descriptor["content_fingerprint"]:
            raise CheckpointCorrupt(
                f"logical shard {descriptor['logical_shard_id']} fingerprint changed",
                shard_id=str(descriptor["logical_shard_id"]),
            )
        expected_file_offset += len(member_payload)
    payload = b"".join(member_payload for _, member_payload in logical)
    return payload, [item[0] for item in logical], split_operations
