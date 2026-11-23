"""Input models and deterministic validation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable

from .canonical import stable_float
from .errors import (
    E_INPUT,
    E_TERM_INVALID,
    E_TERM_UNKNOWN,
    RecallError,
)


def _require_int(value: Any, name: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RecallError(E_INPUT, "ingest", f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise RecallError(E_INPUT, "ingest", f"{name} must be >= {minimum}")
    return value


def normalize_terms(raw: Any, weight_terms: set[str] | None = None) -> dict[str, float]:
    """Normalize dict/list term-frequency input into a sorted term map."""

    counts: dict[str, float] = {}
    if isinstance(raw, dict):
        pairs: Iterable[tuple[Any, Any]] = raw.items()
    elif isinstance(raw, list):
        if all(isinstance(item, str) for item in raw):
            pairs = ((item, 1) for item in raw)
        elif all(isinstance(item, (list, tuple)) and len(item) == 2 for item in raw):
            pairs = ((item[0], item[1]) for item in raw)
        else:
            raise RecallError(
                E_TERM_INVALID,
                "ingest",
                "terms list must contain strings or [term, frequency] pairs",
            )
    else:
        raise RecallError(E_TERM_INVALID, "ingest", "terms must be an object or list")

    for term, frequency in pairs:
        if not isinstance(term, str) or not term:
            raise RecallError(E_TERM_INVALID, "ingest", "each term must be a non-empty string")
        if isinstance(frequency, bool) or not isinstance(frequency, (int, float)):
            raise RecallError(
                E_TERM_INVALID,
                "ingest",
                f"term frequency for {term!r} must be numeric",
            )
        numeric = float(frequency)
        if not math.isfinite(numeric) or numeric <= 0:
            raise RecallError(
                E_TERM_INVALID,
                "ingest",
                f"term frequency for {term!r} must be finite and positive",
            )
        if weight_terms is not None and term not in weight_terms:
            raise RecallError(
                E_TERM_UNKNOWN,
                "ingest",
                f"term {term!r} is absent from the weight table",
                details={"term": term},
            )
        counts[term] = counts.get(term, 0.0) + numeric

    if not counts:
        raise RecallError(E_TERM_INVALID, "ingest", "terms must not be empty")
    return {term: stable_float(counts[term]) for term in sorted(counts)}


@dataclass(frozen=True)
class Document:
    doc_id: str
    batch_no: int
    terms: dict[str, float]
    doc_len: int

    @property
    def batch_key(self) -> str:
        return f"doc:{self.batch_no}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "batch_no": self.batch_no,
            "doc_len": self.doc_len,
            "terms": self.terms,
        }


@dataclass(frozen=True)
class Query:
    query_id: str
    batch_no: int
    terms: dict[str, float]
    top_k: int
    priority: int = 0

    @property
    def batch_key(self) -> str:
        return f"query:{self.batch_no}"

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "query_id": self.query_id,
            "batch_no": self.batch_no,
            "terms": self.terms,
            "top_k": self.top_k,
        }
        if self.priority:
            result["priority"] = self.priority
        return result


def parse_document(
    raw: dict[str, Any],
    *,
    weight_terms: set[str] | None = None,
) -> Document:
    if not isinstance(raw, dict):
        raise RecallError(E_INPUT, "ingest", "document row must be an object")
    doc_id = raw.get("doc_id")
    if not isinstance(doc_id, str) or not doc_id:
        raise RecallError(E_INPUT, "ingest", "doc_id must be a non-empty string")
    batch_no = _require_int(raw.get("batch_no"), "batch_no", minimum=0)
    doc_len = _require_int(raw.get("doc_len"), "doc_len", minimum=1)
    try:
        terms = normalize_terms(raw.get("terms"), weight_terms)
    except RecallError as exc:
        exc.batch_no = batch_no
        exc.batch_kind = "doc"
        raise
    term_total = math.fsum(terms.values())
    if abs(term_total - doc_len) > 1.0e-9:
        raise RecallError(
            E_TERM_INVALID,
            "ingest",
            "doc_len must equal the sum of term frequencies",
            batch_no=batch_no,
            batch_kind="doc",
            details={"doc_len": doc_len, "term_total": stable_float(term_total)},
        )
    return Document(doc_id=doc_id, batch_no=batch_no, terms=terms, doc_len=doc_len)


def parse_query(
    raw: dict[str, Any],
    *,
    weight_terms: set[str] | None = None,
) -> Query:
    if not isinstance(raw, dict):
        raise RecallError(E_INPUT, "ingest", "query row must be an object")
    query_id = raw.get("query_id")
    if not isinstance(query_id, str) or not query_id:
        raise RecallError(E_INPUT, "ingest", "query_id must be a non-empty string")
    batch_no = _require_int(raw.get("batch_no"), "batch_no", minimum=0)
    top_k = _require_int(raw.get("top_k"), "top_k", minimum=1)
    priority_raw = raw.get("priority", 0)
    priority = 0 if priority_raw is None else _require_int(priority_raw, "priority")
    try:
        terms = normalize_terms(raw.get("terms"), weight_terms)
    except RecallError as exc:
        exc.batch_no = batch_no
        exc.batch_kind = "query"
        raise
    return Query(
        query_id=query_id,
        batch_no=batch_no,
        terms=terms,
        top_k=top_k,
        priority=priority,
    )
