from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Token:
    start: int
    end: int
    text: str
    kind: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "text": self.text,
            "kind": self.kind,
        }


@dataclass(frozen=True)
class Sentence:
    shard_id: str
    doc_id: str
    sent_no: int
    text: str
    char_len: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "shard_id": self.shard_id,
            "doc_id": self.doc_id,
            "sent_no": self.sent_no,
            "text": self.text,
            "char_len": self.char_len,
        }


@dataclass
class Document:
    doc_id: str
    shard_ids: list[str]
    sentences: list[Sentence]

    @property
    def text_char_count(self) -> int:
        return sum(item.char_len for item in self.sentences)


@dataclass(frozen=True)
class Corpus:
    documents: tuple[Document, ...]
    shard_count: int
    sentence_count: int
    char_count: int
    input_checksum: str
    shard_checksums: dict[str, str]


@dataclass(frozen=True)
class EntitySpan:
    span_id: str
    doc_id: str
    sent_no: int
    start: int
    end: int
    text: str
    entity_id: str
    canonical_name: str
    type: str
    type_id: int
    dictionary_version: int

    @property
    def length(self) -> int:
        return self.end - self.start

    def to_dict(self) -> dict[str, Any]:
        return {
            "span_id": self.span_id,
            "doc_id": self.doc_id,
            "sent_no": self.sent_no,
            "start": self.start,
            "end": self.end,
            "length": self.length,
            "text": self.text,
            "entity_id": self.entity_id,
            "canonical_name": self.canonical_name,
            "type": self.type,
            "type_id": self.type_id,
            "dictionary_version": self.dictionary_version,
        }


@dataclass
class CandidateTriple:
    triple_id: str
    doc_id: str
    sent_no: int
    head_span_id: str
    tail_span_id: str
    head_start: int
    head_end: int
    tail_start: int
    tail_end: int
    head_text: str
    tail_text: str
    head_entity_id: str
    tail_entity_id: str
    relation_type: str
    relation_type_id: int
    confidence_bp: int
    confidence: float
    score_components: list[dict[str, Any]]
    pattern_line: int
    pattern_id: str
    trigger: str
    trigger_offset: int
    path: str = "exact"

    @property
    def key(self) -> tuple[str, int, int, int, str]:
        return (
            self.doc_id,
            self.head_start,
            self.tail_start,
            self.relation_type_id,
            self.relation_type,
        )

    @property
    def normalized_key(self) -> tuple[str, str, str, str]:
        return (
            self.doc_id,
            self.head_entity_id,
            self.tail_entity_id,
            self.relation_type,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "triple_id": self.triple_id,
            "doc_id": self.doc_id,
            "sent_no": self.sent_no,
            "head_span_id": self.head_span_id,
            "tail_span_id": self.tail_span_id,
            "head_start": self.head_start,
            "head_end": self.head_end,
            "tail_start": self.tail_start,
            "tail_end": self.tail_end,
            "head_text": self.head_text,
            "tail_text": self.tail_text,
            "head_entity_id": self.head_entity_id,
            "tail_entity_id": self.tail_entity_id,
            "relation_type": self.relation_type,
            "relation_type_id": self.relation_type_id,
            "confidence_bp": self.confidence_bp,
            "confidence": self.confidence,
            "score_components": self.score_components,
            "pattern_line": self.pattern_line,
            "pattern_id": self.pattern_id,
            "trigger": self.trigger,
            "trigger_offset": self.trigger_offset,
            "path": self.path,
        }


@dataclass
class DocumentEntities:
    doc_id: str
    spans: list[EntitySpan]
    sentence_tokens: dict[int, list[Token]] = field(default_factory=dict)

