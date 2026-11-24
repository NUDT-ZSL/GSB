"""Byte-oriented HTTP/1.x request framing and ambiguity evidence extraction."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .constants import (
    AUX_CHUNK_TERMINATOR,
    AUX_HTTP_PARSE,
    AUX_INCOMPLETE_BODY,
    AUX_INVALID_CL,
    BUSINESS_CHUNK_HEX,
    BUSINESS_CL_TE,
    BUSINESS_DUP_HEADER,
    BUSINESS_TRAILING_BYTES,
    FINDING_PRIORITY,
)
from .models import Finding

_TOKEN_RE = re.compile(rb"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_HEX_RE = re.compile(rb"^[0-9A-Fa-f]+$")


@dataclass(frozen=True)
class Header:
    name: str
    lower_name: str
    value: str
    start: int
    end: int
    value_start: int
    value_end: int


@dataclass
class ParsedRequest:
    ordinal: int
    start: int
    header_end: int
    body_start: int
    body_end: int
    end: int
    request_line: str
    method: str
    target: str
    version: str
    headers: list[Header] = field(default_factory=list)
    chunked: bool = False

    def report_dict(self) -> dict[str, Any]:
        return {
            "ordinal": self.ordinal,
            "request_line": self.request_line,
            "method": self.method,
            "target": self.target,
            "version": self.version,
            "byte_range": {"start": self.start, "end": self.end},
            "header_range": {"start": self.start, "end": self.header_end},
            "body_range": {"start": self.body_start, "end": self.body_end},
            "chunked": self.chunked,
            "headers": [
                {
                    "name": header.name,
                    "value": header.value,
                    "byte_range": {"start": header.start, "end": header.end},
                    "value_range": {
                        "start": header.value_start,
                        "end": header.value_end,
                    },
                }
                for header in self.headers
            ],
        }


@dataclass
class HttpAnalysis:
    requests: list[ParsedRequest]
    findings: list[Finding]
    timeout_checks: int = 0

    def report_requests(self) -> list[dict[str, Any]]:
        return [request.report_dict() for request in self.requests]


def _finding(
    code: str,
    connection_id: str,
    ordinal: int,
    start: int,
    end: int,
    offset: int,
    details: dict[str, Any],
    tokens: set[str] | list[str],
) -> Finding:
    stable_tokens = tuple(sorted({str(token).casefold() for token in tokens if token}))
    return Finding(
        code=code,
        connection_id=connection_id,
        message_ordinal=ordinal,
        byte_start=max(0, start),
        byte_end=max(0, end),
        byte_offset=max(0, offset),
        details=details,
        match_tokens=stable_tokens,
    )


def _header_lines(data: bytes, start: int, end: int) -> tuple[list[tuple[int, int, bytes]], bytes]:
    segment = data[start:end]
    separator = b"\r\n" if b"\r\n" in segment else b"\n"
    lines: list[tuple[int, int, bytes]] = []
    cursor = start
    while cursor < end:
        newline = data.find(separator, cursor, end)
        if newline < 0:
            lines.append((cursor, end, data[cursor:end]))
            break
        lines.append((cursor, newline, data[cursor:newline]))
        cursor = newline + len(separator)
    return lines, separator


def _find_header_end(data: bytes, start: int) -> tuple[int, int, bytes] | None:
    candidates: list[tuple[int, int, bytes]] = []
    crlf = data.find(b"\r\n\r\n", start)
    if crlf >= 0:
        candidates.append((crlf, crlf + 4, b"\r\n"))
    lf = data.find(b"\n\n", start)
    if lf >= 0:
        candidates.append((lf, lf + 2, b"\n"))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], -len(item[2])))
    return candidates[0]


def _parse_request_line(raw: bytes) -> tuple[str, str, str] | None:
    try:
        text = raw.decode("latin-1")
    except UnicodeDecodeError:
        return None
    parts = text.split()
    if len(parts) != 3:
        return None
    method, target, version = parts
    if not _TOKEN_RE.fullmatch(method.encode("latin-1", "ignore")):
        return None
    if not version.upper().startswith("HTTP/1."):
        return None
    return method, target, version


def _parse_headers(
    data: bytes,
    start: int,
    end: int,
    separator: bytes,
    connection_id: str,
    ordinal: int,
    findings: list[Finding],
) -> list[Header]:
    headers: list[Header] = []
    cursor = start
    while cursor < end:
        newline = data.find(separator, cursor, end)
        line_end = end if newline < 0 else newline
        line = data[cursor:line_end]
        if not line:
            break
        # Obsolete line folding is intentionally not joined: the front end and
        # back end may disagree on how to normalize it.
        if line[:1] in (b" ", b"\t") and headers:
            findings.append(
                _finding(
                    AUX_HTTP_PARSE,
                    connection_id,
                    ordinal,
                    cursor,
                    line_end,
                    cursor,
                    {"reason": "obsolete header folding"},
                    {"http-parse", "obs-fold"},
                )
            )
            cursor = line_end + len(separator)
            continue
        colon = line.find(b":")
        if colon <= 0:
            findings.append(
                _finding(
                    AUX_HTTP_PARSE,
                    connection_id,
                    ordinal,
                    cursor,
                    line_end,
                    cursor,
                    {"reason": "header line has no valid name separator"},
                    {"http-parse", "header"},
                )
            )
            cursor = line_end + len(separator)
            continue
        name_bytes = line[:colon].strip()
        if not _TOKEN_RE.fullmatch(name_bytes):
            findings.append(
                _finding(
                    AUX_HTTP_PARSE,
                    connection_id,
                    ordinal,
                    cursor,
                    line_end,
                    cursor,
                    {"reason": "invalid header field name"},
                    {"http-parse", "header"},
                )
            )
            cursor = line_end + len(separator)
            continue
        value_bytes = line[colon + 1 :]
        left = len(value_bytes) - len(value_bytes.lstrip(b" \t"))
        right = len(value_bytes.rstrip(b" \t"))
        value_start = cursor + colon + 1 + left
        value_end = cursor + colon + 1 + max(left, right)
        name = name_bytes.decode("latin-1")
        value = value_bytes[left:right].decode("latin-1")
        headers.append(
            Header(
                name=name,
                lower_name=name.casefold(),
                value=value,
                start=cursor,
                end=line_end,
                value_start=value_start,
                value_end=value_end,
            )
        )
        cursor = line_end + len(separator)
    return headers


def _detect_duplicate_header_conflicts(
    headers: list[Header],
    connection_id: str,
    ordinal: int,
    findings: list[Finding],
) -> None:
    by_name: dict[str, list[Header]] = {}
    for header in headers:
        by_name.setdefault(header.lower_name, []).append(header)
    for lower_name, occurrences in sorted(by_name.items()):
        if len(occurrences) < 2:
            continue
        normalized = [" ".join(header.value.split()) for header in occurrences]
        if len(set(normalized)) == 1:
            continue
        conflict_index = next(
            index for index in range(1, len(normalized)) if normalized[index] != normalized[0]
        )
        conflict = occurrences[conflict_index]
        match_tokens = {
            "smug_dup_header",
            "dup-header",
            "duplicate-header",
            "duplicate_header",
            "duplicate-header-conflict",
            lower_name,
        }
        for header in occurrences:
            match_tokens.add(f"{header.lower_name}: {header.value}")
            match_tokens.add(header.value)
        findings.append(
            _finding(
                BUSINESS_DUP_HEADER,
                connection_id,
                ordinal,
                conflict.start,
                conflict.end,
                conflict.start,
                {
                    "header_name": lower_name,
                    "values": [
                        {
                            "value": header.value,
                            "byte_range": {"start": header.start, "end": header.end},
                        }
                        for header in occurrences
                    ],
                },
                match_tokens,
            )
        )


def _content_length_value(
    headers: list[Header],
    connection_id: str,
    ordinal: int,
    findings: list[Finding],
) -> int | None:
    values = [header for header in headers if header.lower_name == "content-length"]
    if not values:
        return None
    parsed: list[int] = []
    for header in values:
        raw = header.value.strip()
        if not raw.isdigit():
            findings.append(
                _finding(
                    AUX_INVALID_CL,
                    connection_id,
                    ordinal,
                    header.start,
                    header.end,
                    header.value_start,
                    {"header_name": "content-length", "value": header.value},
                    {"http-parse", "invalid-content-length", "content-length"},
                )
            )
            continue
        parsed.append(int(raw))
    if not parsed:
        return None
    # Conflicting CLs are already a dedicated ambiguity.  The first value is
    # used only to continue byte-oriented evidence collection.
    return parsed[0]


def _transfer_encoding_chunked(headers: list[Header]) -> tuple[bool, bool]:
    values = [header for header in headers if header.lower_name == "transfer-encoding"]
    if not values:
        return False, False
    combined = ",".join(header.value.casefold() for header in values)
    return True, "chunked" in [token.strip() for token in combined.split(",")]


def _read_http_line(
    data: bytes, position: int
) -> tuple[bytes, int, int] | None:
    crlf = data.find(b"\r\n", position)
    lf = data.find(b"\n", position)
    choices = []
    if crlf >= 0:
        choices.append((crlf, crlf + 2, 2))
    if lf >= 0:
        choices.append((lf, lf + 1, 1))
    if not choices:
        return None
    choices.sort()
    end, next_position, _ = choices[0]
    return data[position:end], end, next_position


def _looks_like_request(data: bytes, position: int) -> bool:
    if position >= len(data):
        return False
    # A request cannot start with leading whitespace or a control byte.
    if data[position] <= 0x20 or data[position] >= 0x7F:
        return False
    line = _read_http_line(data, position)
    if line is None:
        return False
    request_line, _, _ = line
    if len(request_line) > 8192:
        return False
    return _parse_request_line(request_line) is not None


def _check_timeout(check_timeout: Callable[[], None] | None) -> None:
    if check_timeout is not None:
        check_timeout()


def analyze_request_stream(
    data: bytes,
    connection_id: str,
    *,
    check_timeout: Callable[[], None] | None = None,
    max_requests: int = 10000,
) -> HttpAnalysis:
    """Parse a reconstructed request stream into framed requests.

    Byte ranges are half-open and relative to the reconstructed stream for the
    request direction.  Findings are emitted in deterministic byte order.
    """

    findings: list[Finding] = []
    requests: list[ParsedRequest] = []
    position = 0
    ordinal = 0
    while position < len(data):
        _check_timeout(check_timeout)
        if ordinal >= max_requests:
            findings.append(
                _finding(
                    AUX_HTTP_PARSE,
                    connection_id,
                    ordinal + 1,
                    position,
                    len(data),
                    position,
                    {"reason": "request framing limit reached"},
                    {"http-parse", "request-limit"},
                )
            )
            break
        ordinal += 1
        header_region = _find_header_end(data, position)
        if header_region is None:
            findings.append(
                _finding(
                    AUX_HTTP_PARSE,
                    connection_id,
                    ordinal,
                    position,
                    len(data),
                    position,
                    {"reason": "missing HTTP header terminator"},
                    {"http-parse", "header-terminator"},
                )
            )
            break
        header_end_index, body_start, separator = header_region
        lines, _ = _header_lines(data, position, header_end_index)
        if not lines:
            findings.append(
                _finding(
                    AUX_HTTP_PARSE,
                    connection_id,
                    ordinal,
                    position,
                    body_start,
                    position,
                    {"reason": "empty request"},
                    {"http-parse", "empty-request"},
                )
            )
            break
        request_line_start, request_line_end, request_line_bytes = lines[0]
        parsed_line = _parse_request_line(request_line_bytes)
        if parsed_line is None:
            findings.append(
                _finding(
                    AUX_HTTP_PARSE,
                    connection_id,
                    ordinal,
                    request_line_start,
                    request_line_end,
                    request_line_start,
                    {"reason": "invalid request line"},
                    {"http-parse", "request-line"},
                )
            )
            # Without a valid start line, continuing risks inventing request
            # boundaries out of body bytes.
            break
        method, target, version = parsed_line
        headers = _parse_headers(
            data,
            request_line_end + len(separator),
            header_end_index,
            separator,
            connection_id,
            ordinal,
            findings,
        )
        _check_timeout(check_timeout)
        _detect_duplicate_header_conflicts(headers, connection_id, ordinal, findings)

        te_present, chunked = _transfer_encoding_chunked(headers)
        cl_value = _content_length_value(headers, connection_id, ordinal, findings)
        if te_present and cl_value is not None:
            relevant = [
                header
                for header in headers
                if header.lower_name in ("transfer-encoding", "content-length")
            ]
            start = min(header.start for header in relevant)
            end = max(header.end for header in relevant)
            match_tokens = {
                "smug_cl_te",
                "cl-te",
                "cl_te",
                "cl.te",
                "cl-te-conflict",
                "content-length+transfer-encoding",
                "te-cl",
            }
            for header in relevant:
                match_tokens.add(f"{header.lower_name}: {header.value}")
                match_tokens.add(header.value)
            findings.append(
                _finding(
                    BUSINESS_CL_TE,
                    connection_id,
                    ordinal,
                    start,
                    end,
                    start,
                    {
                        "content_length": cl_value,
                        "transfer_encoding": [
                            header.value
                            for header in headers
                            if header.lower_name == "transfer-encoding"
                        ],
                        "header_offsets": [
                            {
                                "name": header.lower_name,
                                "byte_range": {
                                    "start": header.start,
                                    "end": header.end,
                                },
                            }
                            for header in relevant
                        ],
                    },
                    match_tokens,
                )
            )

        body_end = body_start
        framing_incomplete = False
        if chunked:
            cursor = body_start
            zero_chunk_seen = False
            while cursor < len(data):
                _check_timeout(check_timeout)
                line = _read_http_line(data, cursor)
                if line is None:
                    findings.append(
                        _finding(
                            AUX_INCOMPLETE_BODY,
                            connection_id,
                            ordinal,
                            cursor,
                            len(data),
                            cursor,
                            {"reason": "chunk-size line has no line terminator"},
                            {"http-parse", "chunk", "incomplete"},
                        )
                    )
                    body_end = len(data)
                    framing_incomplete = True
                    break
                size_line, line_end, next_cursor = line
                size_token = size_line.split(b";", 1)[0].strip()
                invalid_at = None
                if not size_token:
                    invalid_at = 0
                else:
                    for index, byte in enumerate(size_token):
                        if chr(byte) not in "0123456789abcdefABCDEF":
                            invalid_at = index
                            break
                if invalid_at is not None:
                    findings.append(
                        _finding(
                            BUSINESS_CHUNK_HEX,
                            connection_id,
                            ordinal,
                            cursor,
                            next_cursor,
                            cursor + invalid_at,
                            {
                                "chunk_size_line": size_line.decode("latin-1"),
                                "invalid_byte_hex": (
                                    f"{size_token[invalid_at]:02x}"
                                    if size_token and invalid_at < len(size_token)
                                    else None
                                ),
                            },
                            {
                                "smug_chunk_hex",
                                "chunk-hex",
                                "chunk_hex",
                                "chunk-size-hex",
                                "hex",
                                size_token.decode("latin-1", "replace"),
                                size_line.decode("latin-1", "replace"),
                            },
                        )
                    )
                    # All following bytes belong to an unparsable chunked body.
                    body_end = len(data)
                    framing_incomplete = True
                    break
                size = int(size_token, 16)
                if size == 0:
                    trailer_cursor = next_cursor
                    while trailer_cursor < len(data):
                        _check_timeout(check_timeout)
                        trailer_line = _read_http_line(data, trailer_cursor)
                        if trailer_line is None:
                            findings.append(
                                _finding(
                                    AUX_INCOMPLETE_BODY,
                                    connection_id,
                                    ordinal,
                                    trailer_cursor,
                                    len(data),
                                    trailer_cursor,
                                    {"reason": "chunk trailer line is incomplete"},
                                    {"http-parse", "chunk", "incomplete"},
                                )
                            )
                            trailer_cursor = len(data)
                            break
                        trailer_bytes, _, trailer_next = trailer_line
                        if not trailer_bytes:
                            trailer_cursor = trailer_next
                            break
                        trailer_cursor = trailer_next
                    body_end = trailer_cursor
                    zero_chunk_seen = True
                    break
                data_start = next_cursor
                data_end = data_start + size
                if data_end > len(data):
                    findings.append(
                        _finding(
                            AUX_INCOMPLETE_BODY,
                            connection_id,
                            ordinal,
                            data_start,
                            len(data),
                            data_start,
                            {"reason": "chunk data exceeds stream", "declared_size": size},
                            {"http-parse", "chunk", "incomplete"},
                        )
                    )
                    body_end = len(data)
                    framing_incomplete = True
                    break
                if data[data_end : data_end + 2] == b"\r\n":
                    cursor = data_end + 2
                elif data[data_end : data_end + 1] == b"\n":
                    cursor = data_end + 1
                else:
                    findings.append(
                        _finding(
                            AUX_CHUNK_TERMINATOR,
                            connection_id,
                            ordinal,
                            data_end,
                            min(len(data), data_end + 2),
                            data_end,
                            {"reason": "chunk data is not followed by CRLF"},
                            {"http-parse", "chunk", "terminator"},
                        )
                    )
                    body_end = min(len(data), data_end + 2)
                    framing_incomplete = True
                    break
            if not zero_chunk_seen and not framing_incomplete:
                # Ran out of bytes without a terminating chunk.
                findings.append(
                    _finding(
                        AUX_INCOMPLETE_BODY,
                        connection_id,
                        ordinal,
                        body_start,
                        len(data),
                        len(data),
                        {"reason": "missing zero-length terminating chunk"},
                        {"http-parse", "chunk", "incomplete"},
                    )
                )
                framing_incomplete = True
                body_end = len(data)
        elif cl_value is not None:
            body_end = min(len(data), body_start + cl_value)
            if body_start + cl_value > len(data):
                findings.append(
                    _finding(
                        AUX_INCOMPLETE_BODY,
                        connection_id,
                        ordinal,
                        body_start,
                        len(data),
                        len(data),
                        {
                            "reason": "content-length exceeds stream",
                            "declared_length": cl_value,
                        },
                        {"http-parse", "incomplete"},
                    )
                )
                framing_incomplete = True
        else:
            body_end = body_start

        request = ParsedRequest(
            ordinal=ordinal,
            start=position,
            header_end=body_start,
            body_start=body_start,
            body_end=body_end,
            end=body_end,
            request_line=request_line_bytes.decode("latin-1"),
            method=method,
            target=target,
            version=version,
            headers=headers,
            chunked=chunked,
        )

        if framing_incomplete:
            requests.append(request)
            break

        if body_end < len(data):
            if _looks_like_request(data, body_end):
                requests.append(request)
                position = body_end
                continue
            findings.append(
                _finding(
                    BUSINESS_TRAILING_BYTES,
                    connection_id,
                    ordinal,
                    body_end,
                    len(data),
                    body_end,
                    {
                        "reason": "bytes cannot be attributed to a framed request",
                        "byte_length": len(data) - body_end,
                        "preview_hex": data[body_end : body_end + 32].hex(),
                    },
                    {
                        "smug_trailing_bytes",
                        "trailing-bytes",
                        "trailing_bytes",
                        "extra-bytes",
                        "unattributed-bytes",
                    },
                )
            )
            request.body_end = body_end
            request.end = len(data)
            requests.append(request)
            break
        request.end = body_end
        requests.append(request)
        position = body_end
        if position == len(data):
            break

    findings = sorted(
        findings,
        key=lambda item: (
            item.byte_offset,
            FINDING_PRIORITY.get(item.code, 99),
            item.byte_end,
            item.code,
            __import__("json").dumps(item.details, sort_keys=True, ensure_ascii=False),
        ),
    )
    return HttpAnalysis(requests=requests, findings=findings)

