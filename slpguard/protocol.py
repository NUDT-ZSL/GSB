"""Strict binary codec for the local Session Link Protocol (SLP)."""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Any, Iterable

from .errors import (
    KNOWN_TRUTH_MASK,
    ErrorCode,
    ResourceStop,
    SLPError,
)

FILE_MAGIC = b"SLP1"
RECORD_MAGIC = b"MSG1"
SUPPORTED_VERSIONS = (1, 2)
PHASE_TABLE_VERSION = 1
CHECKPOINT_SCHEMA_VERSION = 1

FILE_HEADER = struct.Struct("<4sBBHI")
RECORD_HEADER = struct.Struct("<4sIII4BII")
FILE_HEADER_SIZE = FILE_HEADER.size
RECORD_HEADER_SIZE = RECORD_HEADER.size

FILE_MAGIC_OFFSET = 0
FILE_VERSION_OFFSET = 4
FILE_FLAGS_OFFSET = 5
FILE_COUNT_OFFSET = 6
FILE_TOTAL_LENGTH_OFFSET = 8

RECORD_MAGIC_OFFSET = 0
RECORD_SYNC_ID_OFFSET = 4
RECORD_SESSION_OFFSET = 8
RECORD_SEQUENCE_OFFSET = 12
RECORD_PHASE_OFFSET = 16
RECORD_DIRECTION_OFFSET = 17
RECORD_EXT_OFFSET = 18
RECORD_RESERVED_OFFSET = 19
RECORD_TRUTH_OFFSET = 20
RECORD_PAYLOAD_LENGTH_OFFSET = 24


class Phase(IntEnum):
    HELLO = 1
    AUTH = 2
    NEGOTIATE = 3
    DATA = 4
    CLOSE = 5


PHASE_BY_CODE = {int(phase): phase for phase in Phase}
PHASE_NAMES = {int(phase): phase.name for phase in Phase}


class Direction(IntEnum):
    CLIENT_TO_SERVER = 0
    SERVER_TO_CLIENT = 1


class Extension(IntEnum):
    RENEGOTIATE = 0x01
    HALF_CLOSE = 0x02


KNOWN_EXTENSIONS = int(Extension.RENEGOTIATE) | int(Extension.HALF_CLOSE)


@dataclass(frozen=True)
class SLPRecord:
    sync_id: int
    session_id: int
    sequence: int
    phase: int
    direction: int
    extensions: int
    truth_mask: int
    payload: bytes
    offset: int = 0

    @property
    def phase_name(self) -> str:
        phase = PHASE_BY_CODE.get(int(self.phase))
        return phase.name if phase else f"UNKNOWN({self.phase})"

    @property
    def direction_name(self) -> str:
        try:
            return Direction(self.direction).name
        except ValueError:
            return f"UNKNOWN({self.direction})"

    def to_dict(self, *, include_payload: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "sync_id": self.sync_id,
            "session_id": self.session_id,
            "sequence": self.sequence,
            "phase": self.phase_name,
            "phase_code": int(self.phase),
            "direction": self.direction_name,
            "direction_code": int(self.direction),
            "extensions": int(self.extensions),
            "truth_mask": self.truth_mask,
            "payload_length": len(self.payload),
            "offset": self.offset,
        }
        if include_payload:
            result["payload_hex"] = self.payload.hex()
        return result


@dataclass(frozen=True)
class ParsedSLP:
    version: int
    flags: int
    declared_message_count: int
    declared_total_length: int
    records: tuple[SLPRecord, ...]


class ResourceMeter:
    """Real wall-clock and per-session step accounting for one file."""

    def __init__(
        self,
        *,
        started_at: float,
        max_steps_per_session: int | None,
        max_file_seconds: float | None,
        clock: Any,
    ) -> None:
        self.started_at = started_at
        self.max_steps_per_session = max_steps_per_session
        self.max_file_seconds = max_file_seconds
        self.clock = clock
        self.steps_by_session: dict[int, int] = {}
        self.processed_messages = 0
        self.total_messages: int | None = None
        self.records: list[SLPRecord] = []

    def elapsed(self) -> float:
        return max(0.0, float(self.clock() - self.started_at))

    def check_time(self) -> None:
        if self.max_file_seconds is None:
            return
        elapsed = self.elapsed()
        if elapsed > self.max_file_seconds:
            raise ResourceStop(
                ErrorCode.E_TIMEOUT,
                (
                    f"wall-clock limit exceeded: elapsed={elapsed:.9f}s "
                    f"limit={self.max_file_seconds:.9f}s"
                ),
                processed_messages=self.processed_messages,
                total_messages=self.total_messages,
            )

    def consume(self, record: SLPRecord) -> None:
        self.check_time()
        previous = self.steps_by_session.get(record.session_id, 0)
        if (
            self.max_steps_per_session is not None
            and previous + 1 > self.max_steps_per_session
        ):
            raise ResourceStop(
                ErrorCode.E_STEPS,
                (
                    f"session {record.session_id} step limit exceeded before "
                    f"sequence {record.sequence}: limit={self.max_steps_per_session}"
                ),
                processed_messages=self.processed_messages,
                total_messages=self.total_messages,
                session_id=record.session_id,
                sequence=record.sequence,
            )
        self.steps_by_session[record.session_id] = previous + 1
        self.processed_messages += 1
        self.records.append(record)
        self.check_time()


