from __future__ import annotations

import struct
from pathlib import Path
from typing import Sequence

from replication_lab.segment import LogRecord, build_segment_bytes
from replication_lab.util import atomic_write_bytes


def legacy_records(version: int, start_seq: int, count: int, segment_id: int) -> list[LogRecord]:
    records: list[LogRecord] = []
    for index in range(count):
        seq = start_seq + index
        records.append(
            LogRecord(
                format_version=version,
                commit_seq=seq,
                txn_id=7000 + seq,
                commit_ts_ms=None if version == 1 else 1_000_000 + seq * 100,
                txn_size_bytes=None if version == 1 else 128 * (index + 1),
                segment_id=segment_id if version == 3 else None,
                segment_offset=None,
            )
        )
    return records


def write_legacy_segments(log_dir: Path) -> list[Path]:
    log_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    specs = [(1, 1, 2), (2, 3, 3), (3, 6, 2)]
    for segment_id, start_seq, count in specs:
        version = segment_id
        filename = f"seg-{segment_id:08d}.rlog"
        data = build_segment_bytes(
            legacy_records(version, start_seq, count, segment_id),
            segment_id=segment_id,
            target_version=version,
            filename=filename,
        )
        path = log_dir / filename
        atomic_write_bytes(path, data)
        paths.append(path)
    return paths


def mutate_u16(data: bytes, offset: int, value: int) -> bytes:
    result = bytearray(data)
    struct.pack_into(">H", result, offset, value)
    return bytes(result)


def mutate_u32(data: bytes, offset: int, value: int) -> bytes:
    result = bytearray(data)
    struct.pack_into(">I", result, offset, value)
    return bytes(result)


def mutate_u64(data: bytes, offset: int, value: int) -> bytes:
    result = bytearray(data)
    struct.pack_into(">Q", result, offset, value)
    return bytes(result)


def refresh_frame_crc(data: bytes, frame_offset: int, payload_length: int) -> bytes:
    import zlib

    result = bytearray(data)
    payload_start = frame_offset + 4
    payload = bytes(result[payload_start : payload_start + payload_length])
    struct.pack_into(
        ">I", result, payload_start + payload_length, zlib.crc32(payload) & 0xFFFFFFFF
    )
    return bytes(result)

