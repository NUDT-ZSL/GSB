"""Streaming, all-or-nothing SLI file validation and loading."""

from __future__ import annotations

import calendar
import codecs
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Iterator

from .config import SLISpec
from .errors import InputError, SLIValidationError
from .models import Event, SourceMetadata


_INTEGER_TEXT = re.compile(r"^[0-9]+$")


def _parse_minute(value: Any, line_no: int) -> int:
    if isinstance(value, bool):
        raise SLIValidationError("timestamp must be an integer epoch second or ISO-8601 string", line_no=line_no)
    if isinstance(value, int):
        seconds = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise SLIValidationError("timestamp cannot be empty", line_no=line_no)
        if _INTEGER_TEXT.fullmatch(text):
            seconds = int(text)
        else:
            normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
            try:
                dt = datetime.fromisoformat(normalized)
            except ValueError as exc:
                raise SLIValidationError("timestamp is not valid ISO-8601", line_no=line_no) from exc
            if dt.tzinfo is None or dt.utcoffset() is None:
                raise SLIValidationError(
                    "timestamp must include a timezone (for example Z or +08:00)", line_no=line_no
                )
            if dt.second != 0 or dt.microsecond != 0:
                raise SLIValidationError(
                    "timestamp must be aligned to an exact minute", line_no=line_no
                )
            utc_dt = dt.astimezone(timezone.utc)
            seconds = calendar.timegm(utc_dt.utctimetuple())
    else:
        raise SLIValidationError("timestamp must be an integer epoch second or ISO-8601 string", line_no=line_no)
    if seconds % 60 != 0:
        raise SLIValidationError("timestamp must be aligned to an exact minute", line_no=line_no)
    return seconds


class _RecordValidator:
    def __init__(self, sli: SLISpec, input_format: str, sha256: str) -> None:
        self._sli = sli
        self._input_format = input_format
        self._sha256 = sha256
        self.row_count = 0
        self.first_minute: int | None = None
        self.last_minute: int | None = None

    def consume(self, payload: Any, line_no: int) -> Event:
        if not isinstance(payload, dict):
            raise SLIValidationError("SLI record must be a JSON object", line_no=line_no)
        fields = (
            self._sli.timestamp_field,
            self._sli.total_requests_field,
            self._sli.failed_requests_field,
        )
        for field in fields:
            if field not in payload:
                raise SLIValidationError(
                    f"missing required field {field!r}", line_no=line_no, field=field
                )

        minute = _parse_minute(payload[self._sli.timestamp_field], line_no)
        total = payload[self._sli.total_requests_field]
        failed = payload[self._sli.failed_requests_field]
        if isinstance(total, bool) or not isinstance(total, int):
            raise SLIValidationError(
                "total request count must be an integer",
                line_no=line_no,
                field=self._sli.total_requests_field,
            )
        if isinstance(failed, bool) or not isinstance(failed, int):
            raise SLIValidationError(
                "failed request count must be an integer",
                line_no=line_no,
                field=self._sli.failed_requests_field,
            )
        if total < 0:
            raise SLIValidationError(
                "total request count cannot be negative",
                line_no=line_no,
                field=self._sli.total_requests_field,
            )
        if failed < 0:
            raise SLIValidationError(
                "failed request count cannot be negative",
                line_no=line_no,
                field=self._sli.failed_requests_field,
            )
        if failed > total:
            raise SLIValidationError(
                "failed request count cannot exceed total request count",
                line_no=line_no,
                field=self._sli.failed_requests_field,
            )
        if self.last_minute is not None:
            if minute == self.last_minute:
                raise SLIValidationError("duplicate minute timestamp", line_no=line_no)
            if minute < self.last_minute:
                raise SLIValidationError(
                    "timestamps must be strictly increasing", line_no=line_no
                )

        if self.first_minute is None:
            self.first_minute = minute
        self.last_minute = minute
        self.row_count += 1
        return Event(
            minute_start=minute,
            total_requests=total,
            failed_requests=failed,
            line_no=line_no,
        )

    def metadata(self) -> SourceMetadata:
        return SourceMetadata(
            data_sha256=self._sha256,
            row_count=self.row_count,
            first_minute=self.first_minute,
            last_minute=self.last_minute,
            input_format=self._input_format,
        )


