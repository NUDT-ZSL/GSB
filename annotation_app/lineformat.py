"""Deterministic line-oriented exchange format.

Header:
    #LEDGER-LINE|1
    #PREFIX|<semantic>|<single-character prefix>

Annotation records (all fields escaped; delimiter is `|`):
    E|doc_id|id|type|start|end|text|canonical_id|low|high|coref_id
    T|doc_id|id|type|start|end|text|event_id|low|high
    R|doc_id|id|type|start|end|text|source|target|low|high
    A|doc_id|id|role|start|end|text|source|target|event_id|low|high
Document/meta records:
    D|doc_id|text
    M|sample_id|source_version|ontology_version|ontology_checksum

Escapes: backslash, pipe, LF, CR and TAB become `\\\\`, `\\|`, `\\n`,
`\\r`, `\\t`. Physical lines are always single-line UTF-8.
"""

from __future__ import annotations

import re
from typing import Any

from .errors import AppError, ErrorCode, fail
from .model import (
    CanonicalSample,
    Confidence,
    CorefGroup,
    Document,
    Edge,
    Node,
    V1Sample,
    V2Sample,
)
from .ontology import OntologySnapshot


LINE_MAGIC = "#LEDGER-LINE|1"
PREFIX_LAYOUT = {
    "entity": "E",
    "trigger": "T",
    "relation": "R",
    "argument": "A",
    "document": "D",
    "meta": "M",
}
_INTEGER = re.compile(r"^-?\d+$")


