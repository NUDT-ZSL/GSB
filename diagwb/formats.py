"""On-disk readers/writers for the v1, v2 and v3 result formats.

Format summary
--------------
v1 - unnamed pipe-delimited line: ``tool_id|payload|status`` (three segments,
     no structural metadata and no source markers).
v2 - named key=value pipe-delimited line carrying payload, status,
     ``result_schema_version`` and a confidence interval.
v3 - a structured JSON object carrying shard references, a multi-result
     aggregation declaration and a source chain.

v1/v2 files start with a ``# FORMAT_VERSION=N`` header; v3 files may carry the
same header or be a bare JSON object. Dispatch falls back to field-set
inference when no header is present. All field values are URL-encoded so the
``|`` and ``=`` delimiters are never ambiguous.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib.parse import quote, unquote

from .errors import E_FORMAT_CORRUPT, E_FORMAT_VERSION_UNSUPPORTED, DiagError
from .hashing import raw_content_hash
from .models import ParseRecord

CONTROLLED_STATUSES = {"OK", "EMPTY", "ERROR", "THROTTLED"}
SUPPORTED_VERSIONS = {1, 2, 3}

V2_FIELDS = ("result_ref", "tool_id", "payload", "status",
             "result_schema_version", "confidence")


def _enc(value: Any) -> str:
    """URL-encode a scalar/JSON value for safe pipe-delimited storage."""
    if not isinstance(value, str):
        value = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return quote(value, safe="")


def _dec(segment: str) -> str:
    return unquote(segment)


def _corrupt(message: str, line_no: int, field_offset: int,
             **extra: Any) -> DiagError:
    context = {"line_no": line_no, "field_offset": field_offset}
    context.update(extra)
    return DiagError(E_FORMAT_CORRUPT, message, context)


def detect_version(lines: List[str]) -> int:
    """Dispatch to a format version via header or field-set inference."""
    first = lines[0].strip() if lines else ""
    if first.startswith("#"):
        for token in first[1:].split(";"):
            token = token.strip()
            if token.startswith("FORMAT_VERSION="):
                try:
                    version = int(token.split("=", 1)[1])
                except ValueError as exc:  # pragma: no cover - defensive
                    raise DiagError(
                        E_FORMAT_VERSION_UNSUPPORTED,
                        "non-integer format version header",
                        context={"header": first},
                    ) from exc
                if version not in SUPPORTED_VERSIONS:
                    raise DiagError(
                        E_FORMAT_VERSION_UNSUPPORTED,
                        f"format version {version} has no registered reader",
                        {"version": version, "line_no": 1},
                    )
                return version
        raise DiagError(E_FORMAT_VERSION_UNSUPPORTED,
                        "comment header without FORMAT_VERSION",
                        {"line_no": 1})
    # Field-set inference without a header.
    if first.startswith("{"):
        try:
            obj = json.loads(first)
        except json.JSONDecodeError as exc:
            raise _corrupt("v3 record is not valid JSON", 1, 0,
                           char_offset=exc.pos) from exc
        version = obj.get("format_version")
    elif "=" in first:
        version = 2
    else:
        version = 1
    if version not in SUPPORTED_VERSIONS:
        raise DiagError(E_FORMAT_VERSION_UNSUPPORTED,
                        f"format version {version} has no registered reader",
                        {"version": version, "line_no": 1})
    return int(version)


def _split_segments(line: str, expected: int, line_no: int) -> List[str]:
    segments = line.split("|")
    if len(segments) != expected:
        raise _corrupt(
            f"expected {expected} pipe-delimited fields, found {len(segments)}",
            line_no, min(len(segments), expected),
            expected_fields=expected, actual_fields=len(segments),
        )
    return segments


def _check_status(status: Any, line_no: int, field_offset: int) -> str:
    if status not in CONTROLLED_STATUSES:
        raise _corrupt("status code outside the controlled set", line_no,
                       field_offset, status=status,
                       allowed=sorted(CONTROLLED_STATUSES))
    return status


def _parse_payload(raw: str, line_no: int, field_offset: int) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _corrupt("payload is not parseable JSON", line_no, field_offset,
                       char_offset=exc.pos, raw=raw[:80]) from exc


def _parse_v1(line: str, line_no: int) -> ParseRecord:
    segments = _split_segments(line, 3, line_no)
    tool_id = _dec(segments[0])
    if not tool_id:
        raise _corrupt("v1 tool_id segment is empty", line_no, 0)
    payload = _parse_payload(_dec(segments[1]), line_no, 1)
    status = _check_status(_dec(segments[2]), line_no, 2)
    result_ref = f"{tool_id}@v1"
    fields = {"result_ref": result_ref, "tool_id": tool_id,
              "payload": payload, "status": status}
    return ParseRecord(result_ref, 1, raw_content_hash(line), fields, line_no)


def _parse_v2(line: str, line_no: int) -> ParseRecord:
    segments = _split_segments(line, len(V2_FIELDS), line_no)
    fields: Dict[str, Any] = {}
    for offset, segment in enumerate(segments):
        if "=" not in segment:
            raise _corrupt("v2 segment is not a key=value pair", line_no,
                           offset, segment=segment[:40])
        key, value = segment.split("=", 1)
        if key not in V2_FIELDS:
            raise _corrupt("v2 segment uses an unknown field name", line_no,
                           offset, field_name=key)
        fields[key] = _dec(value)
    missing = [name for name in V2_FIELDS if name not in fields]
    if missing:
        raise _corrupt("v2 record is missing required fields", line_no,
                       V2_FIELDS.index(missing[0]), missing=missing)
    fields["payload"] = _parse_payload(fields["payload"], line_no,
                                       V2_FIELDS.index("payload"))
    fields["status"] = _check_status(
        fields["status"], line_no, V2_FIELDS.index("status"))
    confidence_raw = fields.pop("confidence")
    try:
        low, high = confidence_raw.split(",")
        fields["confidence"] = [float(low), float(high)]
    except (ValueError, IndexError) as exc:
        raise _corrupt("v2 confidence interval is not low,high decimals",
                       line_no, V2_FIELDS.index("confidence"),
                       raw=confidence_raw) from exc
    return ParseRecord(fields["result_ref"], 2, raw_content_hash(line),
                       fields, line_no)


def _parse_v3(line: str, line_no: int) -> ParseRecord:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as exc:
        raise _corrupt("v3 record is not valid JSON", line_no, 0,
                       char_offset=exc.pos) from exc
    required = ("result_ref", "tool_id", "payload", "status",
                "result_schema_version", "confidence", "shards",
                "aggregation", "lineage")
    for offset, name in enumerate(required):
        if name not in obj:
            raise _corrupt("v3 record is missing a required field", line_no,
                           offset, field_name=name)
    if not isinstance(obj["payload"], (dict, list)):
        raise _corrupt("v3 payload must be a structured object or array",
                       line_no, required.index("payload"))
    obj["status"] = _check_status(
        obj["status"], line_no, required.index("status"))
    confidence = obj["confidence"]
    if not (isinstance(confidence, list) and len(confidence) == 2):
        raise _corrupt("v3 confidence must be a [low, high] pair", line_no,
                       required.index("confidence"))
    return ParseRecord(obj["result_ref"], 3, raw_content_hash(line), obj,
                       line_no)


def parse_text(text: str) -> Tuple[int, List[ParseRecord]]:
    """Parse result-file text, returning the version and all records."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    raw_lines = normalized.split("\n")
    version = detect_version(raw_lines)
    body = [line for line in raw_lines
            if line.strip() and not line.strip().startswith("#")]
    parser = {1: _parse_v1, 2: _parse_v2, 3: _parse_v3}[version]
    records: List[ParseRecord] = []
    line_offset = 1 if raw_lines and raw_lines[0].strip().startswith("#") else 0
    for index, line in enumerate(body):
        records.append(parser(line, index + line_offset + 1))
    return version, records


def parse_file(path: Path) -> Tuple[int, List[ParseRecord]]:
    """Parse a result file from disk."""
    return parse_text(Path(path).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Writers (serialization used by migrations and round-trip checks)
# --------------------------------------------------------------------------- #

def render_v1_body(tool_id: str, payload: Any, status: str) -> str:
    return "|".join([_enc(tool_id), _enc(payload), _enc(status)])


def render_v2_body(fields: Dict[str, Any]) -> str:
    ordered = {
        "result_ref": fields["result_ref"],
        "tool_id": fields["tool_id"],
        "payload": fields["payload"],
        "status": fields["status"],
        "result_schema_version": fields["result_schema_version"],
        "confidence": "{:.6f},{:.6f}".format(*fields["confidence"]),
    }
    return "|".join(f"{key}={_enc(value)}" for key, value in ordered.items())


def render_v3_body(obj: Dict[str, Any]) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


def write_result_file(path: Path, version: int, body: str) -> None:
    """Write a record body with the canonical format header."""
    header = f"# FORMAT_VERSION={version}\n"
    Path(path).write_text(header + body + "\n", encoding="utf-8")
