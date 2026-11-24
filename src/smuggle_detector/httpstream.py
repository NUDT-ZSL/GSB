"""Connection-direction reassembly and HTTP message boundary parsing."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Iterable

from .constants import (
    E_BOUNDARY_RESIDUE,
    E_CHUNK_SIZE,
    E_CHUNK_TRAILER,
    E_CL_TE,
    E_DUP_HEADER_CONFLICT,
    E_MSG_GAP,
    E_MIXED_CONN,
)
from .errors import DeadlineExceeded
from .models import MessageRecord

_HEADER_NAME_RE = re.compile(rb"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_REQUEST_LINE_RE = re.compile(
    rb"^[A-Z!#$%&'*+\-.^_`|~0-9]+ [^ \r\n]+ HTTP/\d\.\d$"
)
_RESPONSE_LINE_RE = re.compile(rb"^HTTP/\d\.\d [0-9]{3}(?: [^\r\n]*)?$")
_HEX_SIZE_RE = re.compile(rb"^[0-9A-Fa-f]{1,16}$")
_OWS_RE = re.compile(r"[ \t]+")


class Deadline:
    """A cooperative wall-clock deadline checked inside processing loops."""

    def __init__(self, timeout_ms: int) -> None:
        self.timeout_ms = timeout_ms
        self.started = time.perf_counter()
        self.deadline = self.started + max(timeout_ms, 0) / 1000.0

    def check(self) -> None:
        if time.perf_counter() > self.deadline:
            raise DeadlineExceeded(
                "connection processing exceeded real wall-clock timeout",
                detail={"timeout_ms": self.timeout_ms},
            )

    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self.started) * 1000.0


@dataclass(frozen=True)
class Header:
    name: str
    value: str
    start: int
    end: int
    value_start: int
    value_end: int


@dataclass(frozen=True)
class Candidate:
    code: str
    direction: str
    offset: int
    end_offset: int
    message_index: int | None
    sync_id: str | None
    evidence: str

    def to_doc(self) -> dict:
        return {
            "code": self.code,
            "direction": self.direction,
            "byte_offset": self.offset,
            "range": [self.offset, self.end_offset],
            "message_index": self.message_index,
            "sync_id": self.sync_id,
            "evidence": self.evidence,
        }


@dataclass
class ParsedMessage:
    index: int
    direction: str
    start_offset: int
    end_offset: int
    complete: bool
    framing: str
    sync_id: str | None
    declared_length: int | None = None
    aborted_at: int | None = None

    def to_doc(self) -> dict:
        return {
            "index": self.index,
            "direction": self.direction,
            "start_offset": self.start_offset,
            "end_offset": self.end_offset,
            "complete": self.complete,
            "framing": self.framing,
            "sync_id": self.sync_id,
            "declared_length": self.declared_length,
            "aborted_at": self.aborted_at,
        }


@dataclass
class StreamResult:
    connection_id: str
    direction: str
    received_records: int
    unique_records: int
    seq_min: int | None
    seq_max: int | None
    missing_ranges: list[list[int]] = field(default_factory=list)
    duplicates: list[dict] = field(default_factory=list)
    duplicate_count: int = 0
    messages: list[ParsedMessage] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)

    def to_doc(self, hits: list[dict] | None = None) -> dict:
        return {
            "direction": self.direction,
            "records": {
                "received": self.received_records,
                "unique": self.unique_records,
                "seq_min": self.seq_min,
                "seq_max": self.seq_max,
            },
            "missing_ranges": self.missing_ranges,
            "duplicates": self.duplicates,
            "duplicate_count": self.duplicate_count,
            "messages": [message.to_doc() for message in self.messages],
            "errors": sorted(
                self.errors,
                key=lambda item: (
                    item.get("byte_offset", -1),
                    item["code"],
                ),
            ),
            "hits": hits or [],
        }


def _normalized_header_value(value: str) -> str:
    return _OWS_RE.sub(" ", value.strip(" \t"))


def _header_value(headers: Iterable[Header], name: str) -> str | None:
    for header in headers:
        if header.name.lower() == name:
            return header.value
    return None


def _all_headers(headers: Iterable[Header], name: str) -> list[Header]:
    return [header for header in headers if header.name.lower() == name]


def _start_line_matches(line: bytes, direction: str) -> bool:
    if direction == "c2s":
        return bool(_REQUEST_LINE_RE.fullmatch(line))
    return bool(_RESPONSE_LINE_RE.fullmatch(line))


def _next_message_start(data: bytes, position: int, direction: str) -> int | None:
    cursor = position
    while cursor < len(data):
        if cursor == 0 or data[cursor - 2 : cursor] == b"\r\n":
            line_end = data.find(b"\r\n", cursor)
            if line_end < 0:
                return None
            if _start_line_matches(data[cursor:line_end], direction):
                return cursor
        cursor += 1
    return None


def _parse_chunked_body(
    data: bytes,
    body_start: int,
    *,
    direction: str,
    message_index: int,
    sync_id: str | None,
    deadline: Deadline,
    candidates: list[Candidate],
) -> tuple[int, bool, int | None]:
    position = body_start
    while True:
        deadline.check()
        line_end = data.find(b"\r\n", position)
        if line_end < 0:
            candidates.append(
                Candidate(
                    E_BOUNDARY_RESIDUE,
                    direction,
                    position,
                    len(data),
                    message_index,
                    sync_id,
                    "chunk-size line is not terminated",
                )
            )
            return len(data), False, position

        size_line = data[position:line_end]
        size_token = size_line.split(b";", 1)[0]
        if not _HEX_SIZE_RE.fullmatch(size_token):
            candidates.append(
                Candidate(
                    E_CHUNK_SIZE,
                    direction,
                    position,
                    line_end + 2,
                    message_index,
                    sync_id,
                    f"invalid hexadecimal chunk size {size_token!r}",
                )
            )
            return len(data), False, position

        chunk_size = int(size_token, 16)
        chunk_data_start = line_end + 2
        if chunk_size == 0:
            trailer_position = chunk_data_start
            while True:
                deadline.check()
                if data[trailer_position : trailer_position + 2] == b"\r\n":
                    return trailer_position + 2, True, None
                trailer_end = data.find(b"\r\n", trailer_position)
                if trailer_end < 0:
                    candidates.append(
                        Candidate(
                            E_BOUNDARY_RESIDUE,
                            direction,
                            trailer_position,
                            len(data),
                            message_index,
                            sync_id,
                            "chunk trailer section is not terminated",
                        )
                    )
                    return len(data), False, trailer_position
                trailer_line = data[trailer_position:trailer_end]
                if not trailer_line or b":" not in trailer_line:
                    candidates.append(
                        Candidate(
                            E_CHUNK_TRAILER,
                            direction,
                            trailer_position,
                            trailer_end + 2,
                            message_index,
                            sync_id,
                            f"invalid chunk trailer line {trailer_line!r}",
                        )
                    )
                trailer_position = trailer_end + 2

        chunk_data_end = chunk_data_start + chunk_size
        if chunk_data_end + 2 > len(data):
            candidates.append(
                Candidate(
                    E_BOUNDARY_RESIDUE,
                    direction,
                    chunk_data_start,
                    len(data),
                    message_index,
                    sync_id,
                    "declared chunk body is truncated",
                )
            )
            return len(data), False, chunk_data_start
        if data[chunk_data_end : chunk_data_end + 2] != b"\r\n":
            candidates.append(
                Candidate(
                    E_CHUNK_SIZE,
                    direction,
                    chunk_data_end,
                    min(chunk_data_end + 2, len(data)),
                    message_index,
                    sync_id,
                    "chunk body is not followed by CRLF",
                )
            )
            return len(data), False, chunk_data_end
        position = chunk_data_end + 2


def _parse_segment(
    data: bytes,
    *,
    base_offset: int,
    connection_id: str,
    direction: str,
    start_index: int,
    deadline: Deadline,
) -> tuple[list[ParsedMessage], list[Candidate], list[dict]]:
    messages: list[ParsedMessage] = []
    candidates: list[Candidate] = []
    errors: list[dict] = []
    position = 0
    message_index = start_index

    while position < len(data):
        deadline.check()
        absolute_position = base_offset + position
        header_break = data.find(b"\r\n\r\n", position)
        if header_break < 0:
            candidates.append(
                Candidate(
                    E_BOUNDARY_RESIDUE,
                    direction,
                    absolute_position,
                    base_offset + len(data),
                    None,
                    None,
                    "bytes cannot be assigned to a complete HTTP header block",
                )
            )
            break

        first_line_end = data.find(b"\r\n", position)
        if first_line_end < 0 or first_line_end > header_break:
            first_line_end = header_break
        if not _start_line_matches(data[position:first_line_end], direction):
            next_start = _next_message_start(data, position + 1, direction)
            if next_start is None:
                candidates.append(
                    Candidate(
                        E_BOUNDARY_RESIDUE,
                        direction,
                        absolute_position,
                        base_offset + len(data),
                        None,
                        None,
                        "bytes before the next HTTP message are unassignable",
                    )
                )
                break
            candidates.append(
                Candidate(
                    E_BOUNDARY_RESIDUE,
                    direction,
                    absolute_position,
                    base_offset + next_start,
                    None,
                    None,
                    "bytes before the next HTTP message are unassignable",
                )
            )
            position = next_start
            continue

        headers: list[Header] = []
        cursor = first_line_end + 2
        header_error = False
        while cursor < header_break:
            deadline.check()
            line_end = data.find(b"\r\n", cursor)
            if line_end < 0 or line_end > header_break:
                line_end = header_break
            line = data[cursor:line_end]
            name_bytes, separator, value_bytes = line.partition(b":")
            if not separator or not _HEADER_NAME_RE.fullmatch(name_bytes):
                candidates.append(
                    Candidate(
                        E_BOUNDARY_RESIDUE,
                        direction,
                        base_offset + cursor,
                        base_offset + line_end,
                        message_index,
                        None,
                        f"malformed header line {line!r}",
                    )
                )
                header_error = True
            else:
                value_start = cursor + len(name_bytes) + 1
                while (
                    value_start < line_end
                    and data[value_start : value_start + 1] in (b" ", b"\t")
                ):
                    value_start += 1
                value_end = line_end
                while value_end > value_start and data[value_end - 1 : value_end] in (
                    b" ",
                    b"\t",
                ):
                    value_end -= 1
                name = name_bytes.decode("ascii")
                value = data[value_start:value_end].decode("latin-1")
                headers.append(
                    Header(
                        name=name,
                        value=value,
                        start=base_offset + cursor,
                        end=base_offset + line_end,
                        value_start=base_offset + value_start,
                        value_end=base_offset + value_end,
                    )
                )
            cursor = line_end + 2

        sync_id = _header_value(headers, "x-det-sync-id")
        sync_connection = _header_value(headers, "x-det-sync-conn")
        if sync_connection is not None and sync_connection != connection_id:
            sync_header = _all_headers(headers, "x-det-sync-conn")[0]
            errors.append(
                {
                    "code": E_MIXED_CONN,
                    "direction": direction,
                    "byte_offset": sync_header.start,
                    "range": [sync_header.start, sync_header.end],
                    "message_index": message_index,
                    "sync_id": sync_id,
                    "observed_connection_id": sync_connection,
                    "expected_connection_id": connection_id,
                }
            )

        first_values: dict[str, str] = {}
        for header in headers:
            key = header.name.lower()
            normalized = _normalized_header_value(header.value)
            if key in first_values and first_values[key] != normalized:
                candidates.append(
                    Candidate(
                        E_DUP_HEADER_CONFLICT,
                        direction,
                        header.start,
                        header.end,
                        message_index,
                        sync_id,
                        f"conflicting repeated header {header.name}",
                    )
                )
            else:
                first_values.setdefault(key, normalized)

        content_length_headers = _all_headers(headers, "content-length")
        transfer_encoding_headers = _all_headers(headers, "transfer-encoding")
        if content_length_headers and transfer_encoding_headers:
            earliest = min(
                [header.start for header in content_length_headers]
                + [header.start for header in transfer_encoding_headers]
            )
            latest = max(
                [header.end for header in content_length_headers]
                + [header.end for header in transfer_encoding_headers]
            )
            candidates.append(
                Candidate(
                    E_CL_TE,
                    direction,
                    earliest,
                    latest,
                    message_index,
                    sync_id,
                    "Content-Length and Transfer-Encoding headers coexist",
                )
            )

        transfer_values = [
            header.value for header in transfer_encoding_headers
        ]
        is_chunked = any(
            token.strip().lower() == "chunked"
            for value in transfer_values
            for token in value.split(",")
        )

        content_length: int | None = None
        if content_length_headers:
            first_value = content_length_headers[0].value.strip()
            if not first_value.isdigit():
                candidates.append(
                    Candidate(
                        E_BOUNDARY_RESIDUE,
                        direction,
                        content_length_headers[0].value_start,
                        content_length_headers[0].end,
                        message_index,
                        sync_id,
                        f"invalid Content-Length {first_value!r}",
                    )
                )
                message = ParsedMessage(
                    index=message_index,
                    direction=direction,
                    start_offset=base_offset + position,
                    end_offset=base_offset + len(data),
                    complete=False,
                    framing="content-length",
                    sync_id=sync_id,
                    aborted_at=content_length_headers[0].value_start,
                )
                messages.append(message)
                break
            content_length = int(first_value)

        body_start = header_break + 4
        if is_chunked:
            message_end, complete, aborted_at = _parse_chunked_body(
                data,
                body_start,
                direction=direction,
                message_index=message_index,
                sync_id=sync_id,
                deadline=deadline,
                candidates=candidates,
            )
            messages.append(
                ParsedMessage(
                    index=message_index,
                    direction=direction,
                    start_offset=base_offset + position,
                    end_offset=base_offset + message_end,
                    complete=complete,
                    framing="chunked",
                    sync_id=sync_id,
                    aborted_at=aborted_at,
                )
            )
        elif content_length is not None:
            message_end = body_start + content_length
            if message_end > len(data):
                candidates.append(
                    Candidate(
                        E_BOUNDARY_RESIDUE,
                        direction,
                        base_offset + body_start,
                        base_offset + len(data),
                        message_index,
                        sync_id,
                        "declared Content-Length body is truncated",
                    )
                )
                messages.append(
                    ParsedMessage(
                        index=message_index,
                        direction=direction,
                        start_offset=base_offset + position,
                        end_offset=base_offset + len(data),
                        complete=False,
                        framing="content-length",
                        sync_id=sync_id,
                        declared_length=content_length,
                        aborted_at=base_offset + body_start,
                    )
                )
                break
            messages.append(
                ParsedMessage(
                    index=message_index,
                    direction=direction,
                    start_offset=base_offset + position,
                    end_offset=base_offset + message_end,
                    complete=True,
                    framing="content-length",
                    sync_id=sync_id,
                    declared_length=content_length,
                )
            )
        else:
            message_end = body_start
            messages.append(
                ParsedMessage(
                    index=message_index,
                    direction=direction,
                    start_offset=base_offset + position,
                    end_offset=base_offset + message_end,
                    complete=not header_error,
                    framing="none",
                    sync_id=sync_id,
                )
            )

        message_index += 1
        if message_end <= position:
            candidates.append(
                Candidate(
                    E_BOUNDARY_RESIDUE,
                    direction,
                    base_offset + position,
                    base_offset + len(data),
                    message_index - 1,
                    sync_id,
                    "parser could not advance past the current boundary",
                )
            )
            break
        position = message_end

    return messages, candidates, errors


def _missing_ranges(sequences: list[int]) -> list[list[int]]:
    ranges: list[list[int]] = []
    for previous, current in zip(sequences, sequences[1:]):
        if current > previous + 1:
            ranges.append([previous + 1, current - 1])
    return ranges


def reassemble_direction(
    records: Iterable[MessageRecord],
    *,
    connection_id: str,
    direction: str,
    deadline: Deadline,
) -> StreamResult:
    record_list = list(records)
    by_seq: dict[int, list[MessageRecord]] = {}
    for record in record_list:
        deadline.check()
        by_seq.setdefault(record.seq, []).append(record)

    chosen: dict[int, MessageRecord] = {}
    duplicates: list[dict] = []
    duplicate_count = 0
    for seq in sorted(by_seq):
        deadline.check()
        group = sorted(
            by_seq[seq],
            key=lambda item: (
                item.ts_ms,
                item.payload,
                item.record_index,
            ),
        )
        first = group[0]
        chosen[seq] = first
        if len(group) > 1:
            extra = len(group) - 1
            duplicate_count += extra
            duplicates.append(
                {
                    "seq": seq,
                    "duplicate_count": extra,
                    "total_deliveries": len(group),
                    "conflicting_payloads": len({item.payload for item in group}) > 1,
                }
            )

    sequences = sorted(chosen)
    result = StreamResult(
        connection_id=connection_id,
        direction=direction,
        received_records=len(record_list),
        unique_records=len(chosen),
        seq_min=sequences[0] if sequences else None,
        seq_max=sequences[-1] if sequences else None,
        duplicates=duplicates,
        duplicate_count=duplicate_count,
    )
    if not sequences:
        return result

    result.missing_ranges = _missing_ranges(sequences)
    if result.missing_ranges:
        result.errors.append(
            {
                "code": E_MSG_GAP,
                "direction": direction,
                "byte_offset": -1,
                "range": [],
                "missing_ranges": result.missing_ranges,
            }
        )

    contiguous: list[list[int]] = []
    segment: list[int] = [sequences[0]]
    for seq in sequences[1:]:
        if seq == segment[-1] + 1:
            segment.append(seq)
        else:
            contiguous.append(segment)
            segment = [seq]
    contiguous.append(segment)

    base_offset = 0
    message_index = 0
    for segment_sequences in contiguous:
        deadline.check()
        segment_bytes = b"".join(chosen[seq].payload for seq in segment_sequences)
        messages, candidates, errors = _parse_segment(
            segment_bytes,
            base_offset=base_offset,
            connection_id=connection_id,
            direction=direction,
            start_index=message_index,
            deadline=deadline,
        )
        result.messages.extend(messages)
        result.candidates.extend(candidates)
        result.errors.extend(errors)
        base_offset += len(segment_bytes)
        message_index += len(messages)

    return result

