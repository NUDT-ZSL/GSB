from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .errors import AppError, fail
from .models import AnswerPackage, Citation, Document, Snippet
from .util import canonical_json, q6, sha256_text

SUPPORTED_FORMATS = (1, 2, 3)


@dataclass
class ParsedAnswer:
    package: AnswerPackage
    format_version: int
    dispatch: str
    source_text: str


def _score_text(value: float) -> str:
    return f"{q6(value):.6f}"


def _json_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def serialize_v1(package: AnswerPackage) -> str:
    lines = [
        "CQIR-ANSWER/1",
        f"conv_id={_json_string(package.conv_id)}",
        f"turn_no={package.turn_no}",
        f"answer_text={_json_string(package.answer_text)}",
        f"answer_sha256={package.answer_sha256}",
    ]
    for doc_id, version in sorted(package.doc_versions.items()):
        lines.append(f"doc_version={doc_id}:{version}")
    for citation in package.citations:
        if "|" in citation.doc_id:
            fail("E_FORMAT_CORRUPT", "doc_id cannot contain a pipe", {"field": "doc_id", "line": 0})
        lines.append(
            "|".join(
                (
                    citation.doc_id,
                    str(citation.start),
                    str(citation.end),
                    _score_text(citation.score),
                )
            )
        )
    return "\n".join(lines) + "\n"


def serialize_v2(package: AnswerPackage) -> str:
    lines = [
        "CQIR-ANSWER/2",
        f"conv_id={_json_string(package.conv_id)}",
        f"turn_no={package.turn_no}",
        f"answer_text={_json_string(package.answer_text)}",
        f"answer_sha256={package.answer_sha256}",
    ]
    for doc_id, version in sorted(package.doc_versions.items()):
        lines.append(f"doc_version={doc_id}:{version}")
    for index, citation in enumerate(package.citations):
        required = (
            citation.confidence_low,
            citation.confidence_high,
            citation.paragraph_role,
            citation.order_weight,
        )
        if any(value is None for value in required):
            fail(
                "E_SEMANTIC_INCOMPLETE",
                "v2 serialization requires confidence, role and order weight",
                {
                    "citation_index": index,
                    "doc_id": citation.doc_id,
                    "start": citation.start,
                    "end": citation.end,
                },
            )
        if "|" in citation.doc_id or "|" in str(citation.paragraph_role):
            fail(
                "E_FORMAT_CORRUPT",
                "v2 pipe field cannot contain a pipe",
                {"field": "paragraph_role", "citation_index": index},
            )
        lines.append(
            "|".join(
                (
                    citation.doc_id,
                    str(citation.start),
                    str(citation.end),
                    _score_text(citation.score),
                    _score_text(float(citation.confidence_low)),
                    _score_text(float(citation.confidence_high)),
                    str(citation.paragraph_role),
                    _score_text(float(citation.order_weight)),
                )
            )
        )
    return "\n".join(lines) + "\n"


def serialize_v3(package: AnswerPackage) -> str:
    payload = package.to_storage_dict()
    payload["format"] = "CQIR-ANSWER/3"
    payload["answer_sha256"] = package.answer_sha256
    payload.pop("format_version", None)
    return canonical_json(payload) + "\n"


def serialize(package: AnswerPackage, version: int) -> str:
    if version == 1:
        return serialize_v1(package)
    if version == 2:
        return serialize_v2(package)
    if version == 3:
        return serialize_v3(package)
    fail(
        "E_FORMAT_VERSION_UNSUPPORTED",
        "requested answer format version is not supported",
        {"format_version": version, "supported": list(SUPPORTED_FORMATS)},
    )