def escape_field(value: Any) -> str:
    text = "" if value is None else str(value)
    return (
        text.replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def unescape_field(value: str, *, line_no: int) -> str:
    output: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char != "\\":
            output.append(char)
            index += 1
            continue
        if index + 1 >= len(value):
            fail(
                ErrorCode.FORMAT_INVALID,
                "dangling escape at end of field",
                path=f"line:{line_no}",
                stage="parse",
                format_name="line",
                line=line_no,
            )
        escaped = value[index + 1]
        mapping = {"\\": "\\", "|": "|", "n": "\n", "r": "\r", "t": "\t"}
        if escaped not in mapping:
            fail(
                ErrorCode.FORMAT_INVALID,
                f"unsupported escape sequence \\{escaped}",
                path=f"line:{line_no}",
                stage="parse",
                format_name="line",
                line=line_no,
            )
        output.append(mapping[escaped])
        index += 2
    return "".join(output)


def _split(line: str, line_no: int) -> list[str]:
    fields: list[str] = []
    current: list[str] = []
    index = 0
    while index < len(line):
        char = line[index]
        if char == "\\":
            if index + 1 >= len(line):
                fail(
                    ErrorCode.FORMAT_INVALID,
                    "dangling escape in line",
                    path=f"line:{line_no}",
                    stage="parse",
                    format_name="line",
                    line=line_no,
                )
            current.append(char)
            current.append(line[index + 1])
            index += 2
            continue
        if char == "|":
            fields.append("".join(current))
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    fields.append("".join(current))
    return fields


def _offset(raw: str, path: str, line_no: int) -> int:
    if not _INTEGER.match(raw):
        fail(
            ErrorCode.OFFSET_TYPE,
            "line-format offset must be an integer",
            path=path,
            stage="parse",
            format_name="line",
            line=line_no,
            details={"actual": raw},
        )
    return int(raw)


def _confidence(low_raw: str, high_raw: str, path: str, line_no: int) -> Confidence:
    try:
        low = float(low_raw)
        high = float(high_raw)
    except ValueError as exc:
        fail(
            ErrorCode.SCHEMA_TYPE,
            "line-format confidence must be numeric",
            path=path,
            stage="parse",
            format_name="line",
            line=line_no,
            details={"low": low_raw, "high": high_raw},
            cause=exc,
        )
    if not (0.0 <= low <= high <= 1.0):
        fail(
            ErrorCode.SCHEMA_TYPE,
            "confidence interval must satisfy 0 <= low <= high <= 1",
            path=path,
            stage="parse",
            format_name="line",
            line=line_no,
        )
    return Confidence(low=low, high=high)


def _expect_count(
    fields: list[str],
    expected: int,
    path: str,
    line_no: int,
) -> None:
    if len(fields) != expected:
        fail(
            ErrorCode.FORMAT_INVALID,
            f"expected {expected} fields, got {len(fields)}",
            path=path,
            stage="parse",
            format_name="line",
            line=line_no,
            details={"expected_fields": expected, "actual_fields": len(fields)},
        )


def export_line(
    sample: CanonicalSample,
    *,
    ontology: OntologySnapshot,
) -> str:
    if isinstance(sample, V1Sample):
        from .migration import upgrade_v1_to_v2

        sample = upgrade_v1_to_v2(sample, ontology).sample
    assert isinstance(sample, V2Sample)

    lines = [
        LINE_MAGIC,
        *[
            f"#PREFIX|{name}|{prefix}"
            for name, prefix in PREFIX_LAYOUT.items()
        ],
        "|".join(
            [
                "M",
                escape_field(sample.sample_id),
                escape_field(sample.version),
                escape_field(ontology.version),
                escape_field(ontology.checksum_value),
            ]
        ),
    ]

    coref_membership = sample.coref_membership()
    for document in sample.documents:
        lines.append(
            "|".join(
                [
                    "D",
                    escape_field(document.doc_id),
                    escape_field(document.text),
                ]
            )
        )
        for node in document.nodes:
            if node.kind == "entity":
                fields = [
                    "E",
                    document.doc_id,
                    node.id,
                    node.type,
                    node.start,
                    node.end,
                    node.text,
                    node.canonical_id or "",
                    node.confidence.low,
                    node.confidence.high,
                    coref_membership.get(node.id, ""),
                ]
            else:
                fields = [
                    "T",
                    document.doc_id,
                    node.id,
                    node.type,
                    node.start,
                    node.end,
                    node.text,
                    node.event_id or "",
                    node.confidence.low,
                    node.confidence.high,
                ]
            lines.append("|".join(escape_field(field) for field in fields))

        node_lookup = {node.id: node for node in document.nodes}
        for edge in document.edges:
            if edge.kind == "relation":
                source = node_lookup[edge.source]
                target = node_lookup[edge.target]
                start = min(source.start, target.start)
                end = max(source.end, target.end)
                text = document.text[start:end]
                fields = [
                    "R",
                    document.doc_id,
                    edge.id,
                    edge.type or "",
                    start,
                    end,
                    text,
                    edge.source,
                    edge.target,
                    edge.confidence.low,
                    edge.confidence.high,
                ]
            else:
                source = node_lookup[edge.source]
                fields = [
                    "A",
                    document.doc_id,
                    edge.id,
                    edge.role or "",
                    source.start,
                    source.end,
                    source.text,
                    edge.source,
                    edge.target,
                    edge.event_id or "",
                    edge.confidence.low,
                    edge.confidence.high,
                ]
            lines.append("|".join(escape_field(field) for field in fields))
    return "\n".join(lines) + "\n"


def parse_line_format(
    text: str,
    *,
    ontology: OntologySnapshot | None = None,
    max_import_bytes: int | None = None,
) -> V2Sample:
    if max_import_bytes is not None and len(text.encode("utf-8")) > max_import_bytes:
        fail(
            ErrorCode.RESOURCE_LIMIT,
            "import exceeds the configured byte limit",
            path="$",
            stage="import",
            format_name="line",
            details={
                "limit": max_import_bytes,
                "actual": len(text.encode("utf-8")),
            },
        )
    physical_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if not physical_lines or physical_lines[0].lstrip("\ufeff") != LINE_MAGIC:
        fail(
            ErrorCode.FORMAT_INVALID,
            f"first line must be {LINE_MAGIC}",
            path="line:1",
            stage="parse",
            format_name="line",
            line=1,
        )

    prefixes: dict[str, str] = {}
    reverse_prefixes: dict[str, str] = {}
    meta: dict[str, str] | None = None
    documents: dict[str, dict[str, Any]] = {}
    doc_order: list[str] = []
    coref_members: dict[str, list[str]] = {}
    coref_canonical: dict[str, str] = {}

    for line_no, raw_line in enumerate(physical_lines[1:], start=2):
        if raw_line == "":
            continue
        if raw_line.startswith("#PREFIX|"):
            fields = _split(raw_line, line_no)
            _expect_count(fields, 3, f"line:{line_no}", line_no)
            _, semantic_raw, prefix_raw = fields
            semantic = unescape_field(semantic_raw, line_no=line_no)
            prefix = unescape_field(prefix_raw, line_no=line_no)
            if semantic not in PREFIX_LAYOUT:
                fail(
                    ErrorCode.FORMAT_INVALID,
                    f"unknown semantic prefix declaration {semantic!r}",
                    path=f"line:{line_no}",
                    stage="parse",
                    format_name="line",
                    line=line_no,
                )
            if len(prefix) != 1:
                fail(
                    ErrorCode.FORMAT_INVALID,
                    "type prefix must be one character",
                    path=f"line:{line_no}",
                    stage="parse",
                    format_name="line",
                    line=line_no,
                )
            if semantic in prefixes or prefix in reverse_prefixes:
                fail(
                    ErrorCode.DUP_ID,
                    "duplicate semantic prefix declaration",
                    path=f"line:{line_no}",
                    stage="parse",
                    format_name="line",
                    line=line_no,
                )
            prefixes[semantic] = prefix
            reverse_prefixes[prefix] = semantic
            continue
        if raw_line.startswith("#"):
            fail(
                ErrorCode.FORMAT_INVALID,
                f"unknown directive {raw_line!r}",
                path=f"line:{line_no}",
                stage="parse",
                format_name="line",
                line=line_no,
            )
        if not prefixes:
            fail(
                ErrorCode.SCHEMA_MISSING,
                "type prefix declarations must precede annotation records",
                path=f"line:{line_no}",
                stage="parse",
                format_name="line",
                line=line_no,
            )
        fields = _split(raw_line, line_no)
        prefix = unescape_field(fields[0], line_no=line_no)
        semantic = reverse_prefixes.get(prefix)
        if semantic is None:
            fail(
                ErrorCode.FORMAT_INVALID,
                f"undeclared type prefix {prefix!r}",
                path=f"line:{line_no}",
                stage="parse",
                format_name="line",
                line=line_no,
            )
        decoded = [
            unescape_field(field, line_no=line_no)
            for field in fields[1:]
        ]

        if semantic == "meta":
            _expect_count(fields, 5, f"line:{line_no}", line_no)
            if meta is not None:
                fail(
                    ErrorCode.DUP_ID,
                    "meta record appears more than once",
                    path=f"line:{line_no}",
                    stage="parse",
                    format_name="line",
                    line=line_no,
                )
            meta = {
                "sample_id": decoded[0],
                "source_version": decoded[1],
                "ontology_version": decoded[2],
                "ontology_checksum": decoded[3],
            }
        elif semantic == "document":
            _expect_count(fields, 3, f"line:{line_no}", line_no)
            doc_id, doc_text = decoded
            if doc_id in documents:
                fail(
                    ErrorCode.DUP_ID,
                    f"duplicate document id {doc_id!r}",
                    path=f"line:{line_no}",
                    stage="parse",
                    format_name="line",
                    line=line_no,
                    annotation_id=doc_id,
                )
            documents[doc_id] = {"doc_id": doc_id, "text": doc_text, "nodes": [], "edges": []}
            doc_order.append(doc_id)
        elif semantic == "entity":
            _expect_count(fields, 11, f"line:{line_no}", line_no)
            doc_id, node_id, node_type, start_raw, end_raw, node_text = decoded[:6]
            canonical_id, low_raw, high_raw, coref_id = decoded[6:]
            document = _line_document(documents, doc_id, line_no, "entity")
            document["nodes"].append(
                {
                    "id": node_id,
                    "kind": "entity",
                    "type": node_type,
                    "start": _offset(start_raw, f"line:{line_no}.start", line_no),
                    "end": _offset(end_raw, f"line:{line_no}.end", line_no),
                    "text": node_text,
                    "confidence": _confidence(
                        low_raw,
                        high_raw,
                        f"line:{line_no}.confidence",
                        line_no,
                    ).to_dict(),
                    "canonical_id": canonical_id,
                }
            )
            if coref_id:
                coref_members.setdefault(coref_id, []).append(node_id)
                existing = coref_canonical.get(coref_id)
                if existing is None:
                    coref_canonical[coref_id] = canonical_id
                elif existing != canonical_id:
                    fail(
                        ErrorCode.INCONSISTENT,
                        "coref members disagree on canonical_id",
                        path=f"line:{line_no}.canonical_id",
                        stage="parse",
                        format_name="line",
                        line=line_no,
                        annotation_id=coref_id,
                    )
        elif semantic == "trigger":
            _expect_count(fields, 10, f"line:{line_no}", line_no)
            doc_id, node_id, node_type, start_raw, end_raw, node_text, event_id = decoded[:7]
            low_raw, high_raw = decoded[7:]
            document = _line_document(documents, doc_id, line_no, "trigger")
            document["nodes"].append(
                {
                    "id": node_id,
                    "kind": "trigger",
                    "type": node_type,
                    "start": _offset(start_raw, f"line:{line_no}.start", line_no),
                    "end": _offset(end_raw, f"line:{line_no}.end", line_no),
                    "text": node_text,
                    "confidence": _confidence(
                        low_raw,
                        high_raw,
                        f"line:{line_no}.confidence",
                        line_no,
                    ).to_dict(),
                    "event_id": event_id,
                }
            )
        elif semantic == "relation":
            _expect_count(fields, 11, f"line:{line_no}", line_no)
            (
                doc_id,
                edge_id,
                edge_type,
                start_raw,
                end_raw,
                edge_text,
                source,
                target,
                low_raw,
                high_raw,
            ) = decoded
            document = _line_document(documents, doc_id, line_no, "relation")
            document["edges"].append(
                {
                    "id": edge_id,
                    "kind": "relation",
                    "type": edge_type,
                    "source": source,
                    "target": target,
                    "confidence": _confidence(
                        low_raw,
                        high_raw,
                        f"line:{line_no}.confidence",
                        line_no,
                    ).to_dict(),
                    "_line_span": [start_raw, end_raw, edge_text, line_no],
                }
            )
        elif semantic == "argument":
            _expect_count(fields, 12, f"line:{line_no}", line_no)
            (
                doc_id,
                edge_id,
                role,
                start_raw,
                end_raw,
                edge_text,
                source,
                target,
                event_id,
                low_raw,
                high_raw,
            ) = decoded
            document = _line_document(documents, doc_id, line_no, "argument")
            document["edges"].append(
                {
                    "id": edge_id,
                    "kind": "argument",
                    "role": role,
                    "source": source,
                    "target": target,
                    "event_id": event_id,
                    "confidence": _confidence(
                        low_raw,
                        high_raw,
                        f"line:{line_no}.confidence",
                        line_no,
                    ).to_dict(),
                    "_line_span": [start_raw, end_raw, edge_text, line_no],
                }
            )

    missing = [name for name in PREFIX_LAYOUT if name not in prefixes]
    if missing:
        fail(
            ErrorCode.SCHEMA_MISSING,
            "missing type prefix declarations",
            path="$",
            stage="parse",
            format_name="line",
            details={"missing": missing},
        )
    if meta is None:
        fail(
            ErrorCode.SCHEMA_MISSING,
            "missing meta record",
            path="$",
            stage="parse",
            format_name="line",
        )
    if not documents:
        fail(
            ErrorCode.SCHEMA_MISSING,
            "no document records",
            path="$",
            stage="parse",
            format_name="line",
        )

    document_rows = [documents[doc_id] for doc_id in doc_order]
    _validate_line_edge_spans(document_rows)
    coref_rows = [
        {
            "id": coref_id,
            "canonical_id": coref_canonical[coref_id],
            "members": members,
        }
        for coref_id, members in sorted(coref_members.items())
    ]
    payload = {
        "version": "v2",
        "sample_id": meta["sample_id"],
        "documents": document_rows,
        "coref": coref_rows,
        "line_metadata": {
            "source_version": meta["source_version"],
            "ontology_version": meta["ontology_version"],
            "ontology_checksum": meta["ontology_checksum"],
        },
    }
    from .reader import parse_v2

    return parse_v2(payload, ontology=ontology, format_name="line")


def _line_document(
    documents: dict[str, dict[str, Any]],
    doc_id: str,
    line_no: int,
    record: str,
) -> dict[str, Any]:
    if doc_id not in documents:
        fail(
            ErrorCode.DANGLING_REF,
            f"{record} record references an undeclared document",
            path=f"line:{line_no}.doc_id",
            stage="parse",
            format_name="line",
            line=line_no,
            annotation_id=doc_id,
        )
    return documents[doc_id]


def _validate_line_edge_spans(document_rows: list[dict[str, Any]]) -> None:
    for document in document_rows:
        node_lookup = {node["id"]: node for node in document["nodes"]}
        for edge in document["edges"]:
            line_span = edge.pop("_line_span", None)
            if line_span is None:
                continue
            start_raw, end_raw, text, line_no = line_span
            start = _offset(start_raw, f"line:{line_no}.start", line_no)
            end = _offset(end_raw, f"line:{line_no}.end", line_no)
            if edge["kind"] == "relation":
                source = node_lookup.get(edge["source"])
                target = node_lookup.get(edge["target"])
                if source is None or target is None:
                    # The canonical parser reports the precise dangling endpoint.
                    continue
                expected_start = min(source["start"], target["start"])
                expected_end = max(source["end"], target["end"])
            else:
                source = node_lookup.get(edge["source"])
                if source is None:
                    continue
                expected_start = source["start"]
                expected_end = source["end"]
            expected_text = document["text"][expected_start:expected_end]
            if (start, end, text) != (expected_start, expected_end, expected_text):
                fail(
                    ErrorCode.INCONSISTENT,
                    "line edge span/text does not match its referenced nodes",
                    path=f"line:{line_no}",
                    stage="parse",
                    format_name="line",
                    line=line_no,
                    annotation_id=edge["id"],
                    details={
                        "expected": {
                            "start": expected_start,
                            "end": expected_end,
                            "text": expected_text,
                        },
                        "actual": {"start": start, "end": end, "text": text},
                    },
                )


def assert_line_roundtrip(
    sample: V2Sample,
    *,
    ontology: OntologySnapshot,
) -> str:
    """Export/import and fail with the first semantic mismatch."""

    from .consistency import assert_same_full_semantics

    exported = export_line(sample, ontology=ontology)
    imported = parse_line_format(exported, ontology=ontology)
    assert_same_full_semantics(
        sample,
        imported,
        stage="line_roundtrip",
    )
    return exported

