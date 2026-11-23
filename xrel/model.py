"""Domain objects for multilingual documents, queries and bridge resources."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Tuple


@dataclass(frozen=True)
class DocTerm:
    term: str
    tf: int


@dataclass(frozen=True)
class Document:
    doc_id: str
    doc_lang: str
    terms: List[DocTerm]
    doc_len: int

    def to_dict(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "doc_lang": self.doc_lang,
            "terms": [{"term": item.term, "tf": item.tf} for item in self.terms],
            "doc_len": self.doc_len,
        }

    @staticmethod
    def from_dict(raw: dict) -> "Document":
        return Document(
            doc_id=raw["doc_id"],
            doc_lang=raw["doc_lang"],
            terms=[DocTerm(term=item["term"], tf=int(item["tf"])) for item in raw["terms"]],
            doc_len=int(raw["doc_len"]),
        )


@dataclass(frozen=True)
class QueryTerm:
    term: str


@dataclass(frozen=True)
class Query:
    query_id: str
    query_lang: str
    terms: List[str]
    top_k: int

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(raw: dict) -> "Query":
        return Query(
            query_id=raw["query_id"],
            query_lang=raw["query_lang"],
            terms=list(raw["terms"]),
            top_k=int(raw["top_k"]),
        )


@dataclass
class BridgeResource:
    """Versioned cross-lingual bridge resource.

    ``directions`` is the sorted list of allowed ``(source_lang, target_lang)``
    pairs.  ``entries`` maps ``(source_lang, source_term)`` to a list of
    ``(target_lang, target_term, confidence)`` triples.
    """

    version: int
    directions: List[Tuple[str, str]]
    entries: Dict[Tuple[str, str], List[Tuple[str, str, float]]] = field(default_factory=dict)
    checksum: str = ""

    def direction_pairs(self) -> List[Tuple[str, str]]:
        return sorted(self.directions)

    def has_direction(self, source_lang: str, target_lang: str) -> bool:
        return (source_lang, target_lang) in self.directions

    def lookup(self, source_lang: str, source_term: str):
        return self.entries.get((source_lang, source_term), [])

    # -- deterministic JSON encoding (tuple keys are flattened) -------------
    def to_dict(self) -> dict:
        serialized_entries = {}
        for (source_lang, source_term), mappings in sorted(self.entries.items()):
            key = f"{source_lang}\u0001{source_term}"
            serialized_entries[key] = [
                {"target_lang": target_lang, "target_term": target_term, "confidence": confidence}
                for target_lang, target_term, confidence in sorted(mappings)
            ]
        return {
            "version": self.version,
            "checksum": self.checksum,
            "directions": [list(pair) for pair in sorted(self.directions)],
            "entries": serialized_entries,
        }

    @staticmethod
    def from_dict(raw: dict) -> "BridgeResource":
        entries: Dict[Tuple[str, str], List[Tuple[str, str, float]]] = {}
        for compound_key, mappings in raw["entries"].items():
            source_lang, source_term = compound_key.split("\u0001", 1)
            entries[(source_lang, source_term)] = [
                (item["target_lang"], item["target_term"], float(item["confidence"]))
                for item in mappings
            ]
        directions = [tuple(pair) for pair in raw["directions"]]
        return BridgeResource(
            version=int(raw["version"]),
            directions=directions,
            entries=entries,
            checksum=raw.get("checksum", ""),
        )


def write_jsonl(path, records: List[dict]) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True, ensure_ascii=False))
            handle.write("\n")


def read_jsonl(path) -> List[dict]:
    records = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records
