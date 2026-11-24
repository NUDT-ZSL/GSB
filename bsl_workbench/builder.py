from __future__ import annotations

import hashlib
import hmac as hmac_module
import struct
from dataclasses import dataclass, field
from typing import Optional

from .codes import (
    DEFAULT_HMAC_KEY,
    HEADER_PREFIX_FORMAT,
    HEADER_PREFIX_SIZE,
    LITERAL_HEADER_SIZE,
    MAGIC,
    RECORD_FIXED_FORMAT,
    RECORD_FIXED_SIZE,
    RESERVED_SIZE,
    UINT64_MAX,
)


@dataclass
class SegmentSpec:
    name: str
    payload: bytes = b""
    seg_type: int = 0x01
    compression: int = 0
    digest: Optional[bytes] = None

    def declared_digest(self, hmac_key: bytes = DEFAULT_HMAC_KEY) -> bytes:
        if self.digest is not None:
            if len(self.digest) != 32:
                raise ValueError("digest override must be exactly 32 bytes")
            return self.digest
        if self.compression == 1:
            return hashlib.sha256(self.payload).digest()
        if self.compression == 2:
            return hmac_module.new(hmac_key, self.payload, hashlib.sha256).digest()
        return bytes(32)


@dataclass
class RecordLayout:
    name: str
    record_offset: int
    name_offset: int
    name_length: int
    type_offset: int
    compression_offset: int
    payload_offset_field: int
    payload_length_field: int
    digest_offset: int
    payload_offset: int
    payload_length: int


@dataclass
class BuiltBSL:
    data: bytes
    records: list[RecordLayout] = field(default_factory=list)
    table_offset: int = LITERAL_HEADER_SIZE
    table_end: int = 0

    def for_name(self, name: str) -> RecordLayout:
        for record in self.records:
            if record.name == name:
                return record
        raise KeyError(name)

    def for_name_all(self, name: str) -> list[RecordLayout]:
        return [record for record in self.records if record.name == name]


def pack_header(
    version: int,
    count: int,
    table_offset: int,
    declared_total: int,
    *,
    magic: bytes = MAGIC,
    reserved: bytes = bytes(RESERVED_SIZE),
) -> bytes:
    if len(magic) != 4:
        raise ValueError("magic must be four bytes")
    prefix = struct.pack(
        HEADER_PREFIX_FORMAT,
        magic,
        version,
        count,
        table_offset,
        declared_total,
    )
    return prefix + reserved


def pack_record(
    name: str,
    seg_type: int,
    compression: int,
    payload_offset: int,
    payload_length: int,
    digest: bytes,
) -> bytes:
    name_bytes = name.encode("utf-8")
    if len(name_bytes) > 65535:
        raise ValueError("segment name UTF-8 length exceeds uint16")
    if len(digest) != 32:
        raise ValueError("digest must be 32 bytes")
    return (
        struct.pack("<H", len(name_bytes))
        + name_bytes
        + struct.pack(
            RECORD_FIXED_FORMAT,
            seg_type,
            compression,
            payload_offset,
            payload_length,
            digest,
        )
    )


def build_bsl(
    specs: list[SegmentSpec],
    *,
    version: int = 1,
    hmac_key: bytes = DEFAULT_HMAC_KEY,
    table_offset: int = LITERAL_HEADER_SIZE,
    header_reserved: bytes = bytes(RESERVED_SIZE),
) -> BuiltBSL:
    """Build a contiguous valid BSL file.

    Payloads are laid out immediately after the segment table.  Malformed
    samples can be produced by patching fields in the returned bytearray.
    """
    count = len(specs)
    encoded_names = [spec.name.encode("utf-8") for spec in specs]
    table_size = sum(2 + len(name) + RECORD_FIXED_SIZE for name in encoded_names)
    table_end = table_offset + table_size

    offsets: list[int] = []
    cursor = table_end
    for spec in specs:
        offsets.append(cursor)
        cursor += len(spec.payload)
    total_size = cursor
    if total_size > UINT64_MAX:
        raise ValueError("BSL exceeds uint64 address space")

    output = bytearray(total_size)
    header = pack_header(
        version,
        count,
        table_offset,
        total_size,
        reserved=header_reserved,
    )
    output[0 : len(header)] = header

    records: list[RecordLayout] = []
    position = table_offset
    for index, spec in enumerate(specs):
        name_bytes = encoded_names[index]
        name_offset = position + 2
        fixed_start = name_offset + len(name_bytes)
        type_offset = fixed_start
        compression_offset = fixed_start + 1
        offset_field = fixed_start + 2
        length_field = fixed_start + 10
        digest_offset = fixed_start + 18
        payload_offset = offsets[index]
        payload_length = len(spec.payload)

        record = pack_record(
            spec.name,
            spec.seg_type,
            spec.compression,
            payload_offset,
            payload_length,
            spec.declared_digest(hmac_key),
        )
        output[position : position + len(record)] = record
        output[payload_offset : payload_offset + payload_length] = spec.payload

        records.append(
            RecordLayout(
                name=spec.name,
                record_offset=position,
                name_offset=name_offset,
                name_length=len(name_bytes),
                type_offset=type_offset,
                compression_offset=compression_offset,
                payload_offset_field=offset_field,
                payload_length_field=length_field,
                digest_offset=digest_offset,
                payload_offset=payload_offset,
                payload_length=payload_length,
            )
        )
        position += len(record)

    return BuiltBSL(data=bytes(output), records=records, table_offset=table_offset, table_end=table_end)


def patch_u16(data: bytearray, offset: int, value: int) -> None:
    struct.pack_into("<H", data, offset, value)


def patch_u32(data: bytearray, offset: int, value: int) -> None:
    struct.pack_into("<I", data, offset, value)


def patch_u64(data: bytearray, offset: int, value: int) -> None:
    struct.pack_into("<Q", data, offset, value)


def patch_bytes(data: bytearray, offset: int, value: bytes) -> None:
    data[offset : offset + len(value)] = value


def raw_header(
    *,
    magic: bytes = MAGIC,
    version: int = 1,
    count: int = 0,
    table_offset: int = LITERAL_HEADER_SIZE,
    declared_total: Optional[int] = None,
    total_size: int = LITERAL_HEADER_SIZE,
) -> bytes:
    if declared_total is None:
        declared_total = total_size
    return pack_header(version, count, table_offset, declared_total)


def raw_record_with_name_bytes(
    name_bytes: bytes,
    *,
    seg_type: int = 1,
    compression: int = 0,
    payload_offset: int = 0,
    payload_length: int = 0,
    digest: bytes = bytes(32),
) -> bytes:
    return (
        struct.pack("<H", len(name_bytes))
        + name_bytes
        + struct.pack(
            RECORD_FIXED_FORMAT,
            seg_type,
            compression,
            payload_offset,
            payload_length,
            digest,
        )
    )


def minimal_raw_file(records: list[bytes], *, table_offset: int = LITERAL_HEADER_SIZE) -> bytes:
    table = b"".join(records)
    total = table_offset + len(table)
    header = pack_header(1, len(records), table_offset, total)
    if len(header) < table_offset:
        header += bytes(table_offset - len(header))
    return header + table


__all__ = [
    "HEADER_PREFIX_SIZE",
    "SegmentSpec",
    "BuiltBSL",
    "RecordLayout",
    "build_bsl",
    "pack_header",
    "pack_record",
    "patch_u16",
    "patch_u32",
    "patch_u64",
    "patch_bytes",
    "raw_header",
    "raw_record_with_name_bytes",
    "minimal_raw_file",
]
