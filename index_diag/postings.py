"""Incremental, deterministic inverted posting lists and invariants."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .analysis import AnalyzedDoc, DocId, doc_sort_key
from .errors import DiagnosticError, E_POSTING_INVALID
from .resources import ResourceGuard
from .util import canonical_json, sha256_json


@dataclass(frozen=True)
class Posting:
    doc_id: DocId
    batch_no: int
    positions: tuple[int, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_no": self.batch_no,
            "doc_id": self.doc_id,
            "positions": list(self.positions),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Posting":
        return cls(
            doc_id=value["doc_id"],
            batch_no=int(value["batch_no"]),
            positions=tuple(int(position) for position in value["positions"]),
        )


PostingMap = dict[str, list[Posting]]


def _posting_invalid(message: str, **details: Any) -> DiagnosticError:
    return DiagnosticError(E_POSTING_INVALID, message, details)


def _validate_posting(posting: Posting, term: str) -> None:
    if isinstance(posting.batch_no, bool) or posting.batch_no < 0:
        raise _posting_invalid(
            "posting has invalid batch number",
            term=term,
            doc_id=posting.doc_id,
        )
    if not posting.positions:
        raise _posting_invalid(
            "posting must contain at least one position",
            term=term,
            doc_id=posting.doc_id,
        )
    previous = -1
    for position in posting.positions:
        if isinstance(position, bool) or not isinstance(position, int) or position < 0:
            raise _posting_invalid(
                "posting position must be a non-negative integer",
                term=term,
                doc_id=posting.doc_id,
            )
        if position <= previous:
            raise _posting_invalid(
                "posting positions must be strictly increasing",
                term=term,
                doc_id=posting.doc_id,
            )
        previous = position


def _insert_sorted(
    sequence: list[Posting],
    posting: Posting,
    *,
    term: str,
    guard: ResourceGuard,
) -> None:
    new_key = doc_sort_key(posting.doc_id)
    low = 0
    high = len(sequence)
    while low < high:
        middle = (low + high) // 2
        guard.add_comparisons()
        middle_key = doc_sort_key(sequence[middle].doc_id)
        if middle_key < new_key:
            low = middle + 1
        else:
            high = middle
    if low < len(sequence):
        guard.add_comparisons()
        if doc_sort_key(sequence[low].doc_id) == new_key:
            raise _posting_invalid(
                "doc_id appears more than once in a term posting list",
                term=term,
                doc_id=posting.doc_id,
            )
    sequence.insert(low, posting)


def append_documents(
    postings: PostingMap,
    documents: Iterable[AnalyzedDoc],
    *,
    guard: ResourceGuard | None = None,
) -> dict[str, Any]:
    """Append only terms hit by new documents and preserve posting order."""

    if guard is None:
        from .resources import ResourceLimits

        effective_guard = ResourceGuard(ResourceLimits())
    else:
        effective_guard = guard
    terms_touched: set[str] = set()
    postings_added = 0
    for document in documents:
        position_map = document.position_map
        for term in sorted(position_map):
            positions = tuple(position_map[term])
            posting = Posting(
                doc_id=document.doc_id,
                batch_no=document.batch_no,
                positions=positions,
            )
            _validate_posting(posting, term)
            sequence = postings.setdefault(term, [])
            _insert_sorted(sequence, posting, term=term, guard=effective_guard)
            terms_touched.add(term)
            postings_added += 1
    return {
        "comparisons": effective_guard.comparisons,
        "postings_added": postings_added,
        "terms_touched": len(terms_touched),
        "touched_terms": sorted(terms_touched),
    }


def postings_from_documents(
    documents: Iterable[AnalyzedDoc],
    *,
    existing: PostingMap | None = None,
    guard: ResourceGuard | None = None,
) -> tuple[PostingMap, dict[str, Any]]:
    effective_guard = guard
    if effective_guard is None:
        from .resources import ResourceLimits

        effective_guard = ResourceGuard(ResourceLimits())
    result: PostingMap = {
        term: list(values) for term, values in (existing or {}).items()
    }
    metrics = append_documents(result, documents, guard=effective_guard)
    check_postings_structure(result)
    effective_guard.check_postings_size(estimate_postings_bytes(result))
    metrics["resident_bytes"] = estimate_postings_bytes(result)
    metrics["comparisons"] = effective_guard.comparisons
    return result, metrics


def rebuild_terms(
    postings: PostingMap,
    documents: Iterable[AnalyzedDoc],
    terms: Iterable[str],
    *,
    guard: ResourceGuard | None = None,
) -> dict[str, Any]:
    """Replace only named term lists; used by explicit batch replacement."""

    if guard is None:
        from .resources import ResourceLimits

        effective_guard = ResourceGuard(ResourceLimits())
    else:
        effective_guard = guard
    docs = sort_documents(documents)
    rebuilt_terms: list[str] = []
    postings_rebuilt = 0
    for term in sorted(set(terms)):
        sequence: list[Posting] = []
        for document in docs:
            positions = document.position_map.get(term)
            if positions is None:
                continue
            posting = Posting(
                doc_id=document.doc_id,
                batch_no=document.batch_no,
                positions=positions,
            )
            _validate_posting(posting, term)
            _insert_sorted(sequence, posting, term=term, guard=effective_guard)
        if sequence:
            postings[term] = sequence
        else:
            postings.pop(term, None)
        rebuilt_terms.append(term)
        postings_rebuilt += len(sequence)
    check_postings_structure(postings)
    effective_guard.check_postings_size(estimate_postings_bytes(postings))
    return {
        "comparisons": effective_guard.comparisons,
        "postings_rebuilt": postings_rebuilt,
        "rebuilt_terms": rebuilt_terms,
        "resident_bytes": estimate_postings_bytes(postings),
    }


def sort_documents(documents: Iterable[AnalyzedDoc]) -> list[AnalyzedDoc]:
    return sorted(
        documents,
        key=lambda document: (document.batch_no, doc_sort_key(document.doc_id)),
    )


def check_postings_structure(postings: Mapping[str, list[Posting]]) -> None:
    for term, sequence in postings.items():
        previous_key: tuple[int, Any] | None = None
        for posting in sequence:
            _validate_posting(posting, term)
            key = doc_sort_key(posting.doc_id)
            if previous_key is not None and key <= previous_key:
                raise _posting_invalid(
                    "posting list is not strictly sorted by doc_id",
                    term=term,
                    previous_doc_id=None,
                    doc_id=posting.doc_id,
                )
            previous_key = key


def compute_term_statistics(
    postings: Mapping[str, list[Posting]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for term in sorted(postings):
        sequence = postings[term]
        result[term] = {
            "doc_frequency": len(sequence),
            "position_offset_sum": sum(
                sum(posting.positions) for posting in sequence
            ),
            "position_checksum": sha256_json(
                [
                    [posting.doc_id, list(posting.positions)]
                    for posting in sequence
                ]
            ),
            "term_frequency": sum(len(posting.positions) for posting in sequence),
        }
    return result


def postings_to_sequence(sequence: Iterable[Posting]) -> list[int]:
    """Flatten a posting list to the integer stream used by compression.

    Each posting contributes:
      [doc_type (0=int, 1=string), doc_key, position_count, *positions]

    String document keys use the first 63 bits of SHA-256 to remain stable while
    keeping the sequence integer-only.
    """

    values: list[int] = []
    for posting in sequence:
        if isinstance(posting.doc_id, int) and not isinstance(posting.doc_id, bool):
            doc_type, doc_key = 0, posting.doc_id
        elif isinstance(posting.doc_id, str):
            doc_type = 1
            digest = hashlib.sha256(posting.doc_id.encode("utf-8")).digest()
            doc_key = int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)
        else:
            raise _posting_invalid(
                "doc_id cannot be encoded into integer stream",
                doc_id=posting.doc_id,
            )
        values.extend((doc_type, doc_key, len(posting.positions)))
        values.extend(posting.positions)
    return values


def term_postings_hash(sequence: Iterable[Posting]) -> str:
    return sha256_json([posting.to_dict() for posting in sequence])


def postings_digest(postings: Mapping[str, list[Posting]]) -> str:
    return sha256_json(serialize_postings(postings))


def estimate_postings_bytes(postings: Mapping[str, list[Posting]]) -> int:
    return len(canonical_json(serialize_postings(postings)).encode("utf-8"))


def serialize_postings(postings: Mapping[str, list[Posting]]) -> dict[str, Any]:
    return {
        term: [posting.to_dict() for posting in postings[term]]
        for term in sorted(postings)
    }


def deserialize_postings(value: Mapping[str, Any]) -> PostingMap:
    try:
        result = {
            str(term): [Posting.from_dict(posting) for posting in sequence]
            for term, sequence in value.items()
        }
    except (TypeError, ValueError, KeyError) as exc:
        raise _posting_invalid("stored postings are malformed", error=str(exc)) from exc
    check_postings_structure(result)
    return result


def validate_postings_against_documents(
    postings: Mapping[str, list[Posting]],
    documents: Iterable[AnalyzedDoc],
    *,
    expected_statistics: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Independently reconstruct the index and compare every posting."""

    expected: dict[str, dict[Any, tuple[int, ...]]] = {}
    docs = list(documents)
    document_ids: set[DocId] = set()
    for document in docs:
        if document.doc_id in document_ids:
            raise _posting_invalid(
                "duplicate document in consistency source",
                doc_id=document.doc_id,
            )
        document_ids.add(document.doc_id)
        for term, positions in document.positions:
            expected.setdefault(term, {})[document.doc_id] = positions

    check_postings_structure(postings)
    if set(postings) != set(expected):
        raise _posting_invalid(
            "term set differs from independently reconstructed source",
            missing_terms=sorted(set(expected) - set(postings)),
            extra_terms=sorted(set(postings) - set(expected)),
        )

    for term in sorted(expected):
        sequence = postings[term]
        actual_docs = [posting.doc_id for posting in sequence]
        if len(actual_docs) != len(set(actual_docs)):
            raise _posting_invalid(
                "term posting list contains a duplicate document",
                term=term,
            )
        expected_items = sorted(
            expected[term].items(), key=lambda item: doc_sort_key(item[0])
        )
        if actual_docs != [doc_id for doc_id, _ in expected_items]:
            raise _posting_invalid(
                "term posting documents or ordering differ from source",
                term=term,
                actual=actual_docs,
                expected=[doc_id for doc_id, _ in expected_items],
            )
        for posting, (_, positions) in zip(sequence, expected_items):
            if posting.positions != positions:
                raise _posting_invalid(
                    "posting positions differ from document term stream",
                    term=term,
                    doc_id=posting.doc_id,
                )

    statistics = compute_term_statistics(postings)
    if expected_statistics is not None:
        for term, actual in statistics.items():
            expected_term = expected_statistics.get(term)
            if expected_term is None or dict(expected_term) != actual:
                raise _posting_invalid(
                    "term statistics cannot be independently reproduced",
                    term=term,
                    actual=actual,
                    expected=expected_term,
                )
        if set(expected_statistics) != set(statistics):
            raise _posting_invalid(
                "statistics term set differs from postings",
                missing_terms=sorted(set(statistics) - set(expected_statistics)),
                extra_terms=sorted(set(expected_statistics) - set(statistics)),
            )
    return {
        "documents": len(docs),
        "postings_digest": postings_digest(postings),
        "statistics": statistics,
        "terms": len(postings),
        "total_postings": sum(len(sequence) for sequence in postings.values()),
    }
