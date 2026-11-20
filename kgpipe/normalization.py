from __future__ import annotations

from collections import Counter
from typing import Any

from .dictionary import EntityDictionary
from .errors import KGError
from .models import CandidateTriple, Corpus, Document, DocumentEntities, EntitySpan
from .util import stable_id


def _inconsistency(
    message: str,
    *,
    category: str,
    doc_id: str,
    **details: Any,
) -> None:
    raise KGError(
        "E_INCONSISTENT",
        message,
        doc_id=doc_id,
        details={"category": category, "doc_id": doc_id, **details},
    )


def _triple_sort_key(triple: dict[str, Any]) -> tuple[Any, ...]:
    return (
        -triple["confidence_bp"],
        triple["head_start"],
        triple["tail_start"],
        triple["relation_type_id"],
        triple["sent_no"],
        triple["head_entity_id"],
        triple["tail_entity_id"],
        triple["pattern_line"],
    )


def normalize_document(
    *,
    document: Document,
    entities: DocumentEntities,
    candidates: list[CandidateTriple],
    entity_dictionary: EntityDictionary,
) -> dict[str, Any]:
    spans_by_id: dict[str, EntitySpan] = {
        span.span_id: span for span in entities.spans
    }
    if len(spans_by_id) != len(entities.spans):
        _inconsistency(
            "文档内实体 span_id 重复",
            category="SPAN_ID_DUPLICATE",
            doc_id=document.doc_id,
        )

    canonical: dict[str, dict[str, Any]] = {}
    for span in entities.spans:
        canonical_id = entity_dictionary.resolve_entity_literal(span.text)
        entry = entity_dictionary.by_id[canonical_id]
        if span.entity_id != canonical_id:
            _inconsistency(
                "实体跨度规范 ID 与词典解析结果不一致",
                category="NORMALIZATION_MISMATCH",
                doc_id=document.doc_id,
                span_id=span.span_id,
                span_entity_id=span.entity_id,
                resolved_entity_id=canonical_id,
            )
        item = canonical.setdefault(
            canonical_id,
            {
                "entity_id": canonical_id,
                "canonical_name": entry.canonical_name,
                "type": entry.type,
                "type_id": entry.type_id,
                "aliases": set(),
                "span_ids": [],
                "spans": [],
            },
        )
        item["aliases"].add(span.text)
        item["span_ids"].append(span.span_id)
        item["spans"].append(
            {
                "span_id": span.span_id,
                "sent_no": span.sent_no,
                "start": span.start,
                "end": span.end,
                "text": span.text,
            }
        )

    alias_to_canonical: dict[str, str] = {}
    canonical_rows: list[dict[str, Any]] = []
    for entity_id in sorted(canonical):
        item = canonical[entity_id]
        aliases = sorted(item["aliases"])
        spans = sorted(
            item["spans"],
            key=lambda span: (span["sent_no"], span["start"], span["end"]),
        )
        surface_set = sorted({span["text"] for span in spans})
        if aliases != surface_set:
            _inconsistency(
                "规范实体的别名集合无法与跨度字面量互相核对",
                category="ALIAS_SPAN_MISMATCH",
                doc_id=document.doc_id,
                entity_id=entity_id,
                aliases=aliases,
                span_surfaces=surface_set,
            )
        for alias in aliases:
            previous = alias_to_canonical.get(alias)
            if previous is not None and previous != entity_id:
                _inconsistency(
                    "同一别名映射到多个规范实体",
                    category="ALIAS_CONFLICT",
                    doc_id=document.doc_id,
                    alias=alias,
                    candidate_entity_ids=sorted({previous, entity_id}),
                )
            alias_to_canonical[alias] = entity_id
        canonical_rows.append(
            {
                "entity_id": entity_id,
                "canonical_name": item["canonical_name"],
                "type": item["type"],
                "type_id": item["type_id"],
                "aliases": aliases,
                "span_ids": sorted(item["span_ids"]),
                "spans": spans,
                "mention_count": len(spans),
            }
        )
    canonical_rows.sort(key=lambda item: (item["type_id"], item["entity_id"]))

    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for candidate in candidates:
        head = spans_by_id.get(candidate.head_span_id)
        tail = spans_by_id.get(candidate.tail_span_id)
        if head is None or tail is None:
            _inconsistency(
                "候选三元组端点不在同一文档实体集合中",
                category="TRIPLE_ENDPOINT",
                doc_id=document.doc_id,
                triple_id=candidate.triple_id,
                head_span_id=candidate.head_span_id,
                tail_span_id=candidate.tail_span_id,
            )
        head_entity_id = entity_dictionary.resolve_entity_literal(head.text)
        tail_entity_id = entity_dictionary.resolve_entity_literal(tail.text)
        key = (head_entity_id, tail_entity_id, candidate.relation_type)
        grouped.setdefault(key, []).append(
            {
                "candidate": candidate,
                "head": head,
                "tail": tail,
            }
        )

    triples: list[dict[str, Any]] = []
    duplicate_groups: list[dict[str, Any]] = []
    for key in sorted(grouped):
        items = grouped[key]
        rows: list[dict[str, Any]] = []
        for item in items:
            candidate: CandidateTriple = item["candidate"]
            head: EntitySpan = item["head"]
            tail: EntitySpan = item["tail"]
            rows.append(
                {
                    "triple_id": stable_id(
                        "kn",
                        {
                            "doc_id": document.doc_id,
                            "head_entity_id": key[0],
                            "tail_entity_id": key[1],
                            "relation_type": key[2],
                        },
                    ),
                    "doc_id": document.doc_id,
                    "sent_no": candidate.sent_no,
                    "head_entity_id": key[0],
                    "tail_entity_id": key[1],
                    "relation_type": candidate.relation_type,
                    "relation_type_id": candidate.relation_type_id,
                    "confidence_bp": candidate.confidence_bp,
                    "confidence": candidate.confidence,
                    "score_components": candidate.score_components,
                    "pattern_line": candidate.pattern_line,
                    "pattern_id": candidate.pattern_id,
                    "trigger": candidate.trigger,
                    "trigger_offset": candidate.trigger_offset,
                    "head_span_id": candidate.head_span_id,
                    "tail_span_id": candidate.tail_span_id,
                    "head_start": candidate.head_start,
                    "head_end": candidate.head_end,
                    "tail_start": candidate.tail_start,
                    "tail_end": candidate.tail_end,
                    "head_text": candidate.head_text,
                    "tail_text": candidate.tail_text,
                    "path": candidate.path,
                    "source_candidate_ids": [candidate.triple_id],
                    "support_count": 1,
                }
            )
        rows.sort(key=_triple_sort_key)
        winner = dict(rows[0])
        winner["source_candidate_ids"] = sorted(
            {source for row in rows for source in row["source_candidate_ids"]}
        )
        winner["support_count"] = len(rows)
        winner["head_mention_text"] = head_literal = winner.pop("head_text")
        winner["tail_mention_text"] = tail_literal = winner.pop("tail_text")
        winner["head_mention"] = {
            "span_id": winner["head_span_id"],
            "sent_no": winner["sent_no"],
            "start": winner["head_start"],
            "end": winner["head_end"],
            "text": head_literal,
        }
        winner["tail_mention"] = {
            "span_id": winner["tail_span_id"],
            "sent_no": winner["sent_no"],
            "start": winner["tail_start"],
            "end": winner["tail_end"],
            "text": tail_literal,
        }
        triples.append(winner)
        if len(rows) > 1:
            duplicate_groups.append(
                {
                    "canonical_key": {
                        "head_entity_id": key[0],
                        "tail_entity_id": key[1],
                        "relation_type": key[2],
                    },
                    "candidate_count": len(rows),
                    "chosen_candidate_id": rows[0]["source_candidate_ids"][0],
                    "suppressed_candidate_ids": sorted(
                        {
                            source
                            for row in rows[1:]
                            for source in row["source_candidate_ids"]
                        }
                    ),
                }
            )
    triples.sort(key=_triple_sort_key)
    keys = [
        (triple["head_entity_id"], triple["tail_entity_id"], triple["relation_type"])
        for triple in triples
    ]
    if len(keys) != len(set(keys)):
        _inconsistency(
            "归一化去重后仍存在重复三元组",
            category="TRIPLE_DUPLICATE",
            doc_id=document.doc_id,
        )

    return {
        "doc_id": document.doc_id,
        "shard_ids": list(document.shard_ids),
        "dictionary_version": entity_dictionary.version,
        "canonical_entities": canonical_rows,
        "alias_to_canonical": dict(sorted(alias_to_canonical.items())),
        "triples": triples,
        "duplicate_groups": duplicate_groups,
        "counts": {
            "entity_span_count": len(entities.spans),
            "canonical_entity_count": len(canonical_rows),
            "candidate_triple_count": len(candidates),
            "triple_count": len(triples),
            "suppressed_duplicate_count": sum(
                group["candidate_count"] - 1 for group in duplicate_groups
            ),
        },
    }


