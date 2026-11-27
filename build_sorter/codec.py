"""Length-prefixed segment files and lossless SQLite value encoding."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import SegmentIntegrityError, SorterError

MAGIC = b"EXTSEG01\n"
HEADER_SIZE = len(MAGIC)
_LENGTH = struct.Struct(">I")
MAX_PAYLOAD_BYTES = 0xFFFFFFFF


def segment_filename(segment_id: int) -> str:
    return f"sort-{segment_id:08d}.seg"


def segment_path(work_dir: Path, name: str) -> Path:
    return work_dir / name


def _encode_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {
            "$sqlite_type": "blob",
            "base64": base64.b64encode(value).decode("ascii"),
        }
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise SorterError(
        "UNSUPPORTED_SQLITE_VALUE",
        f"unsupported SQLite value type in build_task row: {type(value).__name__}",
    )


def _decode_value(value: Any) -> Any:
    if isinstance(value, dict):
        if value.get("$sqlite_type") == "blob" and isinstance(value.get("base64"), str):
            return base64.b64decode(value["base64"], validate=True)
        raise SegmentIntegrityError("invalid tagged value in segment record")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise SegmentIntegrityError("invalid SQLite value in segment record")


def serialize_record(input_seq: int, trigger_ts: int | float, values: list[Any]) -> bytes:
    if not isinstance(input_seq, int) or isinstance(input_seq, bool) or input_seq < 0:
        raise SorterError("INVALID_INPUT_SEQUENCE", "input sequence must be a non-negative integer")
    if (
        not isinstance(trigger_ts, (int, float))
        or isinstance(trigger_ts, bool)
        or (isinstance(trigger_ts, float) and not math.isfinite(trigger_ts))
    ):
        raise SorterError(
            "INVALID_TRIGGER_TS",
            "trigger_ts must be a finite integer or float",
            details={"input_seq": input_seq},
        )
    document = {
        "input_seq": input_seq,
        "trigger_ts": trigger_ts,
        "values": [_encode_value(value) for value in values],
    }
    payload = json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise SorterError(
            "RECORD_TOO_LARGE_FOR_FORMAT",
            "serialized record exceeds the 32-bit segment length prefix",
            details={"input_seq": input_seq, "record_bytes": len(payload)},
        )
    return payload


def deserialize_record(payload: bytes) -> tuple[int, int | float, list[Any]]:
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SegmentIntegrityError("segment record is not valid UTF-8 JSON") from exc
    if not isinstance(document, dict) or set(document) != {"input_seq", "trigger_ts", "values"}:
        raise SegmentIntegrityError("segment record has an invalid envelope")
    input_seq = document["input_seq"]
    trigger_ts = document["trigger_ts"]
    values = document["values"]
    if not isinstance(input_seq, int) or isinstance(input_seq, bool) or input_seq < 0:
        raise SegmentIntegrityError("segment record has an invalid input sequence")
    if (
        not isinstance(trigger_ts, (int, float))
        or isinstance(trigger_ts, bool)
        or (isinstance(trigger_ts, float) and not math.isfinite(trigger_ts))
    ):
        raise SegmentIntegrityError("segment record has an invalid trigger timestamp")
    if not isinstance(values, list):
        raise SegmentIntegrityError("segment record values must be a list")
    try:
        decoded_values = [_decode_value(value) for value in values]
    except (ValueError, TypeError) as exc:
        raise SegmentIntegrityError("segment record contains an invalid encoded value") from exc
    return input_seq, trigger_ts, decoded_values


@dataclass(frozen=True, slots=True)
class SegmentRecord:
    input_seq: int
    trigger_ts: int | float
    payload: bytes
    offset_after: int

    @property
    def size_bytes(self) -> int:
        return len(self.payload)


class SegmentWriter:
    """Write one sorted run as length-prefixed JSON records."""

    def __init__(self, work_dir: Path, segment_id: int) -> None:
        self.work_dir = work_dir
        self.segment_id = segment_id
        self.name = segment_filename(segment_id)
        self.path = segment_path(work_dir, self.name)
        self._file = open(self.path, "wb")
        self._sha256 = hashlib.sha256()
        self._closed = False
        self._aborted = False
        self._file.write(MAGIC)
        self._sha256.update(MAGIC)
        self.record_count = 0
        self.file_bytes = HEADER_SIZE
        self.first_trigger_ts: int | float | None = None
        self.last_trigger_ts: int | float | None = None
        self.min_input_seq: int | None = None
        self.max_input_seq: int | None = None
        self.first_record_bytes = 0
        self.max_record_bytes = 0

    def write(self, record: SegmentRecord) -> None:
        if self._closed or self._aborted:
            raise RuntimeError("segment writer is closed")
        payload = record.payload
        length = len(payload)
        if length > MAX_PAYLOAD_BYTES:
            raise SorterError(
                "RECORD_TOO_LARGE_FOR_FORMAT",
                "serialized record exceeds the segment length prefix",
                details={"input_seq": record.input_seq, "record_bytes": length},
            )
        prefix = _LENGTH.pack(length)
        self._file.write(prefix)
        self._file.write(payload)
        self._sha256.update(prefix)
        self._sha256.update(payload)
        self.file_bytes += len(prefix) + length
        self.record_count += 1
        if self.first_trigger_ts is None:
            self.first_trigger_ts = record.trigger_ts
            self.first_record_bytes = length
        self.last_trigger_ts = record.trigger_ts
        if self.min_input_seq is None or record.input_seq < self.min_input_seq:
            self.min_input_seq = record.input_seq
        if self.max_input_seq is None or record.input_seq > self.max_input_seq:
            self.max_input_seq = record.input_seq
        self.max_record_bytes = max(self.max_record_bytes, length)

    def close(self) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("segment writer already closed")
        if self._aborted:
            raise RuntimeError("segment writer was aborted")
        self._file.flush()
        os.fsync(self._file.fileno())
        self._file.close()
        self._closed = True
        if self.record_count == 0:
            raise SegmentIntegrityError("attempted to create an empty segment")
        return {
            "segment_id": self.segment_id,
            "name": self.name,
            "record_count": self.record_count,
            "size_bytes": self.file_bytes,
            "first_trigger_ts": self.first_trigger_ts,
            "last_trigger_ts": self.last_trigger_ts,
            "min_input_seq": self.min_input_seq,
            "max_input_seq": self.max_input_seq,
            "first_record_bytes": self.first_record_bytes,
            "max_record_bytes": self.max_record_bytes,
            "sha256": self._sha256.hexdigest(),
            "consumed_records": 0,
            "consumed_offset": HEADER_SIZE,
            "deleted": False,
        }

    def abort(self) -> None:
        if self._closed or self._aborted:
            return
        self._aborted = True
        try:
            self._file.close()
        finally:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass

    def __enter__(self) -> SegmentWriter:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if exc_type is not None:
            self.abort()


class SegmentReader:
    """Read a segment from a recoverable record boundary."""

    def __init__(
        self,
        path: Path,
        *,
        expected_records: int,
        start_records: int = 0,
        start_offset: int = HEADER_SIZE,
        segment_id: int | None = None,
    ) -> None:
        self.path = path
        self.segment_id = segment_id
        self.expected_records = expected_records
        self.consumed_records = start_records
        self.next_offset = start_offset
        self._file = open(path, "rb")
        header = self._file.read(HEADER_SIZE)
        if header != MAGIC:
            self._file.close()
            raise SegmentIntegrityError(
                "segment magic header mismatch",
                path=str(path),
            )
        if not (0 <= start_records <= expected_records):
            self._file.close()
            raise SegmentIntegrityError(
                "invalid segment resume record count",
                path=str(path),
                start_records=start_records,
                expected_records=expected_records,
            )
        if not (HEADER_SIZE <= start_offset):
            self._file.close()
            raise SegmentIntegrityError(
                "invalid segment resume offset",
                path=str(path),
                start_offset=start_offset,
            )
        self._file.seek(start_offset)

    def read(self) -> SegmentRecord | None:
        if self.consumed_records >= self.expected_records:
            return None
        length_bytes = self._file.read(4)
        if len(length_bytes) != 4:
            raise SegmentIntegrityError(
                "truncated segment record length",
                path=str(self.path),
                record_index=self.consumed_records,
            )
        length = _LENGTH.unpack(length_bytes)[0]
        payload = self._file.read(length)
        if len(payload) != length:
            raise SegmentIntegrityError(
                "truncated segment record payload",
                path=str(self.path),
                record_index=self.consumed_records,
            )
        input_seq, trigger_ts, _values = deserialize_record(payload)
        self.consumed_records += 1
        self.next_offset = self._file.tell()
        return SegmentRecord(
            input_seq=input_seq,
            trigger_ts=trigger_ts,
            payload=payload,
            offset_after=self.next_offset,
        )

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> SegmentReader:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


def scan_segment(path: Path) -> dict[str, Any]:
    """Fully scan a segment and return independently computed metadata."""

    if not path.is_file():
        raise SegmentIntegrityError("segment file is missing", path=str(path))
    sha256 = hashlib.sha256()
    record_count = 0
    first_trigger_ts: int | float | None = None
    last_trigger_ts: int | float | None = None
    min_input_seq: int | None = None
    max_input_seq: int | None = None
    first_record_bytes = 0
    max_record_bytes = 0
    try:
        with open(path, "rb") as segment:
            header = segment.read(HEADER_SIZE)
            if header != MAGIC:
                raise SegmentIntegrityError("segment magic header mismatch", path=str(path))
            sha256.update(header)
            while True:
                length_bytes = segment.read(4)
                if not length_bytes:
                    break
                if len(length_bytes) != 4:
                    raise SegmentIntegrityError(
                        "truncated segment record length",
                        path=str(path),
                    )
                length = _LENGTH.unpack(length_bytes)[0]
                payload = segment.read(length)
                if len(payload) != length:
                    raise SegmentIntegrityError(
                        "truncated segment record payload",
                        path=str(path),
                    )
                sha256.update(length_bytes)
                sha256.update(payload)
                input_seq, trigger_ts, _values = deserialize_record(payload)
                record_count += 1
                if first_trigger_ts is None:
                    first_trigger_ts = trigger_ts
                    first_record_bytes = length
                last_trigger_ts = trigger_ts
                min_input_seq = (
                    input_seq if min_input_seq is None else min(min_input_seq, input_seq)
                )
                max_input_seq = (
                    input_seq if max_input_seq is None else max(max_input_seq, input_seq)
                )
                max_record_bytes = max(max_record_bytes, length)
            trailing = segment.read(1024)
            if trailing:
                raise SegmentIntegrityError(
                    "unexpected trailing bytes after the last segment record",
                    path=str(path),
                )
    except OSError as exc:
        raise SegmentIntegrityError(
            "unable to read segment file",
            path=str(path),
            error=str(exc),
        ) from exc
    return {
        "record_count": record_count,
        "size_bytes": path.stat().st_size,
        "first_trigger_ts": first_trigger_ts,
        "last_trigger_ts": last_trigger_ts,
        "min_input_seq": min_input_seq,
        "max_input_seq": max_input_seq,
        "first_record_bytes": first_record_bytes,
        "max_record_bytes": max_record_bytes,
        "sha256": sha256.hexdigest(),
    }
