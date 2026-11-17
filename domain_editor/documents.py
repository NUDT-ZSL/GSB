"""Annotated document JSONL loading."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from .errors import (
    E_JSON_INVALID,
    E_SPAN_ORDER,
    DuplicateIdError,
    JsonInvalidError,
    SpanOrderError,
)
from .model import Document, Failure, Span
from .parallel import Task, execute_ordered


def _parse_document_line(line_index: int, line: str) -> Document | None:
    if not line.strip():
        return None
    try:
        value = json.loads(line)
    except json.JSONDecodeError as exc:
        raise JsonInvalidError(f"invalid document JSON at line {line_index + 1}: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise JsonInvalidError("document line must be a JSON object")
    for key in ("doc_id", "tokens", "token_positions"):
        if key not in value:
            raise JsonInvalidError(f"document missing key {key!r}")
    doc_id = str(value["doc_id"])
    if not doc_id:
        raise JsonInvalidError("document doc_id must not be empty")
    tokens = value["tokens"]
    positions = value["token_positions"]
    if not isinstance(tokens, list) or not all(isinstance(token, str) for token in tokens):
        raise JsonInvalidError("tokens must be an array of strings")
    if not isinstance(positions, list):
        raise JsonInvalidError("token_positions must be an array")
    if len(tokens) != len(positions):
        raise JsonInvalidError("tokens and token_positions length mismatch")
    spans: list[Span] = []
    previous: Span | None = None
    for index, pair in enumerate(positions):
        if not isinstance(pair, list) or len(pair) != 2:
            raise JsonInvalidError(f"token_positions[{index}] must be a two-item array")
        start, end = pair
        if isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int) or not isinstance(end, int):
            raise JsonInvalidError(f"token_positions[{index}] must contain integers")
        if start < 0 or end < start:
            raise SpanOrderError(f"token_positions[{index}] has invalid order or negative offset")
        span = Span(Decimal(start), Decimal(end))
        if previous is not None and span.start < previous.end:
            raise SpanOrderError(f"token_positions[{index}] is not increasing")
        spans.append(span)
        previous = span
    metadata = value.get("metadata", {})
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        raise JsonInvalidError("metadata must be an object")
    metadata = dict(metadata)
    if "text" in value:
        metadata.setdefault("text", str(value["text"]))
    return Document(doc_id=doc_id, tokens=list(tokens), token_positions=spans, metadata=metadata)


def load_documents(path: str | Path, max_workers: int) -> tuple[dict[str, Document], list[Failure]]:
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        failure = Failure.from_exception(exc, stage="parse:documents")
        return {}, [failure]

    def task_for(index: int, line: str):
        def run() -> tuple[int, Document | None, Failure | None]:
            try:
                return index, _parse_document_line(index, line), None
            except BaseException as exc:
                doc_id = ""
                try:
                    candidate = json.loads(line)
                    if isinstance(candidate, dict):
                        doc_id = str(candidate.get("doc_id", ""))
                except Exception:
                    pass
                failure = Failure.from_exception(
                    exc,
                    stage="parse:documents",
                    doc_id=doc_id,
                    entry_index=index,
                    entry_id=doc_id or None,
                )
                return index, None, failure

        return Task((index, 0, "parse:documents"), run, "documents")

    tasks = [task_for(index, line) for index, line in enumerate(lines) if line.strip()]
    results, _ = execute_ordered(tasks, max_workers)
    parsed: list[tuple[int, Document]] = []
    failures: list[Failure] = []
    for result in results:
        if result.exception is not None:
            line_index = int(result.key[0])
            failures.append(
                Failure.from_exception(result.exception, stage="parse:documents", entry_index=line_index)
            )
            continue
        line_index, document, failure = result.value
        if failure is not None:
            failures.append(failure)
        if document is not None:
            parsed.append((line_index, document))

    parsed.sort(key=lambda item: (item[1].doc_id, item[0]))
    documents: dict[str, Document] = {}
    for line_index, document in parsed:
        if document.doc_id in documents:
            failures.append(
                Failure(
                    business_code=DuplicateIdError.code,
                    stage="parse:documents",
                    exception_type=DuplicateIdError.__name__,
                    message=f"duplicate document id {document.doc_id!r}",
                    doc_id=document.doc_id,
                    entry_index=line_index,
                    entry_id=document.doc_id,
                )
            )
            continue
        documents[document.doc_id] = document
    documents = {doc_id: documents[doc_id] for doc_id in sorted(documents)}
    failures.sort(key=Failure.sort_key)
    return documents, failures
