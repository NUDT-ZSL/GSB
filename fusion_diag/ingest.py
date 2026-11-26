from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .canonical import atomic_write_json, digest_json, stable_float
from .errors import FormatCorrupt


@dataclass(frozen=True)
class DocTerm:
    term: str
    tf: int


@dataclass(frozen=True)
class Document:
    doc_id: str
    terms: tuple[DocTerm, ...]
    doc_len: int
    category: str
    quality_score: float
    line: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "terms": [{"term": item.term, "tf": item.tf} for item in self.terms],
            "doc_len": self.doc_len,
            "category": self.category,
            "quality_score": stable_float(self.quality_score),
        }

    @property
    def term_tf(self) -> dict[str, int]:
        return {item.term: item.tf for item in self.terms}


@dataclass(frozen=True)
class Query:
    query_id: str
    terms: tuple[str, ...]
    top_k: int
    filters: dict[str, Any]
    line: int

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "query_id": self.query_id,
            "terms": list(self.terms),
            "top_k": self.top_k,
        }
        if self.filters:
            payload["filters"] = self.filters
        return payload

    def content_dict(self) -> dict[str, Any]:
        return {
            "query_id": self.query_id,
            "terms": list(self.terms),
            "top_k": self.top_k,
            "filters": self.filters,
        }


@dataclass
class PreparedCorpus:
    documents: tuple[Document, ...]
    all_documents: tuple[Document, ...]
    doc_by_id: dict[str, Document]
    postings: dict[str, dict[str, int]]
    category_docs: dict[str, tuple[str, ...]]
    vocab: tuple[str, ...]
    categories: tuple[str, ...]
    avgdl: float
    total_terms: int
    stats: dict[str, Any]
    content_hash: str
    glossary_hash: str

    def matches_filter(self, doc: Document, filters: dict[str, Any]) -> bool:
        category = filters.get("category")
        if category is not None and doc.category != category:
            return False
        minimum = filters.get("min_quality")
        if minimum is not None and doc.quality_score < float(minimum):
            return False
        return True

    def prepared_payload(self) -> dict[str, Any]:
        return {
            "schema_version": "prepared_corpus.v1",
            "documents": [
                doc.to_dict()
                for doc in sorted(
                    self.documents,
                    key=lambda item: (
                        item.category,
                        -item.quality_score,
                        item.doc_id,
                    ),
                )
            ],
            "stats": self.stats,
            "content_hash": self.content_hash,
            "glossary_hash": self.glossary_hash,
        }


