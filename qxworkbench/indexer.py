"""Deterministic per-language inverted partition construction."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from .data import LocatedDocument
from .util import checksum_object, round_score, stable_json_bytes


def document_set_checksum(documents: Iterable[LocatedDocument]) -> str:
    payload = [
        located.document.to_dict()
        for located in sorted(documents, key=lambda item: item.document.doc_id)
    ]
    return checksum_object(payload)


def partition_languages(documents: Iterable[LocatedDocument]) -> tuple[str, ...]:
    return tuple(sorted({located.document.doc_lang for located in documents}))


def build_partition(
    partition_lang: str,
    documents: Iterable[LocatedDocument],
    *,
    index_version: int,
    bridge_version: int,
    bridge_checksum: str,
    rule_version: int,
    rule_checksum: str,
) -> dict[str, Any]:
    """Build one partition with postings sorted by doc_id and then term."""

    members = sorted(
        (
            located
            for located in documents
            if located.document.doc_lang == partition_lang
        ),
        key=lambda item: item.document.doc_id,
    )
    docs: list[dict[str, Any]] = []
    postings: list[dict[str, Any]] = []
    term_index: dict[str, list[int]] = {}

    for located in members:
        document = located.document
        docs.append(
            {
                "doc_id": document.doc_id,
                "doc_lang": document.doc_lang,
                "doc_len": document.doc_len,
                "source": dict(located.source),
            }
        )
        for term, frequency in sorted(document.terms.items()):
            posting_index = len(postings)
            postings.append(
                {
                    "term": term,
                    "doc_id": document.doc_id,
                    "tf": frequency,
                    "doc_len": document.doc_len,
                    "source": dict(located.source),
                }
            )
            term_index.setdefault(term, []).append(posting_index)

    avg_doc_len = (
        round_score(sum(document["doc_len"] for document in docs) / len(docs))
        if docs
        else 0.0
    )
    return {
        "partition_lang": partition_lang,
        "index_version": index_version,
        "bridge_version": bridge_version,
        "bridge_checksum": bridge_checksum,
        "rule_version": rule_version,
        "rule_checksum": rule_checksum,
        "docs": docs,
        "postings": postings,
        "term_index": dict(sorted(term_index.items())),
        "stats": {
            "doc_count": len(docs),
            "posting_count": len(postings),
            "avg_doc_len": avg_doc_len,
        },
    }


def retag_partition(
    partition: Mapping[str, Any],
    *,
    index_version: int,
    bridge_version: int,
    bridge_checksum: str,
    rule_version: int,
    rule_checksum: str,
) -> dict[str, Any]:
    """Retag reused postings for a new complete snapshot without rebuilding."""

    result = dict(partition)
    result["index_version"] = index_version
    result["bridge_version"] = bridge_version
    result["bridge_checksum"] = bridge_checksum
    result["rule_version"] = rule_version
    result["rule_checksum"] = rule_checksum
    result["postings_checksum"] = checksum_object(
        {
            "partition_lang": result["partition_lang"],
            "docs": result["docs"],
            "postings": result["postings"],
            "term_index": result["term_index"],
        }
    )
    return result


def build_partition_worker(payload: Mapping[str, Any]) -> tuple[str, bytes, int]:
    from .data import Document, LocatedDocument

    documents = [
        LocatedDocument(
            document=Document(
                doc_id=item["doc_id"],
                doc_lang=item["doc_lang"],
                terms=dict(item["terms"]),
                doc_len=item["doc_len"],
            ),
            source=item["source"],
        )
        for item in payload["documents"]
    ]
    partition = build_partition(
        payload["partition_lang"],
        documents,
        index_version=payload["index_version"],
        bridge_version=payload["bridge_version"],
        bridge_checksum=payload["bridge_checksum"],
        rule_version=payload["rule_version"],
        rule_checksum=payload["rule_checksum"],
    )
    partition["postings_checksum"] = checksum_object(
        {
            "partition_lang": partition["partition_lang"],
            "docs": partition["docs"],
            "postings": partition["postings"],
            "term_index": partition["term_index"],
        }
    )
    data = stable_json_bytes(partition)
    return payload["partition_lang"], data, len(partition["postings"])


def partition_payload(located: LocatedDocument) -> dict[str, Any]:
    document = located.document
    return {
        "doc_id": document.doc_id,
        "doc_lang": document.doc_lang,
        "terms": dict(document.terms),
        "doc_len": document.doc_len,
        "source": dict(located.source),
    }
