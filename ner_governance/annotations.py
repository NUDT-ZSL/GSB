"""JSONL annotation loading and invariant checks."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Iterable

from .errors import E_ANNOTATION_CORRUPT, E_INCONSISTENT, fail
from .models import AnnotationRecord


def parse_annotation(
    raw: object,
    *,
    line: int,
) -> AnnotationRecord:
    if not isinstance(raw, dict):
        fail(
            E_ANNOTATION_CORRUPT,
            "annotation line must be a JSON object",
            field="$",
            line=line,
            reason="invalid_root_type",
        )
    required = (
        "doc_id",
        "tokens",
        "ner_tags",
        "annotator_id",
        "annotation_version",
        "label_set_version",
    )
    for key in required:
        if key not in raw:
            fail(
                E_ANNOTATION_CORRUPT,
                f"missing required annotation field {key}",
                field=key,
                line=line,
                reason="required_field_missing",
            )
    doc_id = raw["doc_id"]
    tokens = raw["tokens"]
    tags = raw["ner_tags"]
    annotator = raw["annotator_id"]
    annotation_version = raw["annotation_version"]
    label_version = raw["label_set_version"]
    for field_name, value in (
        ("doc_id", doc_id),
        ("annotator_id", annotator),
        ("annotation_version", annotation_version),
        ("label_set_version", label_version),
    ):
        if not isinstance(value, str) or not value:
            fail(
                E_ANNOTATION_CORRUPT,
                f"{field_name} must be a non-empty string",
                field=field_name,
                line=line,
                reason="invalid_field",
            )
    if not isinstance(tokens, list) or not isinstance(tags, list):
        fail(
            E_ANNOTATION_CORRUPT,
            "tokens and ner_tags must be arrays",
            field="tokens/ner_tags",
            line=line,
            reason="invalid_field",
        )
    if not tokens:
        fail(
            E_ANNOTATION_CORRUPT,
            "tokens must not be empty",
            field="tokens",
            line=line,
            reason="required_field_missing",
        )
    if len(tokens) != len(tags):
        fail(
            E_ANNOTATION_CORRUPT,
            "tokens and ner_tags length mismatch",
            field="ner_tags",
            line=line,
            reason="length_mismatch",
            token_count=len(tokens),
            tag_count=len(tags),
        )
    if any(not isinstance(token, str) for token in tokens):
        fail(
            E_ANNOTATION_CORRUPT,
            "all tokens must be strings",
            field="tokens",
            line=line,
            reason="invalid_field",
        )
    if any(not isinstance(tag, str) for tag in tags):
        fail(
            E_ANNOTATION_CORRUPT,
            "all ner_tags must be strings",
            field="ner_tags",
            line=line,
            reason="invalid_field",
        )
    return AnnotationRecord(
        doc_id=doc_id,
        tokens=tuple(tokens),
        ner_tags=tuple(tags),
        annotator_id=annotator,
        annotation_version=annotation_version,
        label_set_version=label_version,
        source_line=line,
    )


def load_annotations(path: str | Path) -> list[AnnotationRecord]:
    records: list[AnnotationRecord] = []
    seen: dict[tuple[str, str], int] = {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        fail(
            E_ANNOTATION_CORRUPT,
            f"cannot read annotation file: {exc}",
            field="path",
            reason="read_error",
            source=str(path),
        )
    for index, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            fail(
                E_ANNOTATION_CORRUPT,
                f"invalid annotation JSON: {exc.msg}",
                field="$",
                line=index,
                reason="invalid_json",
                column=exc.colno,
            )
        record = parse_annotation(raw, line=index)
        key = (record.doc_id, record.annotator_id)
        if key in seen:
            fail(
                E_INCONSISTENT,
                "duplicate annotation for doc_id and annotator_id",
                field="annotator_id",
                line=index,
                doc_id=record.doc_id,
                reason="duplicate_annotation",
                first_line=seen[key],
            )
        seen[key] = index
        records.append(record)
    if not records:
        fail(
            E_ANNOTATION_CORRUPT,
            "annotation file is empty",
            field="records",
            line=1,
            reason="required_field_missing",
        )
    return records


def group_by_doc(
    records: Iterable[AnnotationRecord],
) -> dict[str, list[AnnotationRecord]]:
    grouped: dict[str, list[AnnotationRecord]] = defaultdict(list)
    for record in records:
        grouped[record.doc_id].append(record)
    return {
        doc_id: sorted(items, key=lambda item: item.annotator_id)
        for doc_id, items in sorted(grouped.items())
    }


def validate_document_sets(records: Iterable[AnnotationRecord]) -> None:
    by_annotator: dict[str, set[str]] = defaultdict(set)
    for record in records:
        by_annotator[record.annotator_id].add(record.doc_id)
    if len(by_annotator) < 2:
        return
    expected = set(next(iter(by_annotator.values())))
    for annotator in sorted(by_annotator):
        actual = by_annotator[annotator]
        if actual == expected:
            continue
        difference = sorted(expected.symmetric_difference(actual))
        doc_id = difference[0] if difference else sorted(expected)[0]
        fail(
            E_INCONSISTENT,
            f"annotator {annotator!r} has an inconsistent doc_id set",
            doc_id=doc_id,
            reason="annotator_doc_id_set_mismatch",
            annotator_id=annotator,
            missing_doc_ids=sorted(expected - actual),
            unexpected_doc_ids=sorted(actual - expected),
        )


def validate_documents(
    grouped: dict[str, list[AnnotationRecord]],
) -> None:
    for doc_id in sorted(grouped):
        items = grouped[doc_id]
        token_sequences = {item.tokens for item in items}
        if len(token_sequences) != 1:
            fail(
                E_INCONSISTENT,
                "annotators disagree on the token sequence",
                doc_id=doc_id,
                reason="token_sequence_mismatch",
                annotator_ids=[item.annotator_id for item in items],
            )
        lengths = {len(item.ner_tags) for item in items}
        if len(lengths) != 1:
            fail(
                E_INCONSISTENT,
                "annotators disagree on the tag-array length",
                doc_id=doc_id,
                reason="tag_length_mismatch",
                tag_counts=sorted(lengths),
            )


def annotations_by_doc_and_annotator(
    records: Iterable[AnnotationRecord],
) -> dict[str, dict[str, AnnotationRecord]]:
    result: dict[str, dict[str, AnnotationRecord]] = {}
    for record in records:
        result.setdefault(record.doc_id, {})[record.annotator_id] = record
    return {
        doc_id: {annotator: values[annotator] for annotator in sorted(values)}
        for doc_id, values in sorted(result.items())
    }
