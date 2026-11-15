from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .canonical import sha256_bytes
from .errors import ErrorCode, Violation


@dataclass
class ParsedEventStream:
    matches: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    match_order: list[str] = field(default_factory=list)
    stream_errors: list[Violation] = field(default_factory=list)
    event_lines: int = 0
    content_sha256: str = ""

    def ordered_matches(self) -> list[tuple[str, list[dict[str, Any]]]]:
        return [(match_id, self.matches[match_id]) for match_id in self.match_order]

    def to_input_summary(self) -> dict[str, Any]:
        return {
            "content_sha256": self.content_sha256,
            "event_lines": self.event_lines,
            "match_ids": sorted(self.matches),
            "stream_error_count": len(self.stream_errors),
        }


def parse_event_stream(data: bytes) -> ParsedEventStream:
    stream = ParsedEventStream(content_sha256=sha256_bytes(data))
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        stream.stream_errors.append(
            Violation(
                ErrorCode.MALFORMED_EVENT,
                "event stream is not valid UTF-8",
                context={"byte_offset": exc.start},
            )
        )
        return stream

    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        stream.event_lines += 1
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            stream.stream_errors.append(
                Violation(
                    ErrorCode.MALFORMED_EVENT,
                    "event line is not valid JSON",
                    context={
                        "line": line_number,
                        "column": exc.colno,
                        "json_error": exc.msg,
                    },
                )
            )
            continue

        if not isinstance(raw, dict):
            stream.stream_errors.append(
                Violation(
                    ErrorCode.MALFORMED_EVENT,
                    "event line must contain a JSON object",
                    context={"line": line_number},
                )
            )
            continue

        match_id = raw.get("match_id")
        if not isinstance(match_id, str) or not match_id:
            stream.stream_errors.append(
                Violation(
                    ErrorCode.MALFORMED_EVENT,
                    "event is missing a non-empty string match_id",
                    context={"line": line_number},
                )
            )
            continue

        event = dict(raw)
        event["_source_line"] = line_number
        if match_id not in stream.matches:
            stream.matches[match_id] = []
            stream.match_order.append(match_id)
        stream.matches[match_id].append(event)

    return stream


def load_event_stream(path: str | Path) -> ParsedEventStream:
    return parse_event_stream(Path(path).read_bytes())
