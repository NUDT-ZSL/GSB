"""Deterministic inverted-index accumulation and partition statistics."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from .canonical import DEFAULT_DECIMALS, stable_float
from .models import Document


REDUCTION_ORDER = "batch_no,chunk_index,doc_id,term"


@dataclass
class NeumaierAccumulator:
    """Compensated summation whose state is serializable and reproducible."""

    raw: float = 0.0
    compensation: float = 0.0

    def add(self, value: float) -> None:
        value = float(value)
        total = self.raw + value
        if abs(self.raw) >= abs(value):
            self.compensation += (self.raw - total) + value
        else:
            self.compensation += (value - total) + self.raw
        self.raw = total

    @property
    def value(self) -> float:
        return self.raw + self.compensation

    def to_dict(self, decimals: int = DEFAULT_DECIMALS) -> dict[str, float]:
        return {
            "raw": stable_float(self.raw, decimals),
            "compensation": stable_float(self.compensation, decimals),
            "value": stable_float(self.value, decimals),
        }


class SimpleAccumulator:
    raw: float

    def __init__(self) -> None:
        self.raw = 0.0
        self.compensation = 0.0

    def add(self, value: float) -> None:
        self.raw += float(value)

    @property
    def value(self) -> float:
        return self.raw

    def to_dict(self, decimals: int = DEFAULT_DECIMALS) -> dict[str, float]:
        value = stable_float(self.value, decimals)
        return {"raw": value, "compensation": 0.0, "value": value}


def chunk_documents(
    documents: Sequence[Document],
    chunk_size: int,
) -> list[list[Document]]:
    if chunk_size < 1:
        raise ValueError("chunk_size must be >= 1")
    ordered = sorted(documents, key=lambda item: item.doc_id)
    return [ordered[index : index + chunk_size] for index in range(0, len(ordered), chunk_size)]


def _new_accumulator(compensated: bool) -> NeumaierAccumulator | SimpleAccumulator:
    return NeumaierAccumulator() if compensated else SimpleAccumulator()


def accumulate_chunk(
    documents: Sequence[Document],
    *,
    batch_no: int,
    chunk_index: int,
    compensated: bool = True,
    decimals: int = DEFAULT_DECIMALS,
) -> dict[str, Any]:
    """Accumulate one explicitly ordered chunk.

    The only valid reduction sequence is batch_no, chunk_index, doc_id, term.
    """

    ordered = sorted(documents, key=lambda item: item.doc_id)
    doc_len_sum = _new_accumulator(compensated)
    term_accumulators: dict[str, dict[str, Any]] = {}
    term_postings: dict[str, list[dict[str, Any]]] = {}

    for document in ordered:
        doc_len_sum.add(document.doc_len)
        for term, frequency in sorted(document.terms.items()):
            entry = term_accumulators.get(term)
            if entry is None:
                entry = {
                    "df": 0,
                    "tf": _new_accumulator(compensated),
                }
                term_accumulators[term] = entry
            entry["df"] += 1
            entry["tf"].add(frequency)
            term_postings.setdefault(term, []).append(
                {
                    "doc_id": document.doc_id,
                    "tf": stable_float(frequency, decimals),
                }
            )

    terms = {}
    for term in sorted(term_accumulators):
        entry = term_accumulators[term]
        terms[term] = {
            "df": entry["df"],
            "tf": entry["tf"].to_dict(decimals),
        }
    for term in term_postings:
        term_postings[term].sort(key=lambda item: item["doc_id"])
    return {
        "schema": "sparse-recall.inverted-chunk.v1",
        "batch_no": batch_no,
        "chunk_index": chunk_index,
        "reduction_order": REDUCTION_ORDER,
        "compensated": compensated,
        "document_count": len(ordered),
        "doc_len_sum": doc_len_sum.to_dict(decimals),
        "terms": terms,
        "term_postings": {
            term: term_postings[term] for term in sorted(term_postings)
        },
    }


def merge_chunks(
    chunks: Sequence[dict[str, Any]],
    *,
    batch_no: int,
    compensated: bool = True,
    decimals: int = DEFAULT_DECIMALS,
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    """Merge chunks in declared chunk-index order."""

    if not chunks:
        partition = {
            "schema": "sparse-recall.partition.v1",
            "batch_no": batch_no,
            "document_count": 0,
            "doc_len_sum": 0.0,
            "doc_len_compensation": 0.0,
            "avg_doc_len": 0.0,
            "terms": {},
            "reduction_order": REDUCTION_ORDER,
            "compensated": compensated,
        }
        return partition, {}

    ordered_chunks = sorted(chunks, key=lambda item: (item["batch_no"], item["chunk_index"]))
    doc_len_sum = _new_accumulator(compensated)
    term_accumulators: dict[str, dict[str, Any]] = {}
    postings: dict[str, list[dict[str, Any]]] = {}
    document_count = 0

    for chunk in ordered_chunks:
        if chunk["batch_no"] != batch_no:
            raise ValueError("cannot merge chunks from another batch")
        document_count += int(chunk["document_count"])
        doc_len_sum.add(float(chunk["doc_len_sum"]["value"]))
        for term in sorted(chunk["terms"]):
            source = chunk["terms"][term]
            target = term_accumulators.get(term)
            if target is None:
                target = {"df": 0, "tf": _new_accumulator(compensated)}
                term_accumulators[term] = target
            target["df"] += int(source["df"])
            target["tf"].add(float(source["tf"]["value"]))
    for chunk in ordered_chunks:
        for term, entries in sorted(chunk.get("term_postings", {}).items()):
            postings.setdefault(term, []).extend(entries)

    terms = {}
    for term in sorted(term_accumulators):
        entry = term_accumulators[term]
        terms[term] = {
            "df": entry["df"],
            "tf": stable_float(entry["tf"].value, decimals),
            "tf_compensation": stable_float(entry["tf"].compensation, decimals),
        }
    for term in postings:
        postings[term].sort(key=lambda item: item["doc_id"])

    value = stable_float(doc_len_sum.value, decimals)
    partition = {
        "schema": "sparse-recall.partition.v1",
        "batch_no": batch_no,
        "document_count": document_count,
        "doc_len_sum": value,
        "doc_len_compensation": stable_float(doc_len_sum.compensation, decimals),
        "avg_doc_len": stable_float(value / document_count, decimals)
        if document_count
        else 0.0,
        "terms": terms,
        "reduction_order": REDUCTION_ORDER,
        "compensated": compensated,
    }
    return partition, postings


def accumulate_batch(
    documents: Sequence[Document],
    *,
    batch_no: int,
    chunk_size: int,
    compensated: bool = True,
    decimals: int = DEFAULT_DECIMALS,
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    chunks = []
    for chunk_index, chunk_docs in enumerate(chunk_documents(documents, chunk_size)):
        chunk = accumulate_chunk(
            chunk_docs,
            batch_no=batch_no,
            chunk_index=chunk_index,
            compensated=compensated,
            decimals=decimals,
        )
        chunks.append(chunk)
    return merge_chunks(
        chunks,
        batch_no=batch_no,
        compensated=compensated,
        decimals=decimals,
    )


def merge_global_partitions(
    partitions: Iterable[dict[str, Any]],
    *,
    compensated: bool = True,
    decimals: int = DEFAULT_DECIMALS,
) -> dict[str, Any]:
    ordered = sorted(partitions, key=lambda item: int(item["batch_no"]))
    doc_len_sum = _new_accumulator(compensated)
    term_accumulators: dict[str, dict[str, Any]] = {}
    document_count = 0
    for partition in ordered:
        document_count += int(partition["document_count"])
        doc_len_sum.add(float(partition["doc_len_sum"]))
        for term in sorted(partition["terms"]):
            entry = partition["terms"][term]
            target = term_accumulators.get(term)
            if target is None:
                target = {"df": 0, "tf": _new_accumulator(compensated)}
                term_accumulators[term] = target
            target["df"] += int(entry["df"])
            target["tf"].add(float(entry["tf"]))

    terms = {}
    for term in sorted(term_accumulators):
        entry = term_accumulators[term]
        terms[term] = {
            "df": entry["df"],
            "tf": stable_float(entry["tf"].value, decimals),
            "tf_compensation": stable_float(entry["tf"].compensation, decimals),
        }
    total = stable_float(doc_len_sum.value, decimals)
    return {
        "schema": "sparse-recall.global-stats.v1",
        "document_count": document_count,
        "partition_count": len(ordered),
        "doc_len_sum": total,
        "doc_len_compensation": stable_float(doc_len_sum.compensation, decimals),
        "avg_doc_len": stable_float(total / document_count, decimals)
        if document_count
        else 0.0,
        "terms": terms,
        "reduction_order": REDUCTION_ORDER,
        "compensated": compensated,
    }


def recompute_stats_from_documents(
    documents: Sequence[Document],
    *,
    compensated: bool = True,
    decimals: int = DEFAULT_DECIMALS,
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    if not documents:
        raise ValueError("documents must not be empty")
    batch_no = min(document.batch_no for document in documents)
    chunk = accumulate_chunk(
        documents,
        batch_no=batch_no,
        chunk_index=0,
        compensated=compensated,
        decimals=decimals,
    )
    return merge_chunks(
        [chunk],
        batch_no=batch_no,
        compensated=compensated,
        decimals=decimals,
    )


def verify_partition_rollup(
    partitions: Sequence[dict[str, Any]],
    global_stats: dict[str, Any],
    *,
    decimals: int = DEFAULT_DECIMALS,
) -> list[str]:
    problems: list[str] = []
    partition_docs = sum(int(item["document_count"]) for item in partitions)
    if partition_docs != int(global_stats["document_count"]):
        problems.append("partition document counts do not roll up")
    partition_len = math.fsum(float(item["doc_len_sum"]) for item in partitions)
    if abs(partition_len - float(global_stats["doc_len_sum"])) > 10 ** (-(decimals - 2)):
        problems.append("partition document lengths do not roll up")

    recomputed_terms: dict[str, dict[str, Any]] = {}
    for partition in sorted(partitions, key=lambda item: int(item["batch_no"])):
        for term in sorted(partition["terms"]):
            entry = partition["terms"][term]
            target = recomputed_terms.setdefault(term, {"df": 0, "tf": []})
            target["df"] += int(entry["df"])
            target["tf"].append(float(entry["tf"]))
    for term in sorted(global_stats["terms"]):
        target = recomputed_terms.get(term)
        if target is None:
            problems.append(f"global term {term!r} missing from partitions")
            continue
        if target["df"] != int(global_stats["terms"][term]["df"]):
            problems.append(f"term {term!r} df does not roll up")
        expected = stable_float(math.fsum(target["tf"]), decimals)
        actual = stable_float(float(global_stats["terms"][term]["tf"]), decimals)
        if expected != actual:
            problems.append(f"term {term!r} tf does not roll up")
    return problems