def detect_format(text: str) -> tuple[int, str]:
    if not text or not text.strip():
        fail(
            "E_FORMAT_CORRUPT",
            "answer text is empty",
            {"field": "header", "line": 1, "column": 1},
        )
    first_line = text.splitlines()[0].strip()
    if first_line.startswith("CQIR-ANSWER/"):
        raw_version = first_line[len("CQIR-ANSWER/") :]
        if not raw_version:
            fail(
                "E_FORMAT_CORRUPT",
                "format header is truncated",
                {"field": "header", "line": 1, "column": 13},
            )
        if not raw_version.isdigit():
            fail(
                "E_FORMAT_CORRUPT",
                "format header version is not numeric",
                {"field": "header", "line": 1, "column": 13},
            )
        version = int(raw_version)
        if version not in SUPPORTED_FORMATS:
            fail(
                "E_FORMAT_VERSION_UNSUPPORTED",
                "answer format version is not supported",
                {
                    "format_version": version,
                    "line": 1,
                    "field": "header",
                    "supported": list(SUPPORTED_FORMATS),
                },
            )
        return version, "header"
    if first_line.startswith("CQIR-"):
        fail(
            "E_FORMAT_CORRUPT",
            "format header is truncated or incomplete",
            {"field": "header", "line": 1, "column": len(first_line) + 1},
        )
    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            value = json.loads(stripped)
        except json.JSONDecodeError as exc:
            fail(
                "E_FORMAT_CORRUPT",
                "JSON answer cannot be parsed",
                {"field": "json", "line": exc.lineno, "column": exc.colno, "reason": exc.msg},
            )
        if "format" in value:
            header = str(value["format"])
            if not header.startswith("CQIR-ANSWER/"):
                fail(
                    "E_FORMAT_CORRUPT",
                    "structured answer has an invalid format field",
                    {"field": "format", "line": 1},
                )
            raw_version = header[len("CQIR-ANSWER/") :]
            if not raw_version.isdigit():
                fail(
                    "E_FORMAT_CORRUPT",
                    "structured format version is not numeric",
                    {"field": "format", "line": 1},
                )
            version = int(raw_version)
            if version not in SUPPORTED_FORMATS:
                fail(
                    "E_FORMAT_VERSION_UNSUPPORTED",
                    "structured answer format version is not supported",
                    {
                        "format_version": version,
                        "line": 1,
                        "field": "format",
                        "supported": list(SUPPORTED_FORMATS),
                    },
                )
            return version, "field_set"
        if {"answer_text", "citations", "context_chain"} <= set(value):
            return 3, "field_set"
        fail(
            "E_FORMAT_CORRUPT",
            "structured answer field set cannot be identified",
            {"field": "format", "line": 1},
        )
    citation_field_counts = {
        len(line.split("|"))
        for line in text.splitlines()[1:]
        if "|" in line and "=" not in line.split("|", 1)[0]
    }
    if citation_field_counts == {4}:
        return 1, "field_set"
    if citation_field_counts == {8}:
        return 2, "field_set"
    fail(
        "E_FORMAT_CORRUPT",
        "answer field set cannot be identified",
        {
            "field": "header",
            "line": 1,
            "citation_field_counts": sorted(citation_field_counts),
        },
    )


def _corrupt(field: str, line: int, column: int, message: str, **extra: Any) -> None:
    details = {"field": field, "line": line, "column": column}
    details.update(extra)
    fail("E_FORMAT_CORRUPT", message, details)


def _parse_json_string(raw: str, field: str, line: int) -> str:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        _corrupt(field, line, 1, "string field is not valid JSON", reason=exc.msg)
    if not isinstance(value, str):
        _corrupt(field, line, 1, "field must be a JSON string")
    return value


