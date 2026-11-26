"""Deterministic JSONL document parsing, validation, and token analysis."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from .config import NormalizedConfig, normalize_term
from .errors import (
    DiagnosticError,
    E_BAD_DOC,
    E_BATCH_ROLLBACK,
    E_DUP_DOC,
)
from .resources import ResourceGuard
from .util import canonical_json, sha256_json


DocId = int | str


def doc_sort_key(doc_id: DocId) -> tuple[int, Any]:
    if isinstance(doc_id, bool):
        raise ValueError("boolean doc_id is invalid")
    if isinstance(doc_id, int):
        if doc_id < 0:
            raise ValueError("integer doc_id must be non-negative")
        return (0, doc_id)
    if isinstance(doc_id, str) and doc_id:
        return (1, doc_id)
    raise ValueError("doc_id must be a positive integer or non-empty string")


def _validate_doc_id(value: Any) -> DocId:
    if isinstance(value, bool):
        raise ValueError("doc_id must not be boolean")
    if isinstance(value, int):
        if value < 0:
            raise ValueError("integer doc_id must be >= 0")
        return value
    if isinstance(value, str) and value:
        return value
    raise ValueError("doc_id must be a non-negative integer or non-empty string")


def _validate_batch_no(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("batch_no must be a non-negative integer")
    return value


def _extract_terms(document: Mapping[str, Any]) -> list[Any]:
    for field_name in ("terms", "term_stream", "tokens"):
        if field_name in document:
            value = document[field_name]
            if not isinstance(value, list):
                raise ValueError(f"{field_name} must be an array")
            return value
    raise ValueError("terms field is missing")


def _extract_frequencies(
    document: Mapping[str, Any],
) -> tuple[dict[str, int] | None, str | None]:
    for field_name in ("term_frequencies", "term_freqs", "tf"):
        if field_name in document:
            value = document[field_name]
            if not isinstance(value, dict):
                raise ValueError(f"{field_name} must be an object")
            return dict(value), field_name
    return None, None


def _extract_positions(
    document: Mapping[str, Any],
) -> tuple[dict[str, list[int]] | None, str | None]:
    for field_name in ("positions", "term_positions"):
        if field_name in document:
            value = document[field_name]
            if not isinstance(value, dict):
                raise ValueError(f"{field_name} must be an object")
            return dict(value), field_name
    return None, None


@dataclass(frozen=True)
class AnalyzedDoc:
    batch_no: int
    doc_id: DocId
    terms: tuple[str, ...]
    term_frequencies: tuple[tuple[str, int], ...]
    positions: tuple[tuple[str, tuple[int, ...]], ...]
    doc_length: int
    content_hash: str
    summary_hash: str

    @property
    def frequency_map(self) -> dict[str, int]:
        return dict(self.term_frequencies)

    @property
    def position_map(self) -> dict[str, tuple[int, ...]]:
        return dict(self.positions)

    def canonical_summary(self) -> dict[str, Any]:
        return {
            "batch_no": self.batch_no,
            "content_hash": self.content_hash,
            "doc_id": self.doc_id,
            "doc_length": self.doc_length,
            "positions": [[term, list(pos)] for term, pos in self.positions],
            "term_frequencies": [[term, count] for term, count in self.term_frequencies],
            "terms": list(self.terms),
        }

    def to_dict(self) -> dict[str, Any]:
        result = self.canonical_summary()
        result["summary_hash"] = self.summary_hash
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AnalyzedDoc":
        doc = cls(
            batch_no=_validate_batch_no(value["batch_no"]),
            doc_id=_validate_doc_id(value["doc_id"]),
            terms=tuple(str(term) for term in value["terms"]),
            term_frequencies=tuple(
                (str(term), int(count)) for term, count in value["term_frequencies"]
            ),
            positions=tuple(
                (str(term), tuple(int(position) for position in positions))
                for term, positions in value["positions"]
            ),
            doc_length=int(value["doc_length"]),
            content_hash=str(value["content_hash"]),
            summary_hash=str(value["summary_hash"]),
        )
        if doc.summary_hash != sha256_json(doc.canonical_summary()):
            raise DiagnosticError(
                E_BAD_DOC,
                "stored document summary hash mismatch",
                {"doc_id": doc.doc_id, "batch_no": doc.batch_no},
            )
        return doc


def analyze_document(
    document: Mapping[str, Any],
    vocabulary: frozenset[str] | set[str],
    *,
    source: str = "<memory>",
    line_number: int | None = None,
) -> AnalyzedDoc:
    doc_id: DocId | None = None
    batch_no: int | None = None
    try:
        if not isinstance(document, Mapping):
            raise ValueError("document must be a JSON object")
        batch_no = _validate_batch_no(document.get("batch_no"))
        doc_id = _validate_doc_id(document.get("doc_id"))
        raw_terms = _extract_terms(document)
        normalized_terms: list[str] = []
        positions_map: dict[str, list[int]] = {}
        for position, raw_term in enumerate(raw_terms):
            try:
                term = normalize_term(raw_term)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid term at position {position}: {exc}") from exc
            if term not in vocabulary:
                raise ValueError(f"term {term!r} is outside the controlled vocabulary")
            normalized_terms.append(term)
            positions_map.setdefault(term, []).append(position)

        doc_length = document.get("doc_length", document.get("length"))
        if isinstance(doc_length, bool) or not isinstance(doc_length, int):
            raise ValueError("doc_length must be an integer")
        if doc_length != len(normalized_terms):
            raise ValueError(
                f"doc_length {doc_length} does not equal term count {len(normalized_terms)}"
            )

        raw_frequencies, frequency_field = _extract_frequencies(document)
        if raw_frequencies is None:
            frequency_map = {
                term: len(positions) for term, positions in positions_map.items()
            }
        else:
            frequency_map = {}
            for raw_term, raw_count in raw_frequencies.items():
                try:
                    term = normalize_term(raw_term)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"invalid {frequency_field} key: {exc}") from exc
                if term in frequency_map:
                    raise ValueError(
                        f"duplicate term {term!r} after normalization in {frequency_field}"
                    )
                if (
                    isinstance(raw_count, bool)
                    or not isinstance(raw_count, int)
                    or raw_count <= 0
                ):
                    raise ValueError(f"frequency for {term!r} must be a positive integer")
                if term not in positions_map:
                    raise ValueError(
                        f"{frequency_field} contains absent term {term!r}"
                    )
                frequency_map[term] = raw_count
            if set(frequency_map) != set(positions_map):
                missing = sorted(set(positions_map) - set(frequency_map))
                raise ValueError(f"{frequency_field} is missing terms {missing}")

        for term, positions in positions_map.items():
            if frequency_map.get(term) != len(positions):
                raise ValueError(
                    f"frequency for {term!r} does not match term stream occurrences"
                )
        frequency_sum = sum(frequency_map.values())
        if frequency_sum != doc_length:
            raise ValueError(
                f"term frequency sum {frequency_sum} does not equal doc_length {doc_length}"
            )

        raw_positions, positions_field = _extract_positions(document)
        if raw_positions is not None:
            supplied_positions: dict[str, list[int]] = {}
            for raw_term, raw_values in raw_positions.items():
                try:
                    term = normalize_term(raw_term)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"invalid {positions_field} key: {exc}") from exc
                if term in supplied_positions:
                    raise ValueError(
                        f"duplicate term {term!r} after normalization in {positions_field}"
                    )
                if not isinstance(raw_values, list) or any(
                    isinstance(item, bool) or not isinstance(item, int) or item < 0
                    for item in raw_values
                ):
                    raise ValueError(
                        f"positions for {term!r} must be an array of non-negative integers"
                    )
                supplied_positions[term] = raw_values
            if supplied_positions != positions_map:
                raise ValueError(
                    f"{positions_field} does not match positions derived from term stream"
                )

        content_hash = document.get(
            "content_hash", document.get("content_hash_hex", document.get("hash"))
        )
        if not isinstance(content_hash, str) or not content_hash:
            raise ValueError("content_hash must be a non-empty string")

        ordered_frequencies = tuple(sorted(frequency_map.items()))
        ordered_positions = tuple(
            (term, tuple(positions_map[term])) for term in sorted(positions_map)
        )
        body = {
            "batch_no": batch_no,
            "content_hash": content_hash,
            "doc_id": doc_id,
            "doc_length": doc_length,
            "positions": [[term, list(pos)] for term, pos in ordered_positions],
            "term_frequencies": [[term, count] for term, count in ordered_frequencies],
            "terms": normalized_terms,
        }
        return AnalyzedDoc(
            batch_no=batch_no,
            doc_id=doc_id,
            terms=tuple(normalized_terms),
            term_frequencies=ordered_frequencies,
            positions=ordered_positions,
            doc_length=doc_length,
            content_hash=content_hash,
            summary_hash=sha256_json(body),
        )
    except DiagnosticError:
        raise
    except (TypeError, ValueError, KeyError) as exc:
        details: dict[str, Any] = {"source": source}
        if doc_id is not None:
            details["doc_id"] = doc_id
        if batch_no is not None:
            details["batch_no"] = batch_no
        if line_number is not None:
            details["line"] = line_number
        raise DiagnosticError(E_BAD_DOC, str(exc), details) from exc


def read_jsonl(path: str | Path) -> list[tuple[int, dict[str, Any]]]:
    result: list[tuple[int, dict[str, Any]]] = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise DiagnosticError(
                        E_BAD_DOC,
                        f"invalid JSONL document: {exc.msg}",
                        {"source": str(path), "line": line_number, "doc_id": None},
                    ) from exc
                if not isinstance(value, dict):
                    raise DiagnosticError(
                        E_BAD_DOC,
                        "JSONL document must be an object",
                        {"source": str(path), "line": line_number, "doc_id": None},
                    )
                result.append((line_number, value))
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise DiagnosticError(
            E_BAD_DOC,
            f"cannot read JSONL source: {exc}",
            {"source": str(path), "doc_id": None},
        ) from exc
    return result


@dataclass
class AnalysisResult:
    documents: list[AnalyzedDoc]
    skipped_batches: list[int]
    batches: list[int]
    observed_batches: list[int] = field(default_factory=list)
    resource_actions: list[dict[str, Any]] = field(default_factory=list)
    analyzed_count: int = 0

    def statistics(self) -> dict[str, Any]:
        term_frequencies: dict[str, int] = {}
        for doc in self.documents:
            for term, count in doc.term_frequencies:
                term_frequencies[term] = term_frequencies.get(term, 0) + count
        summary = {
            "analyzed_count": self.analyzed_count,
            "batches": list(self.batches),
            "document_ids": [doc.doc_id for doc in self.documents],
            "term_frequencies": dict(sorted(term_frequencies.items())),
            "total_terms": sum(len(doc.terms) for doc in self.documents),
        }
        summary["digest"] = sha256_json(summary)
        return summary


def analyze_jsonl_files(
    paths: Iterable[str | Path],
    config: NormalizedConfig,
    *,
    existing_doc_ids: Iterable[DocId] = (),
    skip_batches: Iterable[int] = (),
    last_batch_no: int | None = None,
    resource_guard: ResourceGuard | None = None,
) -> AnalysisResult:
    """Parse sorted JSONL inputs; fully analyze only non-skipped batches."""

    path_list = [str(path) for path in paths]
    vocabulary = config.vocabulary_set
    skipped = set(skip_batches)
    existing_ids = set(existing_doc_ids)
    seen_new_ids: set[DocId] = set()
    guard = resource_guard or ResourceGuard(config.resource_limits)
    documents: list[AnalyzedDoc] = []
    skipped_seen: set[int] = set()
    batches_seen: list[int] = []
    observed_batches: set[int] = set()
    actions: list[dict[str, Any]] = []
    stream_last_batch: int | None = None
    stream_last_doc: tuple[int, Any] | None = None
    global_last_batch = last_batch_no
    batch_buffer: list[tuple[str, int, dict[str, Any]]] = []
    current_batch: int | None = None

    def flush_batch() -> None:
        nonlocal batch_buffer, global_last_batch
        if current_batch is None or not batch_buffer:
            return
        observed_batches.add(current_batch)
        if current_batch in skipped:
            skipped_seen.add(current_batch)
            batch_buffer = []
            return
        if global_last_batch is not None and current_batch < global_last_batch:
            raise DiagnosticError(
                E_BATCH_ROLLBACK,
                "new batch number is lower than the latest committed batch",
                {"batch_no": current_batch, "last_batch_no": global_last_batch},
            )
        original_count = len(batch_buffer)
        kept_count = guard.limit_batch_docs(original_count, batch_no=current_batch)
        if kept_count < original_count:
            actions.append(
                {
                    "action": "truncate_batch",
                    "batch_no": current_batch,
                    "kept_docs": kept_count,
                    "original_docs": original_count,
                }
            )
            batch_buffer = batch_buffer[:kept_count]
        for source, line_number, raw_document in batch_buffer:
            doc = analyze_document(
                raw_document,
                vocabulary,
                source=source,
                line_number=line_number,
            )
            if doc.batch_no != current_batch:
                raise DiagnosticError(
                    E_BAD_DOC,
                    "batch number changed inside batch buffer",
                    {"doc_id": doc.doc_id, "batch_no": doc.batch_no},
                )
            if doc.doc_id in existing_ids or doc.doc_id in seen_new_ids:
                raise DiagnosticError(
                    E_DUP_DOC,
                    "duplicate doc_id",
                    {"doc_id": doc.doc_id, "batch_no": doc.batch_no},
                )
            seen_new_ids.add(doc.doc_id)
            documents.append(doc)
        batches_seen.append(current_batch)
        global_last_batch = (
            current_batch
            if global_last_batch is None
            else max(global_last_batch, current_batch)
        )
        batch_buffer = []

    for path in path_list:
        for line_number, raw_document in read_jsonl(path):
            try:
                batch_no = _validate_batch_no(raw_document.get("batch_no"))
                doc_id = _validate_doc_id(raw_document.get("doc_id"))
            except (TypeError, ValueError) as exc:
                raise DiagnosticError(
                    E_BAD_DOC,
                    str(exc),
                    {
                        "source": path,
                        "line": line_number,
                        "doc_id": raw_document.get("doc_id"),
                    },
                ) from exc
            if stream_last_batch is not None and batch_no < stream_last_batch:
                raise DiagnosticError(
                    E_BATCH_ROLLBACK,
                    "input batch number decreases",
                    {"batch_no": batch_no, "previous_batch_no": stream_last_batch},
                )
            if stream_last_batch == batch_no and stream_last_doc is not None:
                if doc_sort_key(doc_id) < stream_last_doc:
                    raise DiagnosticError(
                        E_BAD_DOC,
                        "documents are not sorted by doc_id within a batch",
                        {"batch_no": batch_no, "doc_id": doc_id},
                    )
            if current_batch is None:
                current_batch = batch_no
            elif batch_no != current_batch:
                flush_batch()
                current_batch = batch_no
            batch_buffer.append((path, line_number, raw_document))
            stream_last_batch = batch_no
            stream_last_doc = doc_sort_key(doc_id)
    flush_batch()

    if not documents and not skipped_seen:
        raise DiagnosticError(
            E_BAD_DOC,
            "no documents were found in the input",
            {"doc_id": None, "sources": path_list},
        )
    return AnalysisResult(
        documents=documents,
        skipped_batches=sorted(skipped_seen),
        batches=batches_seen,
        observed_batches=sorted(observed_batches),
        resource_actions=actions,
        analyzed_count=len(documents),
    )


def canonical_documents_digest(documents: Iterable[AnalyzedDoc]) -> str:
    return sha256_json([doc.canonical_summary() for doc in documents])


def deterministic_byte_view(documents: Iterable[AnalyzedDoc]) -> bytes:
    """Useful in tests and external validation of byte-for-byte determinism."""

    return canonical_json(
        [
            {
                "summary_hash": doc.summary_hash,
                "terms": list(doc.terms),
            }
            for doc in documents
        ]
    ).encode("utf-8")
