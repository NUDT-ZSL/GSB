"""Document and query JSONL readers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .errors import format_corrupt
from .util import read_jsonl_with_locations, require_int


def normalize_terms(
    raw: Any,
    *,
    field: str,
    location: Mapping[str, Any] | None = None,
) -> dict[str, int]:
    """Normalize either a term/frequency array or a term-to-count object."""

    location_dict = dict(location or {})
    if isinstance(raw, dict):
        pairs = list(raw.items())
    elif isinstance(raw, list):
        pairs = []
        for index, item in enumerate(raw):
            if not isinstance(item, dict) or "term" not in item or "freq" not in item:
                raise format_corrupt(
                    "term entry must contain term and freq",
                    field=field,
                    term_index=index,
                    location=location_dict,
                )
            pairs.append((item["term"], item["freq"]))
    else:
        raise format_corrupt(
            "terms must be an array or object",
            field=field,
            location=location_dict,
        )

    result: dict[str, int] = {}
    for term, frequency in pairs:
        if not isinstance(term, str) or not term:
            raise format_corrupt(
                "term must be a non-empty string",
                field=field,
                value=term,
                location=location_dict,
            )
        parsed_frequency = require_int(
            frequency,
            field=f"{field}.freq",
            minimum=1,
            location=location_dict,
        )
        result[term] = result.get(term, 0) + parsed_frequency
    return dict(sorted(result.items()))


@dataclass(frozen=True)
class Document:
    doc_id: str
    doc_lang: str
    terms: Mapping[str, int]
    doc_len: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "doc_lang": self.doc_lang,
            "terms": [{"term": term, "freq": freq} for term, freq in self.terms.items()],
            "doc_len": self.doc_len,
        }


@dataclass(frozen=True)
class Query:
    query_id: str
    query_lang: str
    terms: Mapping[str, int]
    top_k: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_id": self.query_id,
            "query_lang": self.query_lang,
            "terms": [{"term": term, "freq": freq} for term, freq in self.terms.items()],
            "top_k": self.top_k,
        }


@dataclass(frozen=True)
class LocatedDocument:
    document: Document
    source: Mapping[str, Any]


def read_documents(path: str | Path) -> list[LocatedDocument]:
    records = read_jsonl_with_locations(Path(path))
    documents: list[LocatedDocument] = []
    seen: set[str] = set()
    for raw, location in records:
        for field in ("doc_id", "doc_lang"):
            if not isinstance(raw.get(field), str) or not raw[field]:
                raise format_corrupt(
                    "document is missing a required string field",
                    field=field,
                    location=location,
                )
        if raw["doc_id"] in seen:
            raise format_corrupt(
                "duplicate doc_id",
                doc_id=raw["doc_id"],
                location=location,
            )
        seen.add(raw["doc_id"])
        terms = normalize_terms(raw.get("terms"), field="terms", location=location)
        doc_len = require_int(
            raw.get("doc_len"),
            field="doc_len",
            minimum=1,
            location=location,
        )
        documents.append(
            LocatedDocument(
                document=Document(
                    doc_id=raw["doc_id"],
                    doc_lang=raw["doc_lang"],
                    terms=terms,
                    doc_len=doc_len,
                ),
                source=location,
            )
        )
    if not documents:
        raise format_corrupt("document set is empty", source=str(path))
    return documents


def read_queries(path: str | Path) -> list[Query]:
    records = read_jsonl_with_locations(Path(path))
    queries: list[Query] = []
    seen: set[str] = set()
    for raw, location in records:
        for field in ("query_id", "query_lang"):
            if not isinstance(raw.get(field), str) or not raw[field]:
                raise format_corrupt(
                    "query is missing a required string field",
                    field=field,
                    location=location,
                )
        if raw["query_id"] in seen:
            raise format_corrupt(
                "duplicate query_id",
                query_id=raw["query_id"],
                location=location,
            )
        seen.add(raw["query_id"])
        queries.append(
            Query(
                query_id=raw["query_id"],
                query_lang=raw["query_lang"],
                terms=normalize_terms(raw.get("terms"), field="terms", location=location),
                top_k=require_int(
                    raw.get("top_k"),
                    field="top_k",
                    minimum=1,
                    location=location,
                ),
            )
        )
    if not queries:
        raise format_corrupt("query set is empty", source=str(path))
    return queries
