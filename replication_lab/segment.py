"""Versioned master-log segment encoding and strict decoding.

Wire format (all integers are big-endian)
-----------------------------------------
Segment header (24 bytes)::

    magic[8] = b"RLOGSEG\\x01"
    container_version:u16 = 1
    format_version:u16  (highest record version in the segment)
    segment_id:u32
    record_count:u32
    header_crc32:u32    (CRC of the preceding 20 bytes)

Record frame::

    payload_length:u32
    payload[payload_length]
    payload_crc32:u32

Payload v1::

    format_version:u16, commit_seq:u64, txn_id:u32

Payload v2 adds::

    commit_ts_ms:u64, txn_size_bytes:u64

Payload v3 adds::

    segment_id:u32, segment_offset:u32, source_format_version:u16

The all-ones u64 value is an explicit UNKNOWN marker.  This is essential when
a v1/v2 record is upgraded to v3: no timestamp is invented.  A v3 record can
therefore carry the current format version while still declaring its timestamp
unknown.

The v3-only source_format_version is provenance metadata.  When v1/v2 records
are upgraded, it lets decode/display/replay retain the original version marker
without pretending the newly written record is still stored in the old wire
layout.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import BinaryIO, Iterable, Iterator, Sequence

from .errors import FormatError
from .util import sha256_bytes

MAGIC = b"RLOGSEG\x01"
CONTAINER_VERSION = 1
CURRENT_FORMAT_VERSION = 3
SUPPORTED_FORMAT_VERSIONS = frozenset({1, 2, 3})
UNKNOWN_U64 = (1 << 64) - 1

HEADER_SIZE = 24
MAX_PAYLOAD_SIZE = 1024 * 1024

HEADER_PREFIX = struct.Struct(">8sHHII")
HEADER = struct.Struct(">8sHHIII")
U32 = struct.Struct(">I")
BASE = struct.Struct(">HQI")
V2_EXTRA = struct.Struct(">QQ")
V3_EXTRA = struct.Struct(">IIH")


@dataclass(frozen=True)
class LogRecord:
    """A logical master log record preserving its stored format version."""

    format_version: int
    commit_seq: int
    txn_id: int
    commit_ts_ms: int | None = None
    txn_size_bytes: int | None = None
    segment_id: int | None = None
    segment_offset: int | None = None
    source_format_version: int | None = None

    def to_dict(self) -> dict[str, int | None]:
        return {
            "format_version": self.format_version,
            "commit_seq": self.commit_seq,
            "txn_id": self.txn_id,
            "commit_ts_ms": self.commit_ts_ms,
            "txn_size_bytes": self.txn_size_bytes,
            "segment_id": self.segment_id,
            "segment_offset": self.segment_offset,
            "source_format_version": self.original_format_version,
        }

    @property
    def delay_is_observable(self) -> bool:
        return self.commit_ts_ms is not None

    @property
    def original_format_version(self) -> int:
        return self.source_format_version or self.format_version


@dataclass(frozen=True)
class Segment:
    filename: str
    path: str | None
    data: bytes
    digest: str
    segment_id: int
    format_version: int
    declared_record_count: int
    records: tuple[LogRecord, ...]

    @property
    def record_count(self) -> int:
        return len(self.records)

    @property
    def versions(self) -> tuple[int, ...]:
        return tuple(sorted({record.format_version for record in self.records}))

    @property
    def original_versions(self) -> tuple[int, ...]:
        return tuple(sorted({record.original_format_version for record in self.records}))

    @property
    def version_counts(self) -> dict[int, int]:
        counts: dict[int, int] = {}
        for record in self.records:
            counts[record.format_version] = counts.get(record.format_version, 0) + 1
        return {version: counts[version] for version in sorted(counts)}

    def to_manifest_entry(self) -> dict[str, object]:
        return {
            "filename": self.filename,
            "digest": self.digest,
            "segment_id": self.segment_id,
            "format_version": self.format_version,
            "versions": list(self.versions),
            "original_versions": list(self.original_versions),
            "version_counts": {str(k): v for k, v in self.version_counts.items()},
            "record_count": self.record_count,
            "first_commit_seq": self.records[0].commit_seq if self.records else None,
            "last_commit_seq": self.records[-1].commit_seq if self.records else None,
        }


def _format_error(
    filename: str,
    offset: int | None,
    message: str,
    **details: object,
) -> FormatError:
    return FormatError(message, segment=filename, offset=offset, details=details)


def _require_uint(value: int, bits: int, field: str, filename: str, offset: int | None) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise _format_error(filename, offset, f"{field} must be an integer", field=field)
    if value < 0 or value >= (1 << bits):
        raise _format_error(
            filename,
            offset,
            f"{field} does not fit in u{bits}",
            field=field,
            value=value,
        )


def _encode_u64(value: int | None, field: str, filename: str, offset: int | None) -> int:
    if value is None:
        return UNKNOWN_U64
    _require_uint(value, 64, field, filename, offset)
    if value == UNKNOWN_U64:
        raise _format_error(
            filename,
            offset,
            f"{field} uses the reserved UNKNOWN sentinel",
            field=field,
        )
    return value


def frame_payload_length(version: int) -> int:
    if version == 1:
        return BASE.size
    if version == 2:
        return BASE.size + V2_EXTRA.size
    if version == 3:
        return BASE.size + V2_EXTRA.size + V3_EXTRA.size
    raise FormatError(f"unsupported record format version {version}")


def encode_record_payload(
    record: LogRecord,
    *,
    segment_id: int | None = None,
    frame_offset: int | None = None,
    filename: str = "<memory>",
) -> bytes:
    version = record.format_version
    if version not in SUPPORTED_FORMAT_VERSIONS:
        raise _format_error(
            filename,
            frame_offset,
            f"unsupported format version {version}",
            version=version,
        )
    _require_uint(record.commit_seq, 64, "commit_seq", filename, frame_offset)
    _require_uint(record.txn_id, 32, "txn_id", filename, frame_offset)
    if record.commit_seq <= 0:
        raise _format_error(filename, frame_offset, "commit_seq must be positive")
    if record.txn_id <= 0:
        raise _format_error(filename, frame_offset, "txn_id must be positive")

    payload = bytearray(BASE.pack(version, record.commit_seq, record.txn_id))
    if version >= 2:
        payload.extend(
            V2_EXTRA.pack(
                _encode_u64(record.commit_ts_ms, "commit_ts_ms", filename, frame_offset),
                _encode_u64(
                    record.txn_size_bytes,
                    "txn_size_bytes",
                    filename,
                    frame_offset,
                ),
            )
        )
    if version == 3:
        resolved_segment_id = segment_id if segment_id is not None else record.segment_id
        resolved_offset = frame_offset if frame_offset is not None else record.segment_offset
        if resolved_segment_id is None:
            raise _format_error(filename, frame_offset, "v3 record requires segment_id")
        if resolved_offset is None:
            raise _format_error(filename, frame_offset, "v3 record requires segment_offset")
        _require_uint(
            resolved_segment_id, 32, "segment_id", filename, frame_offset
        )
        _require_uint(resolved_offset, 32, "segment_offset", filename, frame_offset)
        source_version = record.source_format_version or version
        _require_uint(source_version, 16, "source_format_version", filename, frame_offset)
        if source_version not in SUPPORTED_FORMAT_VERSIONS:
            raise _format_error(
                filename,
                frame_offset,
                f"unsupported source_format_version {source_version}",
                source_format_version=source_version,
            )
        payload.extend(V3_EXTRA.pack(resolved_segment_id, resolved_offset, source_version))
    return bytes(payload)


def encode_record_frame(
    record: LogRecord,
    *,
    segment_id: int | None = None,
    frame_offset: int | None = None,
    filename: str = "<memory>",
) -> bytes:
    payload = encode_record_payload(
        record,
        segment_id=segment_id,
        frame_offset=frame_offset,
        filename=filename,
    )
    return U32.pack(len(payload)) + payload + U32.pack(zlib.crc32(payload) & 0xFFFFFFFF)


def decode_record_payload(
    payload: bytes,
    *,
    filename: str = "<memory>",
    frame_offset: int | None = None,
) -> LogRecord:
    if len(payload) < BASE.size:
        raise _format_error(
            filename,
            frame_offset,
            "record payload is shorter than the v1 header",
            payload_length=len(payload),
        )
    version, commit_seq, txn_id = BASE.unpack_from(payload, 0)
    if version not in SUPPORTED_FORMAT_VERSIONS:
        raise _format_error(
            filename,
            frame_offset,
            f"unsupported format version {version}",
            version=version,
        )
    expected_length = frame_payload_length(version)
    if commit_seq <= 0:
        raise _format_error(filename, frame_offset, "commit_seq must be positive")
    if txn_id <= 0:
        raise _format_error(filename, frame_offset, "txn_id must be positive")
    if len(payload) != expected_length:
        raise _format_error(
            filename,
            frame_offset,
            f"payload length {len(payload)} does not match v{version} length {expected_length}",
            version=version,
            payload_length=len(payload),
            expected_payload_length=expected_length,
        )

    commit_ts_ms: int | None = None
    txn_size_bytes: int | None = None
    segment_id: int | None = None
    segment_offset: int | None = None
    source_version: int | None = None
    if version >= 2:
        commit_ts_raw, size_raw = V2_EXTRA.unpack_from(payload, BASE.size)
        commit_ts_ms = None if commit_ts_raw == UNKNOWN_U64 else commit_ts_raw
        txn_size_bytes = None if size_raw == UNKNOWN_U64 else size_raw
    if version == 3:
        segment_id, segment_offset, source_version = V3_EXTRA.unpack_from(
            payload, BASE.size + V2_EXTRA.size
        )
        if source_version not in SUPPORTED_FORMAT_VERSIONS:
            raise _format_error(
                filename,
                frame_offset,
                f"unsupported source_format_version {source_version}",
                source_format_version=source_version,
            )
    else:
        source_version = version
    return LogRecord(
        format_version=version,
        commit_seq=commit_seq,
        txn_id=txn_id,
        commit_ts_ms=commit_ts_ms,
        txn_size_bytes=txn_size_bytes,
        segment_id=segment_id,
        segment_offset=segment_offset,
        source_format_version=source_version,
    )


def parse_segment(
    data: bytes,
    *,
    filename: str = "<memory>",
    path: str | None = None,
) -> Segment:
    if len(data) < HEADER_SIZE:
        raise _format_error(filename, 0, "segment is shorter than its header")
    (
        magic,
        container_version,
        declared_format_version,
        segment_id,
        declared_record_count,
        header_crc,
    ) = HEADER.unpack_from(data, 0)
    if magic != MAGIC:
        raise _format_error(filename, 0, "bad segment magic", reason="BAD_MAGIC")
    if container_version != CONTAINER_VERSION:
        raise _format_error(
            filename,
            8,
            f"unsupported segment container version {container_version}",
            container_version=container_version,
        )
    if declared_format_version not in SUPPORTED_FORMAT_VERSIONS:
        raise _format_error(
            filename,
            10,
            f"unsupported segment format version {declared_format_version}",
            version=declared_format_version,
        )
    actual_header_crc = zlib.crc32(HEADER_PREFIX.pack(
        magic,
        container_version,
        declared_format_version,
        segment_id,
        declared_record_count,
    )) & 0xFFFFFFFF
    if header_crc != actual_header_crc:
        raise _format_error(filename, HEADER_SIZE - 4, "segment header CRC mismatch")
    if segment_id <= 0:
        raise _format_error(filename, 12, "segment_id must be positive")

    records: list[LogRecord] = []
    offset = HEADER_SIZE
    previous_seq: int | None = None
    data_length = len(data)
    while offset < data_length:
        frame_offset = offset
        if data_length - offset < U32.size:
            raise _format_error(filename, offset, "truncated record length prefix")
        (payload_length,) = U32.unpack_from(data, offset)
        offset += U32.size
        if payload_length <= 0 or payload_length > MAX_PAYLOAD_SIZE:
            raise _format_error(
                filename,
                frame_offset,
                f"invalid record payload length {payload_length}",
                payload_length=payload_length,
            )
        frame_end = offset + payload_length + U32.size
        if frame_end > data_length:
            raise _format_error(
                filename,
                frame_offset,
                "record extends beyond the segment file",
                payload_length=payload_length,
                available=data_length - offset,
            )
        payload = data[offset : offset + payload_length]
        (stored_crc,) = U32.unpack_from(data, offset + payload_length)
        if (zlib.crc32(payload) & 0xFFFFFFFF) != stored_crc:
            raise _format_error(
                filename,
                frame_offset,
                "record payload CRC mismatch",
                payload_length=payload_length,
            )
        record = decode_record_payload(
            payload, filename=filename, frame_offset=frame_offset
        )
        if previous_seq is not None and record.commit_seq <= previous_seq:
            raise _format_error(
                filename,
                frame_offset,
                "commit_seq is not strictly increasing",
                previous_commit_seq=previous_seq,
                commit_seq=record.commit_seq,
            )
        if record.format_version == 3:
            if record.segment_id != segment_id:
                raise _format_error(
                    filename,
                    frame_offset,
                    "v3 record segment_id does not match segment header",
                    record_segment_id=record.segment_id,
                    segment_id=segment_id,
                )
            if record.segment_offset != frame_offset:
                raise _format_error(
                    filename,
                    frame_offset,
                    "v3 record segment_offset does not match its actual frame offset",
                    record_segment_offset=record.segment_offset,
                    actual_offset=frame_offset,
                )
        if record.commit_seq <= 0:
            raise _format_error(filename, frame_offset, "commit_seq must be positive")
        if record.txn_id <= 0:
            raise _format_error(filename, frame_offset, "txn_id must be positive")
        records.append(record)
        previous_seq = record.commit_seq
        offset = frame_end

    if offset != data_length:
        raise _format_error(filename, offset, "unexpected trailing segment bytes")
    if len(records) != declared_record_count:
        raise _format_error(
            filename,
            HEADER_SIZE,
            "segment record count does not match header",
            declared_record_count=declared_record_count,
            decoded_record_count=len(records),
        )
    actual_format_version = max(
        (record.format_version for record in records), default=declared_format_version
    )
    if declared_format_version != actual_format_version:
        raise _format_error(
            filename,
            10,
            "segment format_version does not match the highest record version",
            declared_format_version=declared_format_version,
            actual_format_version=actual_format_version,
        )
    return Segment(
        filename=filename,
        path=path,
        data=data,
        digest=sha256_bytes(data),
        segment_id=segment_id,
        format_version=declared_format_version,
        declared_record_count=declared_record_count,
        records=tuple(records),
    )


def read_segment(path: Path) -> Segment:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise _format_error(path.name, 0, f"cannot read segment: {exc}") from exc
    return parse_segment(data, filename=path.name, path=str(path))


def read_segment_bytes(data: bytes, filename: str, *, path: str | None = None) -> Segment:
    return parse_segment(data, filename=filename, path=path)


def _header_bytes(
    segment_id: int,
    format_version: int,
    record_count: int,
) -> bytes:
    prefix = HEADER_PREFIX.pack(
        MAGIC,
        CONTAINER_VERSION,
        format_version,
        segment_id,
        record_count,
    )
    return prefix + U32.pack(zlib.crc32(prefix) & 0xFFFFFFFF)


def upgrade_record(
    record: LogRecord,
    *,
    target_version: int = CURRENT_FORMAT_VERSION,
    segment_id: int | None = None,
    frame_offset: int | None = None,
) -> LogRecord:
    if target_version not in SUPPORTED_FORMAT_VERSIONS:
        raise FormatError(f"unsupported target format version {target_version}")
    if target_version < record.format_version:
        raise FormatError(
            f"cannot downgrade record v{record.format_version} to v{target_version}"
        )
    if target_version == 1:
        return LogRecord(
            format_version=1,
            commit_seq=record.commit_seq,
            txn_id=record.txn_id,
            source_format_version=record.original_format_version,
        )
    if target_version == 2:
        return LogRecord(
            format_version=2,
            commit_seq=record.commit_seq,
            txn_id=record.txn_id,
            commit_ts_ms=record.commit_ts_ms,
            txn_size_bytes=record.txn_size_bytes,
            source_format_version=record.original_format_version,
        )
    return LogRecord(
        format_version=3,
        commit_seq=record.commit_seq,
        txn_id=record.txn_id,
        commit_ts_ms=record.commit_ts_ms,
        txn_size_bytes=record.txn_size_bytes,
        segment_id=segment_id if segment_id is not None else record.segment_id,
        segment_offset=frame_offset if frame_offset is not None else record.segment_offset,
        source_format_version=record.original_format_version,
    )


def build_segment_bytes(
    records: Sequence[LogRecord],
    *,
    segment_id: int,
    target_version: int | None = None,
    filename: str = "<memory>",
) -> bytes:
    """Serialize records, resolving v3 ids/offsets from their final layout."""

    if segment_id <= 0 or segment_id >= (1 << 32):
        raise _format_error(filename, 0, "segment_id must fit in u32 and be positive")
    prepared: list[LogRecord] = []
    previous_seq: int | None = None
    for record in records:
        current = (
            upgrade_record(record, target_version=target_version, segment_id=segment_id)
            if target_version is not None
            else record
        )
        if previous_seq is not None and current.commit_seq <= previous_seq:
            raise _format_error(
                filename,
                None,
                "commit_seq is not strictly increasing",
                previous_commit_seq=previous_seq,
                commit_seq=current.commit_seq,
            )
        previous_seq = current.commit_seq
        prepared.append(current)

    format_version = max(
        (record.format_version for record in prepared),
        default=target_version or CURRENT_FORMAT_VERSION,
    )
    frame_offset = HEADER_SIZE
    frames: list[bytes] = []
    for record in prepared:
        if record.format_version == 3:
            encoded = encode_record_frame(
                record,
                segment_id=segment_id,
                frame_offset=frame_offset,
                filename=filename,
            )
        else:
            encoded = encode_record_frame(record, filename=filename)
        frames.append(encoded)
        frame_offset += len(encoded)
    return _header_bytes(segment_id, format_version, len(prepared)) + b"".join(frames)


def upgrade_segment_bytes(segment: Segment, *, target_version: int = CURRENT_FORMAT_VERSION) -> bytes:
    return build_segment_bytes(
        segment.records,
        segment_id=segment.segment_id,
        target_version=target_version,
        filename=segment.filename,
    )


def validate_collection(segments: Sequence[Segment]) -> None:
    seen_segment_ids: set[int] = set()
    previous_seq: int | None = None
    previous_segment: str | None = None
    for segment in sorted(segments, key=lambda item: (item.segment_id, item.filename)):
        if segment.segment_id in seen_segment_ids:
            raise FormatError(
                "duplicate segment_id in collection",
                segment=segment.filename,
                offset=12,
                details={"segment_id": segment.segment_id},
            )
        seen_segment_ids.add(segment.segment_id)
        for record in segment.records:
            if previous_seq is not None and record.commit_seq <= previous_seq:
                raise FormatError(
                    "commit_seq is not strictly increasing across segments",
                    segment=segment.filename,
                    offset=record.segment_offset or HEADER_SIZE,
                    details={
                        "previous_commit_seq": previous_seq,
                        "commit_seq": record.commit_seq,
                        "previous_segment": previous_segment,
                    },
                )
            previous_seq = record.commit_seq
        previous_segment = segment.filename


def collect_records(segments: Sequence[Segment]) -> list[LogRecord]:
    records = [record for segment in segments for record in segment.records]
    records.sort(key=lambda item: item.commit_seq)
    return records


def iter_segment_paths(log_dir: Path) -> Iterator[Path]:
    yield from sorted(log_dir.glob("*.rlog"), key=lambda item: item.name)


def segment_set_digest(entries: Iterable[dict[str, object]]) -> str:
    canonical = []
    for entry in sorted(entries, key=lambda item: str(item.get("filename", ""))):
        canonical.append(
            {
                "filename": entry.get("filename"),
                "digest": entry.get("digest"),
                "format_version": entry.get("format_version"),
                "versions": entry.get("versions"),
            }
        )
    from .util import canonical_json_bytes

    return sha256_bytes(canonical_json_bytes(canonical))


def record_semantic_digest(records: Iterable[LogRecord]) -> str:
    """Digest logical record content, independent of wire format and segment layout.

    This lets persisted replay progress survive a record-level format upgrade.
    The source version is included because it is part of the retained provenance.
    """

    canonical = [
        {
            "commit_seq": record.commit_seq,
            "txn_id": record.txn_id,
            "commit_ts_ms": record.commit_ts_ms,
            "txn_size_bytes": record.txn_size_bytes,
            "source_format_version": record.original_format_version,
        }
        for record in sorted(records, key=lambda item: item.commit_seq)
    ]
    from .util import canonical_json_bytes

    return sha256_bytes(canonical_json_bytes(canonical))
