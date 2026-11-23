"""Deterministic JSONL ingestion and term validation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import (
    E_DUPLICATE_ID,
    E_INPUT,
    RecallError,
    error_dict,
)
from .models import Document, Query, parse_document, parse_query
from .weights import WeightTable


@dataclass
class IngestResult:
    documents: list[Document] = field(default_factory=list)
    queries: list[Query] = field(default_factory=list)
    doc_batches: dict[int, list[Document]] = field(default_factory=dict)
    query_batches: dict[int, list[Query]] = field(default_factory=dict)
    validation: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)

    def documents_for_batch(self, batch_no: int) -> list[Document]:
        return self.doc_batches.get(batch_no, [])

    def queries_for_batch(self, batch_no: int) -> list[Query]:
        return self.query_batches.get(batch_no, [])

    def batch_count(self, kind: str) -> int:
        return len(self.doc_batches if kind == "doc" else self.query_batches)


def _load_jsonl_tolerant(path: str | Path) -> tuple[list[tuple[int, Any]], list[dict[str, Any]]]:
    rows: list[tuple[int, Any]] = []
    errors: list[dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                text = line.strip()
                if not text:
                    continue
                try:
                    rows.append((line_no, json.loads(text)))
                except json.JSONDecodeError as exc:
                    errors.append(
                        error_dict(
                            E_INPUT,
                            "ingest",
                            f"invalid JSON at line {line_no}: {exc.msg}",
                            details={"line": line_no, "source": str(path)},
                        )
                    )
    except FileNotFoundError as exc:
        raise RecallError(E_INPUT, "ingest", f"input file not found: {path}") from exc
    return rows, errors


def _raw_hint(raw: Any, key: str) -> Any:
    return raw.get(key) if isinstance(raw, dict) else None


def _validation_key(record: dict[str, Any]) -> tuple[int, int, str, str, int]:
    batch_no = record.get("batch_no")
    batch_order = batch_no if isinstance(batch_no, int) else 2**62
    record_id = str(record.get("record_id") or "")
    return (
        0 if record.get("status") == "error" else 1,
        batch_order,
        str(record.get("batch_kind") or ""),
        record_id,
        int(record.get("line") or 0),
    )


def ingest_dataset(
    document_path: str | Path,
    query_path: str | Path,
    weights: WeightTable,
) -> IngestResult:
    """Parse both streams independent of their physical JSONL ordering."""

    result = IngestResult()
    weight_terms = weights.term_names

    doc_rows, doc_json_errors = _load_jsonl_tolerant(document_path)
    query_rows, query_json_errors = _load_jsonl_tolerant(query_path)
    result.errors.extend(doc_json_errors)
    result.errors.extend(query_json_errors)

    doc_candidates: list[tuple[int, str, int, Document]] = []
    query_candidates: list[tuple[int, str, int, Query]] = []

    for line_no, raw in doc_rows:
        batch_no = _raw_hint(raw, "batch_no")
        doc_id = str(_raw_hint(raw, "doc_id") or "")
        try:
            document = parse_document(raw, weight_terms=weight_terms)
        except RecallError as exc:
            record = {
                "status": "error",
                "batch_kind": "doc",
                "batch_no": exc.batch_no if exc.batch_no is not None else batch_no,
                "record_id": doc_id,
                "line": line_no,
                **exc.to_dict(),
            }
            result.validation.append(record)
            result.errors.append(record)
            continue
        doc_candidates.append((document.batch_no, document.doc_id, line_no, document))

    for line_no, raw in query_rows:
        batch_no = _raw_hint(raw, "batch_no")
        query_id = str(_raw_hint(raw, "query_id") or "")
        try:
            query = parse_query(raw, weight_terms=weight_terms)
        except RecallError as exc:
            record = {
                "status": "error",
                "batch_kind": "query",
                "batch_no": exc.batch_no if exc.batch_no is not None else batch_no,
                "record_id": query_id,
                "line": line_no,
                **exc.to_dict(),
            }
            result.validation.append(record)
            result.errors.append(record)
            continue
        query_candidates.append((query.batch_no, query.query_id, line_no, query))

    doc_candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    seen_docs: set[str] = set()
    for batch_no, doc_id, line_no, document in doc_candidates:
        if doc_id in seen_docs:
            duplicate = error_dict(
                E_DUPLICATE_ID,
                "ingest",
                f"duplicate doc_id {doc_id!r}",
                batch_no=batch_no,
                batch_kind="doc",
                details={"record_id": doc_id, "line": line_no},
            )
            result.validation.append(
                {
                    "status": "error",
                    "batch_kind": "doc",
                    "batch_no": batch_no,
                    "record_id": doc_id,
                    "line": line_no,
                    **duplicate,
                }
            )
            result.errors.append(duplicate)
            continue
        seen_docs.add(doc_id)
        result.documents.append(document)
        result.doc_batches.setdefault(batch_no, []).append(document)
        result.validation.append(
            {
                "status": "accepted",
                "batch_kind": "doc",
                "batch_no": batch_no,
                "record_id": doc_id,
                "line": line_no,
                "term_count": len(document.terms),
                "doc_len": document.doc_len,
            }
        )

    query_candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    seen_queries: set[str] = set()
    for batch_no, query_id, line_no, query in query_candidates:
        if query_id in seen_queries:
            duplicate = error_dict(
                E_DUPLICATE_ID,
                "ingest",
                f"duplicate query_id {query_id!r}",
                batch_no=batch_no,
                batch_kind="query",
                details={"record_id": query_id, "line": line_no},
            )
            result.validation.append(
                {
                    "status": "error",
                    "batch_kind": "query",
                    "batch_no": batch_no,
                    "record_id": query_id,
                    "line": line_no,
                    **duplicate,
                }
            )
            result.errors.append(duplicate)
            continue
        seen_queries.add(query_id)
        result.queries.append(query)
        result.query_batches.setdefault(batch_no, []).append(query)
        result.validation.append(
            {
                "status": "accepted",
                "batch_kind": "query",
                "batch_no": batch_no,
                "record_id": query_id,
                "line": line_no,
                "term_count": len(query.terms),
                "top_k": query.top_k,
            }
        )

    for docs in result.doc_batches.values():
        docs.sort(key=lambda item: item.doc_id)
    for queries in result.query_batches.values():
        queries.sort(key=lambda item: item.query_id)
    result.documents.sort(key=lambda item: (item.batch_no, item.doc_id))
    result.queries.sort(key=lambda item: (item.batch_no, item.query_id))
    result.validation.sort(key=_validation_key)
    result.errors.sort(
        key=lambda item: (
            str(item.get("code")),
            item.get("batch_no") if isinstance(item.get("batch_no"), int) else 2**62,
            str(item.get("record_id") or ""),
        )
    )
    return result
