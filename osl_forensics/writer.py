"""Deterministic OSL writer used by the fixed-seed generator and tests."""

from __future__ import annotations

import hashlib
import hmac as hmac_mod
import struct
from dataclasses import dataclass, field
from typing import Any

from .policy import OSL40_LAYOUT, Policy


@dataclass
class SegmentSpec:
    name: str
    payload: bytes = b""
    segment_type: int = 1
    compression: int = 0x00
    algo: int | None = None
    refs: list[str] | tuple[str, ...] | None = None
    encoded_payload_offset: int | None = None
    encoded_declared_length: int | None = None
    digest: bytes | None = None
    digest_declared_length: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def resolved_payload(self) -> bytes:
        if self.refs is not None:
            return "\n".join(self.refs).encode("utf-8")
        return self.payload


@dataclass
class BuiltOsl:
    data: bytes
    records: list[dict[str, int | str]]
    header_size: int
    table_offset: int
    table_end: int

    def record(self, name: str) -> dict[str, int | str]:
        for item in self.records:
            if item["name"] == name:
                return item
        raise KeyError(name)


def build_osl(
    specs: list[SegmentSpec],
    policy: Policy | None = None,
    *,
    format_version: int = 1,
    table_offset: int | None = None,
    count_override: int | None = None,
    total_length_override: int | None = None,
    magic: bytes = b"OSL1",
    sync_marker: bytes | None = b"OSLSYNC1",
) -> BuiltOsl:
    used_policy = policy or Policy()
    layout = used_policy.header_layout
    table_start = layout.size if table_offset is None else table_offset

    prepared: list[dict[str, Any]] = []
    table_size = 0
    for spec in specs:
        payload = spec.resolved_payload()
        algo = spec.algo
        if algo is None:
            algo = {0x00: 0, 0x01: 1, 0x02: 2, 0x10: 0}.get(spec.compression, 0)
        digest = spec.digest
        if digest is None:
            if spec.compression == 0x01 or algo == 1:
                digest = hashlib.sha256(payload).digest()
            elif spec.compression == 0x02 or algo == 2:
                digest = hmac_mod.new(used_policy.hmac_key, payload, hashlib.sha256).digest()
            else:
                digest = b""
        declared_digest_length = (
            spec.digest_declared_length
            if spec.digest_declared_length is not None
            else len(digest)
        )
        record_size = _record_size(spec.name, len(digest), used_policy.digest_length_prefix)
        prepared.append(
            {
                "spec": spec,
                "payload": payload,
                "algo": algo,
                "digest": digest,
                "digest_declared_length": declared_digest_length,
                "record_size": record_size,
            }
        )
        table_size += record_size

    table_end = table_start + table_size
    physical_cursor = table_end
    for item in prepared:
        payload = item["payload"]
        item["physical_payload_offset"] = physical_cursor
        physical_cursor += len(payload)

    natural_total = physical_cursor
    total_length = total_length_override if total_length_override is not None else natural_total
    if total_length < natural_total:
        total_length = natural_total

    blob = bytearray(total_length)
    blob[0:4] = magic
    struct.pack_into("<H", blob, 4, format_version)
    struct.pack_into("<I", blob, 6, count_override if count_override is not None else len(specs))
    struct.pack_into("<Q", blob, 10, table_start)
    struct.pack_into("<Q", blob, 18, total_length)
    # Reserved bytes are already zero-initialized.
    if sync_marker is not None:
        if len(sync_marker) > layout.reserved_size:
            raise ValueError("sync marker does not fit reserved area")
        blob[26 : 26 + len(sync_marker)] = sync_marker

    cursor = table_start
    records: list[dict[str, int | str]] = []
    for item in prepared:
        spec: SegmentSpec = item["spec"]
        payload: bytes = item["payload"]
        digest: bytes = item["digest"]
        record_start = cursor
        name_bytes = spec.name.encode("utf-8")
        name_length_offset = cursor
        struct.pack_into("<H", blob, name_length_offset, len(name_bytes))
        name_offset = name_length_offset + 2
        blob[name_offset : name_offset + len(name_bytes)] = name_bytes

        type_offset = name_offset + len(name_bytes)
        compression_offset = type_offset + 1
        payload_field = compression_offset + 1
        length_field = payload_field + 8
        algo_offset = length_field + 8
        digest_length_offset = algo_offset + 1
        digest_offset = digest_length_offset + (2 if used_policy.digest_length_prefix else 0)

        blob[type_offset] = spec.segment_type
        blob[compression_offset] = spec.compression
        wire_offset = (
            spec.encoded_payload_offset
            if spec.encoded_payload_offset is not None
            else item["physical_payload_offset"]
        )
        wire_length = (
            spec.encoded_declared_length
            if spec.encoded_declared_length is not None
            else len(payload)
        )
        struct.pack_into("<Q", blob, payload_field, wire_offset)
        struct.pack_into("<Q", blob, length_field, wire_length)
        blob[algo_offset] = item["algo"]
        if used_policy.digest_length_prefix:
            struct.pack_into("<H", blob, digest_length_offset, item["digest_declared_length"])
        blob[digest_offset : digest_offset + len(digest)] = digest

        physical_offset = int(item["physical_payload_offset"])
        blob[physical_offset : physical_offset + len(payload)] = payload

        records.append(
            {
                "name": spec.name,
                "record_offset": record_start,
                "name_offset": name_offset,
                "type_offset": type_offset,
                "compression_offset": compression_offset,
                "payload_offset_field_offset": payload_field,
                "declared_length_field_offset": length_field,
                "algo_offset": algo_offset,
                "digest_length_offset": digest_length_offset
                if used_policy.digest_length_prefix
                else -1,
                "digest_offset": digest_offset,
                "digest_length": len(digest),
                "physical_payload_offset": physical_offset,
                "wire_payload_offset": wire_offset,
                "wire_declared_length": wire_length,
            }
        )
        cursor += int(item["record_size"])

    return BuiltOsl(bytes(blob), records, layout.size, table_start, table_end)


def _record_size(name: str, digest_length: int, prefix: bool) -> int:
    base = 2 + len(name.encode("utf-8")) + 1 + 1 + 8 + 8 + 1
    if prefix:
        base += 2
    return base + digest_length