class _JsonCursor:
    """Chunked JSON cursor that keeps only a small text window in memory."""

    def __init__(self, fh: BinaryIO, hasher: "hashlib._Hash") -> None:
        self._fh = fh
        self._hasher = hasher
        self._decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
        self._buffer = ""
        self._pos = 0
        self._eof = False
        self.line_no = 1

    def _fill(self) -> bool:
        if self._eof:
            return False
        raw = self._fh.read(65536)
        if raw:
            self._hasher.update(raw)
            try:
                text = self._decoder.decode(raw)
            except UnicodeDecodeError as exc:
                raise SLIValidationError("SLI file is not valid UTF-8", line_no=self.line_no) from exc
            if text:
                self._buffer += text
            return True
        try:
            tail = self._decoder.decode(b"", final=True)
        except UnicodeDecodeError as exc:
            raise SLIValidationError("SLI file is not valid UTF-8", line_no=self.line_no) from exc
        if tail:
            self._buffer += tail
        self._eof = True
        return bool(tail)

    def _advance(self, count: int) -> None:
        if count <= 0:
            return
        end = self._pos + count
        self.line_no += self._buffer.count("\n", self._pos, end)
        self._pos = end
        if self._pos > 65536 and self._pos * 2 > len(self._buffer):
            self._buffer = self._buffer[self._pos :]
            self._pos = 0

    def skip_ws(self) -> None:
        while True:
            while self._pos < len(self._buffer) and self._buffer[self._pos].isspace():
                self._advance(1)
            if self._pos < len(self._buffer):
                return
            if not self._fill():
                return

    def peek(self) -> str | None:
        self.skip_ws()
        if self._pos >= len(self._buffer):
            return None
        return self._buffer[self._pos]

    def expect(self, char: str) -> None:
        actual = self.peek()
        if actual != char:
            raise SLIValidationError(
                f"expected {char!r} in JSON data, found {actual!r}", line_no=self.line_no
            )
        self._advance(1)

    def decode_value(self) -> Any:
        self.skip_ws()
        decoder = json.JSONDecoder()
        while True:
            if self._pos >= len(self._buffer):
                if not self._fill():
                    raise SLIValidationError("unexpected end of JSON input", line_no=self.line_no)
            start = self._pos
            try:
                value, end = decoder.raw_decode(self._buffer, start)
            except json.JSONDecodeError as exc:
                if not self._eof:
                    if not self._fill():
                        continue
                    continue
                error_line = self.line_no + self._buffer.count("\n", start, max(start, exc.pos))
                raise SLIValidationError(
                    f"invalid JSON value: {exc.msg}", line_no=error_line
                ) from exc
            if end >= len(self._buffer) and not self._eof:
                if self._fill():
                    continue
            self._advance(end - start)
            return value

    def finish(self) -> None:
        self.skip_ws()
        if self.peek() is not None:
            raise SLIValidationError("trailing content after JSON document", line_no=self.line_no)


def _iter_json_array(cursor: _JsonCursor, validator: _RecordValidator) -> Iterator[Event]:
    cursor.expect("[")
    if cursor.peek() == "]":
        cursor.expect("]")
        return
    while True:
        cursor.skip_ws()
        line_no = cursor.line_no
        value = cursor.decode_value()
        yield validator.consume(value, line_no)
        next_char = cursor.peek()
        if next_char == ",":
            cursor.expect(",")
            continue
        if next_char == "]":
            cursor.expect("]")
            return
        raise SLIValidationError("expected ',' or ']' after JSON array record", line_no=cursor.line_no)


def _iter_json_records(cursor: _JsonCursor, validator: _RecordValidator) -> Iterator[Event]:
    first = cursor.peek()
    if first is None:
        raise SLIValidationError("SLI file is empty", line_no=1)
    if first == "[":
        yield from _iter_json_array(cursor, validator)
        cursor.finish()
        return
    if first != "{":
        raise SLIValidationError("JSON SLI document must be an array or object", line_no=cursor.line_no)

    cursor.expect("{")
    if cursor.peek() == "}":
        raise SLIValidationError("JSON SLI object must contain a records/events array", line_no=cursor.line_no)
    found_array = False
    while True:
        cursor.skip_ws()
        key_line = cursor.line_no
        key = cursor.decode_value()
        if not isinstance(key, str):
            raise SLIValidationError("JSON object key must be a string", line_no=key_line)
        cursor.expect(":")
        if key in {"records", "events", "data"}:
            if cursor.peek() != "[":
                raise SLIValidationError(f"JSON key {key!r} must contain an array", line_no=cursor.line_no)
            found_array = True
            yield from _iter_json_array(cursor, validator)
        else:
            cursor.decode_value()
        next_char = cursor.peek()
        if next_char == ",":
            cursor.expect(",")
            continue
        if next_char == "}":
            cursor.expect("}")
            break
        raise SLIValidationError("expected ',' or '}' in JSON object", line_no=cursor.line_no)
    if not found_array:
        raise SLIValidationError("JSON SLI object must contain a records/events array", line_no=1)
    cursor.finish()


