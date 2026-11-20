from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .dictionary import EntityDictionary
from .models import Document, DocumentEntities, EntitySpan
from .tokenizer import tokenize
from .util import stable_id


@dataclass(frozen=True)
class MatchCandidate:
    start: int
    end: int
    alias: str
    entity_id: str
    canonical_name: str
    entity_type: str
    type_id: int

    @property
    def length(self) -> int:
        return self.end - self.start


def _candidate_sort_key(candidate: MatchCandidate) -> tuple[Any, ...]:
    return (
        -candidate.length,
        candidate.start,
        candidate.type_id,
        candidate.alias,
        candidate.entity_id,
    )


def match_sentence(
    *,
    doc_id: str,
    sent_no: int,
    text: str,
    entity_dictionary: EntityDictionary,
) -> list[EntitySpan]:
    candidates_by_start: dict[int, list[MatchCandidate]] = {}
    aliases = sorted(
        entity_dictionary.alias_to_entity,
        key=lambda alias: (-len(alias), alias),
    )
    for start in range(len(text)):
        for alias in aliases:
            if not text.startswith(alias, start):
                continue
            entry = entity_dictionary.entry_for_alias(alias)
            candidates_by_start.setdefault(start, []).append(
                MatchCandidate(
                    start=start,
                    end=start + len(alias),
                    alias=alias,
                    entity_id=entry.entity_id,
                    canonical_name=entry.canonical_name,
                    entity_type=entry.type,
                    type_id=entry.type_id,
                )
            )

    selected: list[MatchCandidate] = []
    index = 0
    while index < len(text):
        candidates = candidates_by_start.get(index, [])
        if not candidates:
            index += 1
            continue
        candidates.sort(key=_candidate_sort_key)
        candidate = candidates[0]
        selected.append(candidate)
        index = candidate.end

    selected.sort(key=lambda item: (item.start, item.end, item.type_id, item.entity_id))
    spans: list[EntitySpan] = []
    for candidate in selected:
        identity = {
            "doc_id": doc_id,
            "sent_no": sent_no,
            "start": candidate.start,
            "end": candidate.end,
            "entity_id": candidate.entity_id,
            "dictionary_version": entity_dictionary.version,
        }
        spans.append(
            EntitySpan(
                span_id=stable_id("sp", identity),
                doc_id=doc_id,
                sent_no=sent_no,
                start=candidate.start,
                end=candidate.end,
                text=text[candidate.start : candidate.end],
                entity_id=candidate.entity_id,
                canonical_name=candidate.canonical_name,
                type=candidate.entity_type,
                type_id=candidate.type_id,
                dictionary_version=entity_dictionary.version,
            )
        )
    return spans


def recognize_document(
    document: Document,
    entity_dictionary: EntityDictionary,
) -> DocumentEntities:
    spans: list[EntitySpan] = []
    sentence_tokens = {}
    for sentence in document.sentences:
        spans.extend(
            match_sentence(
                doc_id=document.doc_id,
                sent_no=sentence.sent_no,
                text=sentence.text,
                entity_dictionary=entity_dictionary,
            )
        )
        sentence_tokens[sentence.sent_no] = tokenize(sentence.text)
    spans.sort(key=lambda span: (span.sent_no, span.start, span.end, span.type_id))
    return DocumentEntities(
        doc_id=document.doc_id,
        spans=spans,
        sentence_tokens=sentence_tokens,
    )


def recognize_corpus(
    document: Document,
    entity_dictionary: EntityDictionary,
) -> DocumentEntities:
    return recognize_document(document, entity_dictionary)


def verify_span_invariants(
    *,
    text: str,
    spans: list[EntitySpan],
) -> list[str]:
    problems: list[str] = []
    previous_end = -1
    for span in sorted(spans, key=lambda item: (item.start, item.end)):
        if span.start < 0 or span.end > len(text) or span.start >= span.end:
            problems.append("offset_out_of_range")
        elif text[span.start : span.end] != span.text:
            problems.append("surface_mismatch")
        if span.start < previous_end:
            problems.append("overlap")
        previous_end = max(previous_end, span.end)
    return problems