def _require_object(value: Any, *, line: int, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FormatCorrupt(
            "JSON line must contain an object", line=line, field=field
        )
    return value


def _require_string(
    obj: dict[str, Any],
    key: str,
    *,
    line: int,
    prefix: str = "",
    allow_empty: bool = False,
) -> str:
    value = obj.get(key)
    field = f"{prefix}{key}"
    if not isinstance(value, str):
        raise FormatCorrupt("value must be a string", line=line, field=field)
    if not allow_empty and not value:
        raise FormatCorrupt("value must not be empty", line=line, field=field)
    return value


def _require_positive_int(
    obj: dict[str, Any], key: str, *, line: int, prefix: str = ""
) -> int:
    value = obj.get(key)
    field = f"{prefix}{key}"
    if isinstance(value, bool) or not isinstance(value, int):
        raise FormatCorrupt(
            "value must be a positive integer", line=line, field=field
        )
    if value <= 0:
        raise FormatCorrupt(
            "value must be a positive integer", line=line, field=field
        )
    return value


def _read_json_lines(path: str | Path) -> list[tuple[int, Any]]:
    source = Path(path)
    rows: list[tuple[int, Any]] = []
    try:
        handle = source.open("r", encoding="utf-8")
    except OSError as exc:
        raise FormatCorrupt(
            f"cannot read input file: {exc}", field="path"
        ) from exc
    with handle:
        for line_number, raw in enumerate(handle, start=1):
            if not raw.strip():
                continue
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise FormatCorrupt(
                    f"invalid JSON: {exc.msg}",
                    line=line_number,
                    field="json",
                ) from exc
            rows.append((line_number, value))
    return rows


def load_vocabulary(path: str | Path) -> tuple[str, ...]:
    source = Path(path)
    try:
        terms = [
            line.strip()
            for line in source.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except OSError as exc:
        raise FormatCorrupt(
            f"cannot read vocabulary: {exc}", field="vocab_path"
        ) from exc
    if not terms:
        raise FormatCorrupt("vocabulary must not be empty", field="vocab")
    if len(set(terms)) != len(terms):
        raise FormatCorrupt("vocabulary contains duplicate terms", field="vocab")
    return tuple(sorted(terms))


def _parse_doc_term(
    value: Any, *, line: int, index: int, vocab: set[str]
) -> DocTerm:
    item = _require_object(value, line=line, field=f"terms[{index}]")
    term = _require_string(
        item, "term", line=line, prefix=f"terms[{index}]."
    )
    tf = _require_positive_int(
        item, "tf", line=line, prefix=f"terms[{index}]."
    )
    if term not in vocab:
        raise FormatCorrupt(
            "term is outside the controlled vocabulary",
            line=line,
            field=f"terms[{index}].term",
        )
    return DocTerm(term=term, tf=tf)


def load_documents(
    path: str | Path,
    vocab: Iterable[str],
    *,
    allowed_categories: Iterable[str] | None = None,
) -> tuple[Document, ...]:
    vocab_set = set(vocab)
    category_set = set(allowed_categories) if allowed_categories is not None else None
    documents: list[Document] = []
    seen: dict[str, int] = {}
    for line_number, raw in _read_json_lines(path):
        row = _require_object(raw, line=line_number, field="document")
        doc_id = _require_string(row, "doc_id", line=line_number)
        if doc_id in seen:
            raise FormatCorrupt(
                f"duplicate doc_id (first seen on line {seen[doc_id]})",
                line=line_number,
                field="doc_id",
            )
        terms_value = row.get("terms")
        if not isinstance(terms_value, list):
            raise FormatCorrupt(
                "value must be a list", line=line_number, field="terms"
            )
        terms = tuple(
            _parse_doc_term(item, line=line_number, index=index, vocab=vocab_set)
            for index, item in enumerate(terms_value)
        )
        if not terms:
            raise FormatCorrupt(
                "document terms must not be empty", line=line_number, field="terms"
            )
        term_names = [item.term for item in terms]
        if len(set(term_names)) != len(term_names):
            raise FormatCorrupt(
                "document contains duplicate terms",
                line=line_number,
                field="terms",
            )
        doc_len = _require_positive_int(row, "doc_len", line=line_number)
        if any(item.tf > doc_len for item in terms):
            raise FormatCorrupt(
                "term frequency exceeds doc_len",
                line=line_number,
                field="terms",
            )
        category = _require_string(row, "category", line=line_number)
        if category_set is not None and category not in category_set:
            raise FormatCorrupt(
                "category is outside the controlled category set",
                line=line_number,
                field="category",
            )
        quality = row.get("quality_score")
        if isinstance(quality, bool) or not isinstance(quality, (int, float)):
            raise FormatCorrupt(
                "value must be a number in [0,1]",
                line=line_number,
                field="quality_score",
            )
        quality_float = float(quality)
        if not math.isfinite(quality_float) or not 0.0 <= quality_float <= 1.0:
            raise FormatCorrupt(
                "value must be a number in [0,1]",
                line=line_number,
                field="quality_score",
            )
        seen[doc_id] = line_number
        documents.append(
            Document(
                doc_id=doc_id,
                terms=terms,
                doc_len=doc_len,
                category=category,
                quality_score=quality_float,
                line=line_number,
            )
        )
    if not documents:
        raise FormatCorrupt("document batch is empty", field="documents")
    documents.sort(key=lambda item: item.doc_id)
    return tuple(documents)


def _parse_filters(
    value: Any, *, line: int, categories: set[str]
) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise FormatCorrupt(
            "filters must be an object", line=line, field="filters"
        )
    unknown = sorted(set(value) - {"category", "min_quality"})
    if unknown:
        raise FormatCorrupt(
            f"unknown filter field: {unknown[0]}", line=line, field="filters"
        )
    filters: dict[str, Any] = {}
    if "category" in value:
        category = value["category"]
        if not isinstance(category, str):
            raise FormatCorrupt(
                "filter category must be a string",
                line=line,
                field="filters.category",
            )
        if categories and category not in categories:
            raise FormatCorrupt(
                "filter category is outside the controlled category set",
                line=line,
                field="filters.category",
            )
        filters["category"] = category
    if "min_quality" in value:
        minimum = value["min_quality"]
        if isinstance(minimum, bool) or not isinstance(minimum, (int, float)):
            raise FormatCorrupt(
                "filter min_quality must be a number in [0,1]",
                line=line,
                field="filters.min_quality",
            )
        minimum_float = float(minimum)
        if not math.isfinite(minimum_float) or not 0.0 <= minimum_float <= 1.0:
            raise FormatCorrupt(
                "filter min_quality must be a number in [0,1]",
                line=line,
                field="filters.min_quality",
            )
        filters["min_quality"] = stable_float(minimum_float)
    return filters


def load_queries(
    path: str | Path, *, categories: Iterable[str] = ()
) -> tuple[Query, ...]:
    category_set = set(categories)
    queries: list[Query] = []
    seen: dict[str, int] = {}
    for line_number, raw in _read_json_lines(path):
        row = _require_object(raw, line=line_number, field="query")
        query_id = _require_string(row, "query_id", line=line_number)
        if query_id in seen:
            raise FormatCorrupt(
                f"duplicate query_id (first seen on line {seen[query_id]})",
                line=line_number,
                field="query_id",
            )
        terms_value = row.get("terms")
        if not isinstance(terms_value, list):
            raise FormatCorrupt(
                "value must be a list", line=line_number, field="terms"
            )
        if not terms_value:
            raise FormatCorrupt(
                "query terms must not be empty", line=line_number, field="terms"
            )
        terms: list[str] = []
        for index, term in enumerate(terms_value):
            if not isinstance(term, str) or not term:
                raise FormatCorrupt(
                    "query term must be a non-empty string",
                    line=line_number,
                    field=f"terms[{index}]",
                )
            terms.append(term)
        top_k = _require_positive_int(row, "top_k", line=line_number)
        filters = _parse_filters(
            row.get("filters"), line=line_number, categories=category_set
        )
        seen[query_id] = line_number
        queries.append(
            Query(
                query_id=query_id,
                terms=tuple(terms),
                top_k=top_k,
                filters=filters,
                line=line_number,
            )
        )
    if not queries:
        raise FormatCorrupt("query batch is empty", field="queries")
    queries.sort(key=lambda item: item.query_id)
    return tuple(queries)


def prepare_corpus(
    documents: Iterable[Document], vocab: Iterable[str]
) -> PreparedCorpus:
    all_documents = tuple(sorted(documents, key=lambda item: item.doc_id))
    if not all_documents:
        raise FormatCorrupt("document batch is empty", field="documents")
    vocab_tuple = tuple(sorted(vocab))
    doc_by_id = {doc.doc_id: doc for doc in all_documents}
    postings: dict[str, dict[str, int]] = {}
    category_lists: dict[str, list[str]] = {}
    total_terms = 0
    for doc in all_documents:
        total_terms += doc.doc_len
        category_lists.setdefault(doc.category, []).append(doc.doc_id)
        for item in doc.terms:
            postings.setdefault(item.term, {})[doc.doc_id] = item.tf
    presorted = tuple(
        sorted(
            all_documents,
            key=lambda item: (item.category, -item.quality_score, item.doc_id),
        )
    )
    categories = tuple(sorted(category_lists))
    category_docs = {
        category: tuple(
            doc_id
            for doc_id in sorted(
                category_lists[category],
                key=lambda doc_id: (
                    -doc_by_id[doc_id].quality_score,
                    doc_id,
                ),
            )
        )
        for category in categories
    }
    avgdl = total_terms / len(all_documents)
    stats = {
        "document_count": len(all_documents),
        "term_count": total_terms,
        "average_doc_len": stable_float(avgdl),
        "category_counts": {
            category: len(category_lists[category]) for category in categories
        },
        "posting_terms": len(postings),
        "presorted_doc_ids": [doc.doc_id for doc in presorted],
    }
    content_hash = digest_json(
        {
            "documents": [doc.to_dict() for doc in all_documents],
            "vocab": list(vocab_tuple),
        }
    )
    glossary_hash = digest_json({"vocab": list(vocab_tuple)})
    return PreparedCorpus(
        documents=presorted,
        all_documents=all_documents,
        doc_by_id=doc_by_id,
        postings=postings,
        category_docs=category_docs,
        vocab=vocab_tuple,
        categories=categories,
        avgdl=avgdl,
        total_terms=total_terms,
        stats=stats,
        content_hash=content_hash,
        glossary_hash=glossary_hash,
    )


def write_prepared(corpus: PreparedCorpus, path: str | Path) -> str:
    payload = corpus.prepared_payload()
    atomic_write_json(path, payload)
    return digest_json(payload)
