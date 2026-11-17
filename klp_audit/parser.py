"""Strict parser for the indexed KLP binary container."""

from __future__ import annotations

import hashlib
import struct
import zlib
from pathlib import Path

from .constants import (
    ALGORITHM_TO_ID,
    DIRECTORY_ENTRY,
    DIRECTORY_ENTRY_SIZE,
    FILE_HEADER,
    FILE_HEADER_SIZE,
    FILE_MAGIC,
    FILE_VERSION,
    ID_TO_ALGORITHM,
    ID_TO_STAGE,
    MAX_LEVEL,
    MAX_OUTPUT_BYTES,
    MAX_RECORD_COUNT,
    MAX_SALT_BYTES,
    MAX_SESSION_ID_BYTES,
    RECORD_HEADER,
    RECORD_HEADER_SIZE,
    STAGE_TO_ID,
)
from .errors import (
    E_ALGORITHM,
    E_CRC,
    E_FORMAT,
    E_INPUT,
    E_PARAM,
    E_STAGE,
    KlpInputError,
)
from .models import Event, ParsedFile, RejectedRecord


class _RecordRejected(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _decode_session_id(raw: bytes) -> str:
    if not raw:
        raise _RecordRejected(E_PARAM, "session identifier must not be empty")
    try:
        value = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise _RecordRejected(E_FORMAT, f"session identifier is not UTF-8: {exc}") from exc
    if "\x00" in value:
        raise _RecordRejected(E_PARAM, "session identifier must not contain NUL")
    return value


def _parse_payload(
    payload: bytes,
    record_index: int,
    record_offset: int,
    max_iterations: int,
) -> Event:
    if len(payload) < RECORD_HEADER_SIZE:
        raise _RecordRejected(E_FORMAT, "record payload is shorter than its header")

    (
        payload_index,
        sync_marker,
        session_id_length,
        stage_id,
        algorithm_id,
        seq,
        iterations,
        salt_length,
        output_length,
        level,
    ) = RECORD_HEADER.unpack_from(payload, 0)

    if payload_index != record_index:
        raise _RecordRejected(
            E_FORMAT,
            f"record index mismatch: directory={record_index}, payload={payload_index}",
        )
    if session_id_length == 0 or session_id_length > MAX_SESSION_ID_BYTES:
        raise _RecordRejected(E_PARAM, "session identifier length is outside 1..128 bytes")
    if salt_length > MAX_SALT_BYTES:
        raise _RecordRejected(E_PARAM, "salt length exceeds the parser safety limit")
    expected_length = RECORD_HEADER_SIZE + session_id_length + salt_length
    if len(payload) != expected_length:
        raise _RecordRejected(
            E_FORMAT,
            f"record length mismatch: expected={expected_length}, actual={len(payload)}",
        )

    stage = ID_TO_STAGE.get(stage_id)
    if stage is None:
        raise _RecordRejected(E_STAGE, f"unknown stage identifier {stage_id}")
    algorithm = ID_TO_ALGORITHM.get(algorithm_id)
    if algorithm is None:
        raise _RecordRejected(E_ALGORITHM, f"unknown algorithm identifier {algorithm_id}")

    if seq == 0:
        raise _RecordRejected(E_PARAM, "stage sequence must be a positive integer")
    if iterations == 0 or iterations > max_iterations:
        raise _RecordRejected(
            E_PARAM,
            f"iterations must be in 1..header.max_iterations ({max_iterations})",
        )
    if output_length == 0 or output_length > MAX_OUTPUT_BYTES:
        raise _RecordRejected(E_PARAM, "output length must be a positive byte count")
    if level == 0 or level > MAX_LEVEL:
        raise _RecordRejected(E_PARAM, f"derivation level must be in 1..{MAX_LEVEL}")

    sid_start = RECORD_HEADER_SIZE
    sid_end = sid_start + session_id_length
    salt_end = sid_end + salt_length
    session_id = _decode_session_id(payload[sid_start:sid_end])
    salt = bytes(payload[sid_end:salt_end])

    return Event(
        session_id=session_id,
        stage=stage,
        algorithm=algorithm,
        seq=seq,
        iterations=iterations,
        salt=salt,
        output_length=output_length,
        level=level,
        sync_marker=sync_marker,
        record_index=record_index,
        offset=record_offset,
    )


def parse_bytes(data: bytes) -> ParsedFile:
    """Parse an in-memory KLP file.

    Record-level errors never abort the remaining directory entries. Header or
    directory truncation is fatal because no reliable record boundary remains.
    """

    file_sha256 = hashlib.sha256(data).hexdigest()
    rejected: list[RejectedRecord] = []
    events: list[Event] = []

    if len(data) < FILE_HEADER_SIZE:
        return ParsedFile(
            data_size=len(data),
            file_sha256=file_sha256,
            max_iterations=0,
            record_count=0,
            events=(),
            rejected=(),
            fatal_code=E_FORMAT,
            fatal_message="file is shorter than the KLP header",
        )

    magic, version, flags, header_size, max_iterations, record_count = FILE_HEADER.unpack_from(
        data, 0
    )
    if magic != FILE_MAGIC:
        return ParsedFile(
            data_size=len(data),
            file_sha256=file_sha256,
            max_iterations=max_iterations,
            record_count=record_count,
            events=(),
            rejected=(),
            fatal_code=E_FORMAT,
            fatal_message="invalid KLP magic",
        )
    if version != FILE_VERSION:
        return ParsedFile(
            data_size=len(data),
            file_sha256=file_sha256,
            max_iterations=max_iterations,
            record_count=record_count,
            events=(),
            rejected=(),
            fatal_code=E_FORMAT,
            fatal_message=f"unsupported KLP container version {version}",
        )
    if flags != 0:
        return ParsedFile(
            data_size=len(data),
            file_sha256=file_sha256,
            max_iterations=max_iterations,
            record_count=record_count,
            events=(),
            rejected=(),
            fatal_code=E_FORMAT,
            fatal_message="reserved header flags must be zero",
        )
    if max_iterations == 0:
        return ParsedFile(
            data_size=len(data),
            file_sha256=file_sha256,
            max_iterations=max_iterations,
            record_count=record_count,
            events=(),
            rejected=(),
            fatal_code=E_FORMAT,
            fatal_message="header.max_iterations must be positive",
        )
    if record_count > MAX_RECORD_COUNT:
        return ParsedFile(
            data_size=len(data),
            file_sha256=file_sha256,
            max_iterations=max_iterations,
            record_count=record_count,
            events=(),
            rejected=(),
            fatal_code=E_FORMAT,
            fatal_message="record count exceeds the parser safety limit",
        )

    expected_header_size = FILE_HEADER_SIZE + record_count * DIRECTORY_ENTRY_SIZE
    if header_size != expected_header_size:
        return ParsedFile(
            data_size=len(data),
            file_sha256=file_sha256,
            max_iterations=max_iterations,
            record_count=record_count,
            events=(),
            rejected=(),
            fatal_code=E_FORMAT,
            fatal_message=(
                f"header size mismatch: expected={expected_header_size}, actual={header_size}"
            ),
        )
    if len(data) < header_size:
        return ParsedFile(
            data_size=len(data),
            file_sha256=file_sha256,
            max_iterations=max_iterations,
            record_count=record_count,
            events=(),
            rejected=(),
            fatal_code=E_FORMAT,
            fatal_message="record directory is truncated",
        )

    for record_index in range(record_count):
        entry_offset = FILE_HEADER_SIZE + record_index * DIRECTORY_ENTRY_SIZE
        payload_offset, payload_length, expected_crc, _reserved = DIRECTORY_ENTRY.unpack_from(
            data, entry_offset
        )

        try:
            if payload_offset < header_size:
                raise _RecordRejected(E_FORMAT, "record offset points inside the directory")
            if payload_length < RECORD_HEADER_SIZE:
                raise _RecordRejected(E_FORMAT, "record length is shorter than its header")
            payload_end = payload_offset + payload_length
            if payload_end > len(data):
                raise _RecordRejected(E_FORMAT, "record extends beyond the end of the file")
            payload = data[payload_offset:payload_end]
            actual_crc = zlib.crc32(payload) & 0xFFFFFFFF
            if actual_crc != expected_crc:
                raise _RecordRejected(
                    E_CRC,
                    f"CRC mismatch: expected={expected_crc:08x}, actual={actual_crc:08x}",
                )
            event = _parse_payload(
                payload,
                record_index=record_index,
                record_offset=payload_offset,
                max_iterations=max_iterations,
            )
            events.append(event)
        except _RecordRejected as exc:
            rejected.append(
                RejectedRecord(
                    record_index=record_index,
                    offset=payload_offset if payload_offset <= len(data) else None,
                    code=exc.code,
                    message=exc.message,
                )
            )

    return ParsedFile(
        data_size=len(data),
        file_sha256=file_sha256,
        max_iterations=max_iterations,
        record_count=record_count,
        events=tuple(events),
        rejected=tuple(rejected),
    )


def parse_file(path: str | Path) -> tuple[ParsedFile, bytes]:
    input_path = Path(path)
    try:
        data = input_path.read_bytes()
    except OSError as exc:
        raise KlpInputError(E_INPUT, f"cannot read input file {input_path}: {exc}") from exc
    return parse_bytes(data), data


def build_payload(
    *,
    record_index: int,
    session_id: str,
    stage_id: int,
    algorithm_id: int,
    seq: int,
    iterations: int,
    salt: bytes,
    output_length: int,
    level: int,
    sync_marker: int,
) -> bytes:
    """Build one deterministic record payload for the sample generator."""

    sid = session_id.encode("utf-8")
    header = RECORD_HEADER.pack(
        record_index,
        sync_marker,
        len(sid),
        stage_id,
        algorithm_id,
        seq,
        iterations,
        len(salt),
        output_length,
        level,
    )
    return header + sid + salt


def build_file(
    payloads: list[bytes],
    *,
    max_iterations: int,
    directory_offsets: list[int] | None = None,
) -> bytes:
    """Build a KLP container from already encoded payloads."""

    header_size = FILE_HEADER_SIZE + len(payloads) * DIRECTORY_ENTRY_SIZE
    current_offset = header_size
    entries: list[tuple[int, int, int, int]] = []
    for index, payload in enumerate(payloads):
        offset = current_offset if directory_offsets is None else directory_offsets[index]
        entries.append((offset, len(payload), zlib.crc32(payload) & 0xFFFFFFFF, 0))
        current_offset += len(payload)

    header = FILE_HEADER.pack(
        FILE_MAGIC,
        FILE_VERSION,
        0,
        header_size,
        max_iterations,
        len(payloads),
    )
    directory = b"".join(DIRECTORY_ENTRY.pack(*entry) for entry in entries)
    return header + directory + b"".join(payloads)


def encode_stage(stage: str) -> int:
    # Unknown stages map to a non-whitelisted ID for malformed generator samples.
    return STAGE_TO_ID.get(stage, 250)


def encode_algorithm(algorithm: str) -> int:
    if algorithm not in ALGORITHM_TO_ID:
        return 250
    return ALGORITHM_TO_ID[algorithm]