class SLIEventStream:
    """Single-use iterator over a validated SLI source.

    The file is never returned as a partially loaded dataset. If any record is
    invalid, iteration raises :class:`SLIValidationError` and callers discard
    their aggregate state.
    """

    def __init__(self, path: str | Path, sli: SLISpec) -> None:
        self.path = Path(path)
        self.sli = sli
        self.metadata: SourceMetadata | None = None
        self._consumed = False

    def _detect_format(self) -> str:
        suffix = self.path.suffix.lower()
        if suffix in {".ndjson", ".jsonl"}:
            return "ndjson"
        if suffix == ".json":
            return "json_document"
        # Extensionless or unknown suffixes default to NDJSON so that the source
        # is opened exactly once even on the streaming path.
        return "ndjson"

    def __iter__(self) -> Iterator[Event]:
        if self._consumed:
            raise RuntimeError("SLIEventStream can only be iterated once")
        self._consumed = True
        if not self.path.is_file():
            raise InputError(f"SLI file does not exist: {self.path}", details={"path": str(self.path)})
        file_format = self._detect_format()
        if file_format == "ndjson":
            yield from self._iter_ndjson()
        else:
            yield from self._iter_json_document()

    def _iter_ndjson(self) -> Iterator[Event]:
        hasher = hashlib.sha256()
        validator = _RecordValidator(self.sli, "ndjson", "")
        try:
            with self.path.open("rb") as fh:
                for line_no, raw in enumerate(fh, start=1):
                    hasher.update(raw)
                    try:
                        text = raw.decode("utf-8-sig" if line_no == 1 else "utf-8")
                    except UnicodeDecodeError as exc:
                        raise SLIValidationError("SLI file is not valid UTF-8", line_no=line_no) from exc
                    if not text.strip():
                        continue
                    try:
                        payload = json.loads(text)
                    except json.JSONDecodeError as exc:
                        raise SLIValidationError(
                            f"invalid JSON record: {exc.msg}", line_no=line_no
                        ) from exc
                    yield validator.consume(payload, line_no)
        except OSError as exc:
            raise InputError(f"cannot read SLI file: {self.path}", details={"path": str(self.path)}) from exc
        self._finalize_metadata(validator, hasher.hexdigest(), "ndjson")

    def _iter_json_document(self) -> Iterator[Event]:
        hasher = hashlib.sha256()
        validator = _RecordValidator(self.sli, "json_document", "")
        try:
            with self.path.open("rb") as fh:
                cursor = _JsonCursor(fh, hasher)
                yield from _iter_json_records(cursor, validator)
        except OSError as exc:
            raise InputError(f"cannot read SLI file: {self.path}", details={"path": str(self.path)}) from exc
        if validator.row_count == 0:
            raise SLIValidationError("SLI file contains no records", line_no=1)
        self._finalize_metadata(validator, hasher.hexdigest(), "json_document")

    def _finalize_metadata(self, validator: _RecordValidator, sha256: str, input_format: str) -> None:
        if validator.row_count == 0:
            raise SLIValidationError("SLI file contains no records", line_no=1)
        validator._sha256 = sha256
        validator._input_format = input_format
        self.metadata = validator.metadata()


@dataclass(frozen=True, slots=True)
class LoadedSLI:
    events: tuple[Event, ...]
    metadata: SourceMetadata


def load_sli(path: str | Path, sli: SLISpec) -> LoadedSLI:
    """Load and validate all records. No partial result is ever returned."""

    stream = SLIEventStream(path, sli)
    events = tuple(stream)
    if stream.metadata is None:
        raise SLIValidationError("SLI file contains no records", line_no=1)
    return LoadedSLI(events=events, metadata=stream.metadata)
