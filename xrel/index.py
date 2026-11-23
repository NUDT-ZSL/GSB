"""Per-language inverted index and BM25 collection statistics."""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from .model import Document


@dataclass
class Posting:
    doc_id: str
    tf: int

    def to_dict(self) -> dict:
        return {"doc_id": self.doc_id, "tf": self.tf}


@dataclass
class PartitionStats:
    """Versioned statistics for one language partition."""

    lang: str
    version: int
    doc_count: int
    total_len: int
    avg_len: float
    df: Dict[str, int] = field(default_factory=dict)
    postings: Dict[str, List[Posting]] = field(default_factory=dict)
    checksum: str = ""

    def to_dict(self) -> dict:
        return {
            "lang": self.lang,
            "version": self.version,
            "doc_count": self.doc_count,
            "total_len": self.total_len,
            "avg_len": self.avg_len,
            "df": dict(sorted(self.df.items())),
            "postings": {
                term: [posting.to_dict() for posting in sorted(items, key=lambda p: p.doc_id)]
                for term, items in sorted(self.postings.items())
            },
            "checksum": self.checksum,
        }

    @staticmethod
    def from_dict(raw: dict) -> "PartitionStats":
        stats = PartitionStats(
            lang=raw["lang"],
            version=int(raw["version"]),
            doc_count=int(raw["doc_count"]),
            total_len=int(raw["total_len"]),
            avg_len=float(raw["avg_len"]),
            df=dict(raw["df"]),
            postings={
                term: [Posting(item["doc_id"], int(item["tf"])) for item in items]
                for term, items in raw["postings"].items()
            },
            checksum=raw.get("checksum", ""),
        )
        return stats


@dataclass
class InvertedIndex:
    partitions: Dict[str, PartitionStats]
    version: int = 1
    checksum: str = ""

    def languages(self) -> List[str]:
        return sorted(self.partitions.keys())


def build_partition(lang: str, documents: List[Document], version: int) -> PartitionStats:
    lang_docs = [doc for doc in documents if doc.doc_lang == lang]
    postings: Dict[str, List[Posting]] = {}
    total_len = 0
    for doc in sorted(lang_docs, key=lambda item: item.doc_id):
        total_len += doc.doc_len
        for term_entry in sorted(doc.terms, key=lambda item: item.term):
            postings.setdefault(term_entry.term, []).append(
                Posting(doc_id=doc.doc_id, tf=term_entry.tf)
            )
    for term in postings:
        postings[term].sort(key=lambda posting: posting.doc_id)
    df = {term: len(items) for term, items in postings.items()}
    doc_count = len(lang_docs)
    avg_len = (total_len / doc_count) if doc_count else 0.0
    return PartitionStats(
        lang=lang,
        version=version,
        doc_count=doc_count,
        total_len=total_len,
        avg_len=avg_len,
        df=df,
        postings=postings,
    )


def build_index(documents: List[Document], version: int = 1) -> InvertedIndex:
    languages = sorted({doc.doc_lang for doc in documents})
    partitions = {
        lang: build_partition(lang, documents, version) for lang in languages
    }
    return InvertedIndex(partitions=partitions, version=version)


def term_partition_hit(
    stats: PartitionStats, term: str
) -> Tuple[List[Posting], int]:
    """Return postings and document frequency for a term (empty if absent)."""

    postings = stats.postings.get(term, [])
    return postings, stats.df.get(term, 0)