def _parse_int(raw: str, field: str, line: int, column: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        _corrupt(field, line, column, "field is not a valid integer", value=raw)
    return value


def _parse_float(raw: str, field: str, line: int, column: int) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        _corrupt(field, line, column, "field is not a valid number", value=raw)
    if value != value or value in (float("inf"), float("-inf")):
        _corrupt(field, line, column, "field is not a finite number", value=raw)
    return q6(value)


def _snippet_detail(
    document: Document,
    start: int,
    end: int,
    score: float,
    role: str,
    source: str,
) -> dict[str, Any]:
    return {
        "doc_id": document.doc_id,
        "start": start,
        "end": end,
        "paragraph_role": role,
        "matched_terms": [],
        "weights": {"confidence": 1.0, "coverage": 0.0},
        "confidence": q6(score),
        "coverage": 0.0,
        "final_score": q6(score),
        "source": source,
    }


def _parse_flat(
    text: str,
    version: int,
    documents: dict[str, Document],
    dispatch: str,
) -> ParsedAnswer:
    lines = text.splitlines()
    data_lines = lines[1:] if dispatch == "header" else lines
    metadata: dict[str, str] = {}
    doc_versions: dict[str, int] = {}
    citations: list[Citation] = []
    citation_lines: list[int] = []
    expected_fields = 4 if version == 1 else 8
    for line_number, raw_line in enumerate(data_lines, start=2 if dispatch == "header" else 1):
        line = raw_line.strip()
        if not line:
            continue
        if "=" in line and "|" not in line:
            key, raw_value = line.split("=", 1)
            key = key.strip()
            if key == "doc_version":
                if ":" not in raw_value:
                    _corrupt("doc_version", line_number, 1, "doc_version must be doc_id:version")
                doc_id, raw_version = raw_value.rsplit(":", 1)
                if not doc_id:
                    _corrupt("doc_version", line_number, 1, "doc_version doc_id is empty")
                doc_versions[doc_id] = _parse_int(
                    raw_version,
                    "doc_version",
                    line_number,
                    1 + len(doc_id) + 1,
                )
            else:
                metadata[key] = raw_value
            continue
        parts = line.split("|")
        if len(parts) != expected_fields:
            _corrupt(
                "citation",
                line_number,
                len(line) + 1,
                "citation field count does not match format version",
                expected_fields=expected_fields,
                actual_fields=len(parts),
                format_version=version,
            )
        if not parts[0]:
            _corrupt("doc_id", line_number, 1, "doc_id is empty")
        start = _parse_int(parts[1], "start", line_number, len(parts[0]) + 2)
        end = _parse_int(parts[2], "end", line_number, len(parts[0]) + len(parts[1]) + 3)
        score = _parse_float(
            parts[3],
            "score",
            line_number,
            len(parts[0]) + len(parts[1]) + len(parts[2]) + 4,
        )
        if version == 1:
            citation = Citation(
                doc_id=parts[0],
                start=start,
                end=end,
                score=score,
            )
        else:
            column = len(parts[0]) + len(parts[1]) + len(parts[2]) + len(parts[3]) + 5
            confidence_low = _parse_float(
                parts[4],
                "confidence_low",
                line_number,
                column,
            )
            column += len(parts[4]) + 1
            confidence_high = _parse_float(
                parts[5],
                "confidence_high",
                line_number,
                column,
            )
            column += len(parts[5]) + 1
            if not parts[6]:
                _corrupt("paragraph_role", line_number, column, "paragraph_role is empty")
            column += len(parts[6]) + 1
            order_weight = _parse_float(
                parts[7],
                "order_weight",
                line_number,
                column,
            )
            citation = Citation(
                doc_id=parts[0],
                start=start,
                end=end,
                score=score,
                confidence_low=confidence_low,
                confidence_high=confidence_high,
                paragraph_role=parts[6],
                order_weight=order_weight,
            )
        citations.append(citation)
        citation_lines.append(line_number)

    required_metadata = ("conv_id", "turn_no", "answer_text", "answer_sha256")
    for field in required_metadata:
        if field not in metadata:
            _corrupt(field, 1, 1, "required metadata field is missing")
    conv_id = _parse_json_string(metadata["conv_id"], "conv_id", 2)
    turn_no = _parse_int(metadata["turn_no"], "turn_no", 3, 1)
    answer_text = _parse_json_string(metadata["answer_text"], "answer_text", 4)
    answer_sha256 = metadata["answer_sha256"].strip()
    if answer_sha256 != sha256_text(answer_text):
        _corrupt(
            "answer_sha256",
            5,
            1,
            "answer checksum does not match answer_text",
            expected=sha256_text(answer_text),
            actual=answer_sha256,
        )
    snippets: list[Snippet] = []
    for index, citation in enumerate(citations):
        document = documents.get(citation.doc_id)
        if document is None:
            _corrupt(
                "doc_id",
                citation_lines[index],
                1,
                "citation document is not available for span reconstruction",
                doc_id=citation.doc_id,
            )
        role = document.role_for_span(citation.start, citation.end)
        snippets.append(
            Snippet(
                doc_id=citation.doc_id,
                start=citation.start,
                end=citation.end,
                text=document.text[citation.start : citation.end],
                score_detail=_snippet_detail(
                    document,
                    citation.start,
                    citation.end,
                    citation.score,
                    role,
                    f"v{version}_flat",
                ),
            )
        )
    package = AnswerPackage(
        conv_id=conv_id,
        turn_no=turn_no,
        answer_text=answer_text,
        doc_versions=dict(sorted(doc_versions.items())),
        context_chain=(),
        snippets=tuple(snippets),
        citations=tuple(citations),
        format_version=version,
    )
    package.validate(documents)
    return ParsedAnswer(
        package=package,
        format_version=version,
        dispatch=dispatch,
        source_text=text,
    )


def _require_fields(value: dict[str, Any], fields: tuple[str, ...], context: str) -> None:
    for field in fields:
        if field not in value:
            _corrupt(
                f"{context}.{field}" if context else field,
                1,
                1,
                "required structured field is missing",
            )


def _int_field(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _corrupt(field, 1, 1, "structured field must be an integer")
    return value


def _float_field(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _corrupt(field, 1, 1, "structured field must be a number")
    result = q6(float(value))
    if result != result or result in (float("inf"), float("-inf")):
        _corrupt(field, 1, 1, "structured field must be finite")
    return result


def _string_field(value: Any, field: str) -> str:
    if not isinstance(value, str):
        _corrupt(field, 1, 1, "structured field must be a string")
    return value


def _parse_v3(
    text: str,
    dispatch: str,
    documents: dict[str, Document],
) -> ParsedAnswer:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        _corrupt("json", exc.lineno, exc.colno, "JSON answer cannot be parsed", reason=exc.msg)
    if not isinstance(value, dict):
        _corrupt("root", 1, 1, "structured answer root must be an object")
    if "format" in value and value["format"] != "CQIR-ANSWER/3":
        fail(
            "E_FORMAT_VERSION_UNSUPPORTED",
            "structured value is not a v3 answer",
            {"format": value["format"], "line": 1, "supported": ["CQIR-ANSWER/3"]},
        )
    _require_fields(
        value,
        (
            "conv_id",
            "turn_no",
            "answer_text",
            "answer_sha256",
            "doc_versions",
            "context_chain",
            "snippets",
            "citations",
        ),
        "",
    )
    conv_id = _string_field(value["conv_id"], "conv_id")
    turn_no = _int_field(value["turn_no"], "turn_no")
    answer_text = _string_field(value["answer_text"], "answer_text")
    answer_sha256 = _string_field(value["answer_sha256"], "answer_sha256")
    if answer_sha256 != sha256_text(answer_text):
        _corrupt(
            "answer_sha256",
            1,
            1,
            "answer checksum does not match answer_text",
            expected=sha256_text(answer_text),
            actual=answer_sha256,
        )
    if not isinstance(value["doc_versions"], dict):
        _corrupt("doc_versions", 1, 1, "doc_versions must be an object")
    doc_versions = {
        str(doc_id): _int_field(version, f"doc_versions.{doc_id}")
        for doc_id, version in sorted(value["doc_versions"].items())
    }
    if not isinstance(value["context_chain"], list):
        _corrupt("context_chain", 1, 1, "context_chain must be an array")
    context_chain = tuple(
        _string_field(ref, f"context_chain[{index}]")
        for index, ref in enumerate(value["context_chain"])
    )
    if not isinstance(value["snippets"], list):
        _corrupt("snippets", 1, 1, "snippets must be an array")
    snippets: list[Snippet] = []
    snippet_keys: list[tuple[str, int, int]] = []
    for index, item in enumerate(value["snippets"]):
        if not isinstance(item, dict):
            _corrupt(f"snippets[{index}]", 1, 1, "snippet must be an object")
        _require_fields(
            item,
            ("doc_id", "start", "end", "text", "score_detail"),
            f"snippets[{index}]",
        )
        snippet = Snippet(
            doc_id=_string_field(item["doc_id"], f"snippets[{index}].doc_id"),
            start=_int_field(item["start"], f"snippets[{index}].start"),
            end=_int_field(item["end"], f"snippets[{index}].end"),
            text=_string_field(item["text"], f"snippets[{index}].text"),
            score_detail=item["score_detail"],
        )
        detail = snippet.score_detail
        try:
            recomputed = q6(
                float(detail["weights"]["confidence"]) * float(detail["confidence"])
                + float(detail["weights"]["coverage"]) * float(detail["coverage"])
            )
            recorded = q6(float(detail["final_score"]))
        except (KeyError, TypeError, ValueError):
            _corrupt(
                f"snippets[{index}].score_detail",
                1,
                1,
                "snippet score detail is incomplete or not numeric",
            )
        if recomputed != recorded:
            _corrupt(
                f"snippets[{index}].score_detail",
                1,
                1,
                "snippet score detail does not recompute to final_score",
                recomputed=recomputed,
                final_score=recorded,
            )
        document = documents.get(snippet.doc_id)
        if document is None:
            _corrupt(
                f"snippets[{index}].doc_id",
                1,
                1,
                "snippet document is not available",
                doc_id=snippet.doc_id,
            )
        if snippet.text != document.text[snippet.start : snippet.end]:
            _corrupt(
                f"snippets[{index}].text",
                1,
                1,
                "snippet text does not match the document span",
                doc_id=snippet.doc_id,
                start=snippet.start,
                end=snippet.end,
            )
        snippets.append(snippet)
        snippet_keys.append(snippet.key)
    if not isinstance(value["citations"], list):
        _corrupt("citations", 1, 1, "citations must be an array")
    citations: list[Citation] = []
    citation_keys: list[tuple[str, int, int]] = []
    for index, item in enumerate(value["citations"]):
        if not isinstance(item, dict):
            _corrupt(f"citations[{index}]", 1, 1, "citation must be an object")
        _require_fields(
            item,
            (
                "doc_id",
                "start",
                "end",
                "score",
                "confidence_low",
                "confidence_high",
                "paragraph_role",
                "order_weight",
                "revision",
                "context_refs",
            ),
            f"citations[{index}]",
        )
        context_refs = item["context_refs"]
        if not isinstance(context_refs, list):
            _corrupt(f"citations[{index}].context_refs", 1, 1, "context_refs must be an array")
        citation = Citation(
            doc_id=_string_field(item["doc_id"], f"citations[{index}].doc_id"),
            start=_int_field(item["start"], f"citations[{index}].start"),
            end=_int_field(item["end"], f"citations[{index}].end"),
            score=_float_field(item["score"], f"citations[{index}].score"),
            confidence_low=_float_field(
                item["confidence_low"],
                f"citations[{index}].confidence_low",
            ),
            confidence_high=_float_field(
                item["confidence_high"],
                f"citations[{index}].confidence_high",
            ),
            paragraph_role=_string_field(
                item["paragraph_role"],
                f"citations[{index}].paragraph_role",
            ),
            order_weight=_float_field(
                item["order_weight"],
                f"citations[{index}].order_weight",
            ),
            revision=_int_field(item["revision"], f"citations[{index}].revision"),
            context_refs=tuple(
                _string_field(ref, f"citations[{index}].context_refs[{ref_index}]")
                for ref_index, ref in enumerate(context_refs)
            ),
        )
        citations.append(citation)
        citation_keys.append(citation.key)
    if len(snippet_keys) != len(citation_keys):
        fail(
            "E_FORMAT_CORRUPT",
            "v3 snippet and citation counts differ",
            {
                "field": "snippets",
                "line": 1,
                "snippet_count": len(snippet_keys),
                "citation_count": len(citation_keys),
            },
        )
    if snippet_keys != citation_keys:
        fail(
            "E_FORMAT_CORRUPT",
            "v3 snippet and citation spans are not aligned",
            {"field": "citations", "line": 1},
        )
    package = AnswerPackage(
        conv_id=conv_id,
        turn_no=turn_no,
        answer_text=answer_text,
        doc_versions=doc_versions,
        context_chain=context_chain,
        snippets=tuple(snippets),
        citations=tuple(citations),
        format_version=3,
    )
    package.validate(documents)
    return ParsedAnswer(
        package=package,
        format_version=3,
        dispatch=dispatch,
        source_text=text,
    )


def parse_answer(
    text: str,
    documents: dict[str, Document],
    expected_format: int | None = None,
) -> ParsedAnswer:
    try:
        version, dispatch = detect_format(text)
        if expected_format is not None and version != expected_format:
            fail(
                "E_FORMAT_VERSION_UNSUPPORTED",
                "detected format differs from the requested format",
                {
                    "detected": version,
                    "expected": expected_format,
                    "line": 1,
                    "field": "header",
                },
            )
        if version in (1, 2):
            return _parse_flat(text, version, documents, dispatch)
        return _parse_v3(text, dispatch, documents)
    except AppError:
        raise