def file_id_for_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _check_record_header_bounds(offset: int, data_len: int) -> None:
    if offset + RECORD_HEADER_SIZE > data_len:
        raise SLPError(
            ErrorCode.E_BOUNDS,
            (
                f"record header exceeds file: offset={offset}, "
                f"needed={RECORD_HEADER_SIZE}, available={max(0, data_len - offset)}"
            ),
            offset=offset,
        )


def parse_bytes(data: bytes | bytearray | memoryview, meter: ResourceMeter | None = None) -> ParsedSLP:
    """Parse one SLP file strictly.

    Structural and semantic format failures raise ``SLPError``.  Resource
    limits, when supplied through ``meter``, raise ``ResourceStop`` so a batch
    can isolate the affected file.
    """

    raw = bytes(data)
    if meter is not None:
        meter.check_time()
    if len(raw) < FILE_HEADER_SIZE:
        raise SLPError(
            ErrorCode.E_BOUNDS,
            f"file shorter than the {FILE_HEADER_SIZE}-byte header",
            offset=len(raw),
        )

    magic, version, flags, message_count, total_length = FILE_HEADER.unpack_from(raw, 0)
    if magic != FILE_MAGIC:
        raise SLPError(ErrorCode.E_MAGIC, f"expected magic {FILE_MAGIC!r}", offset=0)
    if version not in SUPPORTED_VERSIONS:
        raise SLPError(
            ErrorCode.E_VERSION,
            f"version {version} is not in whitelist {SUPPORTED_VERSIONS}",
            offset=FILE_VERSION_OFFSET,
        )
    if flags != 0:
        raise SLPError(
            ErrorCode.E_EXT,
            f"unknown file-level flags 0x{flags:02x}",
            offset=FILE_FLAGS_OFFSET,
        )
    if total_length < FILE_HEADER_SIZE or total_length > len(raw):
        raise SLPError(
            ErrorCode.E_BOUNDS,
            (
                f"declared total length {total_length} is outside actual file "
                f"length {len(raw)}"
            ),
            offset=FILE_TOTAL_LENGTH_OFFSET,
        )
    if total_length != len(raw):
        raise SLPError(
            ErrorCode.E_FORMAT,
            (
                f"declared total length {total_length} does not equal actual "
                f"file length {len(raw)}"
            ),
            offset=FILE_TOTAL_LENGTH_OFFSET,
        )
    if meter is not None:
        meter.total_messages = message_count

    records: list[SLPRecord] = []
    seen_sync_ids: set[int] = set()
    offset = FILE_HEADER_SIZE
    for index in range(message_count):
        if meter is not None:
            meter.check_time()
        _check_record_header_bounds(offset, len(raw))
        (
            record_magic,
            sync_id,
            session_id,
            sequence,
            phase,
            direction,
            extensions,
            reserved,
            truth_mask,
            payload_length,
        ) = RECORD_HEADER.unpack_from(raw, offset)

        if record_magic != RECORD_MAGIC:
            raise SLPError(
                ErrorCode.E_FORMAT,
                f"record {index} has bad sync marker {record_magic!r}",
                offset=offset + RECORD_MAGIC_OFFSET,
            )
        if sync_id == 0:
            raise SLPError(
                ErrorCode.E_BOUNDS,
                "sync_id must be non-zero",
                offset=offset + RECORD_SYNC_ID_OFFSET,
            )
        if sync_id in seen_sync_ids:
            raise SLPError(
                ErrorCode.E_FORMAT,
                f"duplicate sync_id {sync_id}",
                offset=offset + RECORD_SYNC_ID_OFFSET,
            )
        if session_id == 0:
            raise SLPError(
                ErrorCode.E_BOUNDS,
                "session_id must be non-zero",
                offset=offset + RECORD_SESSION_OFFSET,
            )
        if sequence == 0:
            raise SLPError(
                ErrorCode.E_BOUNDS,
                "sequence must be non-zero",
                offset=offset + RECORD_SEQUENCE_OFFSET,
            )
        if phase not in PHASE_BY_CODE:
            raise SLPError(
                ErrorCode.E_PHASE,
                f"unknown phase code {phase}",
                offset=offset + RECORD_PHASE_OFFSET,
                session_id=session_id,
                sequence=sequence,
                sync_ids=(sync_id,),
            )
        if direction not in (int(Direction.CLIENT_TO_SERVER), int(Direction.SERVER_TO_CLIENT)):
            raise SLPError(
                ErrorCode.E_EXT,
                f"unknown direction code {direction}",
                offset=offset + RECORD_DIRECTION_OFFSET,
                session_id=session_id,
                sequence=sequence,
                sync_ids=(sync_id,),
            )
        if reserved != 0:
            raise SLPError(
                ErrorCode.E_EXT,
                f"reserved byte must be zero, got 0x{reserved:02x}",
                offset=offset + RECORD_RESERVED_OFFSET,
                session_id=session_id,
                sequence=sequence,
                sync_ids=(sync_id,),
            )
        if extensions & ~KNOWN_EXTENSIONS:
            raise SLPError(
                ErrorCode.E_EXT,
                f"unknown extension bits 0x{extensions & ~KNOWN_EXTENSIONS:02x}",
                offset=offset + RECORD_EXT_OFFSET,
                session_id=session_id,
                sequence=sequence,
                sync_ids=(sync_id,),
            )
        if (extensions & int(Extension.RENEGOTIATE)) and phase != int(Phase.NEGOTIATE):
            raise SLPError(
                ErrorCode.E_EXT,
                "RENEGOTIATE extension is only valid on NEGOTIATE",
                offset=offset + RECORD_EXT_OFFSET,
                session_id=session_id,
                sequence=sequence,
                sync_ids=(sync_id,),
            )
        if (extensions & int(Extension.HALF_CLOSE)) and phase != int(Phase.CLOSE):
            raise SLPError(
                ErrorCode.E_EXT,
                "HALF_CLOSE extension is only valid on CLOSE",
                offset=offset + RECORD_EXT_OFFSET,
                session_id=session_id,
                sequence=sequence,
                sync_ids=(sync_id,),
            )
        if truth_mask & ~KNOWN_TRUTH_MASK:
            raise SLPError(
                ErrorCode.E_FORMAT,
                f"truth mask has unknown business-code bits 0x{truth_mask:08x}",
                offset=offset + RECORD_TRUTH_OFFSET,
                session_id=session_id,
                sequence=sequence,
                sync_ids=(sync_id,),
            )
        if truth_mask & 0x01:
            raise SLPError(
                ErrorCode.E_FORMAT,
                "truth mask must not declare E_OK as an expected failure",
                offset=offset + RECORD_TRUTH_OFFSET,
                session_id=session_id,
                sequence=sequence,
                sync_ids=(sync_id,),
            )

        payload_start = offset + RECORD_HEADER_SIZE
        payload_end = payload_start + payload_length
        if payload_end > len(raw):
            raise SLPError(
                ErrorCode.E_BOUNDS,
                (
                    f"payload length {payload_length} at record offset {offset} "
                    f"ends at {payload_end}, beyond file length {len(raw)}"
                ),
                offset=offset + RECORD_PAYLOAD_LENGTH_OFFSET,
                session_id=session_id,
                sequence=sequence,
                sync_ids=(sync_id,),
            )

        record = SLPRecord(
            sync_id=sync_id,
            session_id=session_id,
            sequence=sequence,
            phase=phase,
            direction=direction,
            extensions=extensions,
            truth_mask=truth_mask,
            payload=raw[payload_start:payload_end],
            offset=offset,
        )
        if meter is not None:
            meter.consume(record)
        seen_sync_ids.add(sync_id)
        records.append(record)
        offset = payload_end

    if offset != len(raw):
        raise SLPError(
            ErrorCode.E_FORMAT,
            (
                f"{message_count} record(s) consumed {offset} bytes but file has "
                f"{len(raw)}; trailing bytes or count mismatch"
            ),
            offset=offset,
        )

    return ParsedSLP(
        version=version,
        flags=flags,
        declared_message_count=message_count,
        declared_total_length=total_length,
        records=tuple(records),
    )


def parse_file(path: str | Path, meter: ResourceMeter | None = None) -> ParsedSLP:
    return parse_bytes(Path(path).read_bytes(), meter=meter)


def encode_file(
    records: Iterable[SLPRecord],
    *,
    version: int = 1,
    flags: int = 0,
) -> bytes:
    """Encode records without semantic validation.

    Keeping the encoder permissive lets tests and the injector deliberately
    construct malformed protocol samples.  The parser remains the trust
    boundary.
    """

    materialized = list(records)
    body = bytearray()
    for record in materialized:
        payload = bytes(record.payload)
        body.extend(
            RECORD_HEADER.pack(
                RECORD_MAGIC,
                int(record.sync_id),
                int(record.session_id),
                int(record.sequence),
                int(record.phase),
                int(record.direction),
                int(record.extensions),
                0,
                int(record.truth_mask),
                len(payload),
            )
        )
        body.extend(payload)
    total_length = FILE_HEADER_SIZE + len(body)
    header = FILE_HEADER.pack(FILE_MAGIC, int(version), int(flags), len(materialized), total_length)
    return header + bytes(body)


def canonical_record_key(record: SLPRecord) -> tuple[int, int, int, int]:
    return (record.session_id, record.sequence, record.sync_id, record.offset)