def recompute_graph_stats(
    corpus: Corpus,
    normalized_docs: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    relation_counts: Counter[str] = Counter()
    relation_type_id_counts: Counter[str] = Counter()
    entity_type_span_counts: Counter[str] = Counter()
    canonical_entity_ids: set[str] = set()
    canonical_entity_types: dict[str, str] = {}
    shard_triple_counts: Counter[str] = Counter()
    shard_entity_span_counts: Counter[str] = Counter()
    per_document: list[dict[str, Any]] = []

    for document in corpus.documents:
        detail = normalized_docs.get(document.doc_id)
        if detail is None:
            raise KGError(
                "E_INCONSISTENT",
                "逐文档明细缺少文档",
                doc_id=document.doc_id,
                details={"category": "GRAPH_STATS_INPUT", "doc_id": document.doc_id},
            )
        doc_relation_counts: Counter[str] = Counter()
        doc_span_type_counts: Counter[str] = Counter()
        doc_canonical_ids = set()
        for entity in detail["canonical_entities"]:
            doc_canonical_ids.add(entity["entity_id"])
            canonical_entity_ids.add(entity["entity_id"])
            canonical_entity_types[entity["entity_id"]] = entity["type"]
            doc_span_type_counts[entity["type"]] += entity["mention_count"]
            for shard_id in document.shard_ids:
                shard_entity_span_counts[shard_id] += entity["mention_count"]
        for triple in detail["triples"]:
            doc_relation_counts[triple["relation_type"]] += 1
            relation_type_id_counts[str(triple["relation_type_id"])] += 1
            for shard_id in document.shard_ids:
                shard_triple_counts[shard_id] += 1
        relation_counts.update(doc_relation_counts)
        entity_type_span_counts.update(doc_span_type_counts)
        per_document.append(
            {
                "doc_id": document.doc_id,
                "shard_ids": list(document.shard_ids),
                "sentence_count": len(document.sentences),
                "char_count": document.text_char_count,
                "entity_span_count": detail["counts"]["entity_span_count"],
                "canonical_entity_count": len(doc_canonical_ids),
                "triple_count": len(detail["triples"]),
                "relation_counts": dict(sorted(doc_relation_counts.items())),
                "entity_type_span_counts": dict(
                    sorted(doc_span_type_counts.items())
                ),
            }
        )

    per_document.sort(key=lambda item: item["doc_id"])
    canonical_entities_by_type = Counter(canonical_entity_types.values())
    return {
        "shard_count": corpus.shard_count,
        "document_count": len(corpus.documents),
        "sentence_count": corpus.sentence_count,
        "char_count": corpus.char_count,
        "entity_span_count": sum(
            detail["counts"]["entity_span_count"] for detail in normalized_docs.values()
        ),
        "unique_canonical_entity_count": len(canonical_entity_ids),
        "unique_canonical_entity_type_counts": dict(
            sorted(canonical_entities_by_type.items())
        ),
        "triple_count": sum(
            len(detail["triples"]) for detail in normalized_docs.values()
        ),
        "relation_counts": dict(sorted(relation_counts.items())),
        "relation_type_id_counts": dict(
            sorted(
                relation_type_id_counts.items(),
                key=lambda item: int(item[0]),
            )
        ),
        "entity_type_span_counts": dict(sorted(entity_type_span_counts.items())),
        "shard_triple_counts": dict(sorted(shard_triple_counts.items())),
        "shard_entity_span_counts": dict(sorted(shard_entity_span_counts.items())),
        "per_document": per_document,
    }

