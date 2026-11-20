from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .errors import KGError
from .models import (
    CandidateTriple,
    Corpus,
    Document,
    DocumentEntities,
    EntitySpan,
)
from .util import json_safe_float, read_json, stable_id, verify_checksum


@dataclass(frozen=True)
class ScoreComponents:
    type_bp: int
    trigger_bp: int
    distance_bonus_bp: int
    gap_penalty_bp: int

    @property
    def maximum_bp(self) -> int:
        return self.type_bp + self.trigger_bp + self.distance_bonus_bp

    def to_dict(self) -> dict[str, int]:
        return {
            "type_bp": self.type_bp,
            "trigger_bp": self.trigger_bp,
            "distance_bonus_bp": self.distance_bonus_bp,
            "gap_penalty_bp": self.gap_penalty_bp,
        }


@dataclass(frozen=True)
class RelationPattern:
    line_no: int
    pattern_id: str
    relation_type: str
    relation_type_id: int
    head_type: str
    tail_type: str
    trigger: str
    max_gap: int
    mandatory: bool
    score: ScoreComponents

    def to_dict(self) -> dict[str, Any]:
        return {
            "line_no": self.line_no,
            "pattern_id": self.pattern_id,
            "relation_type": self.relation_type,
            "relation_type_id": self.relation_type_id,
            "head_type": self.head_type,
            "tail_type": self.tail_type,
            "trigger": self.trigger,
            "max_gap": self.max_gap,
            "mandatory": self.mandatory,
            "score_components": self.score.to_dict(),
        }


class RelationSchema:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.version = int(payload["schema_version"])
        self.checksum = str(payload["checksum"])
        raw_patterns = payload.get("patterns")
        if not isinstance(raw_patterns, list):
            raise KGError("E_SCHEMA_INVALID", "关系模式 patterns 必须是数组")
        self.patterns: tuple[RelationPattern, ...] = tuple(
            self._parse_pattern(item, position)
            for position, item in enumerate(raw_patterns, start=1)
        )
        self._validate()

    @classmethod
    def load(cls, path: str | Path) -> "RelationSchema":
        raw = read_json(path)
        if not isinstance(raw, dict):
            raise KGError(
                "E_SNAPSHOT_CORRUPT",
                "关系模式表必须是 JSON 对象",
                details={"snapshot_category": "relation_schema", "version": None},
            )
        version = raw.get("schema_version")
        if not isinstance(version, int) or version < 1:
            raise KGError(
                "E_SCHEMA_INVALID",
                "关系模式表 schema_version 必须为正整数",
                details={"schema_version": version},
            )
        verify_checksum(raw, category="relation_schema", version=version)
        return cls(raw)

    @staticmethod
    def _parse_pattern(item: dict[str, Any], position: int) -> RelationPattern:
        try:
            raw_components = item["score_components"]
            score = ScoreComponents(
                type_bp=int(raw_components["type_bp"]),
                trigger_bp=int(raw_components["trigger_bp"]),
                distance_bonus_bp=int(raw_components["distance_bonus_bp"]),
                gap_penalty_bp=int(raw_components["gap_penalty_bp"]),
            )
            return RelationPattern(
                line_no=int(item.get("line_no", position)),
                pattern_id=str(item["pattern_id"]),
                relation_type=str(item["relation_type"]),
                relation_type_id=int(item["relation_type_id"]),
                head_type=str(item["head_type"]),
                tail_type=str(item["tail_type"]),
                trigger=str(item["trigger"]),
                max_gap=int(item.get("max_gap", 64)),
                mandatory=bool(item.get("mandatory", False)),
                score=score,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise KGError(
                "E_SCHEMA_INVALID",
                f"关系模式第 {position} 行字段无效",
                details={"row": position, "error": str(exc)},
            ) from exc

    def _validate(self) -> None:
        if not self.patterns:
            raise KGError("E_SCHEMA_INVALID", "关系模式表不能为空")
        line_numbers: set[int] = set()
        relation_type_ids: dict[str, int] = {}
        pattern_ids: set[str] = set()
        for pattern in self.patterns:
            if pattern.line_no < 1 or pattern.line_no in line_numbers:
                raise KGError(
                    "E_SCHEMA_INVALID",
                    "模式行号必须唯一且为正整数",
                    details={"line_no": pattern.line_no},
                )
            line_numbers.add(pattern.line_no)
            if pattern.relation_type_id < 1:
                raise KGError(
                    "E_SCHEMA_INVALID",
                    "关系类型编号必须为正整数",
                    details={
                        "line_no": pattern.line_no,
                        "relation_type_id": pattern.relation_type_id,
                    },
                )
            if pattern.pattern_id in pattern_ids:
                raise KGError(
                    "E_SCHEMA_INVALID",
                    "pattern_id 必须唯一",
                    details={"pattern_id": pattern.pattern_id},
                )
            pattern_ids.add(pattern.pattern_id)
            if not pattern.trigger:
                raise KGError(
                    "E_SCHEMA_INVALID",
                    "触发词不能为空",
                    details={"line_no": pattern.line_no},
                )
            previous_id = relation_type_ids.setdefault(
                pattern.relation_type, pattern.relation_type_id
            )
            if previous_id != pattern.relation_type_id:
                raise KGError(
                    "E_SCHEMA_INVALID",
                    f"关系类型 {pattern.relation_type!r} 对应多个编号",
                    details={"relation_type": pattern.relation_type},
                )
            values = (
                pattern.score.type_bp,
                pattern.score.trigger_bp,
                pattern.score.distance_bonus_bp,
                pattern.score.gap_penalty_bp,
                pattern.max_gap,
            )
            if any(value < 0 for value in values):
                raise KGError(
                    "E_SCHEMA_INVALID",
                    "置信分项与最大间距不能为负数",
                    details={"line_no": pattern.line_no},
                )
            if pattern.score.maximum_bp > 10000:
                raise KGError(
                    "E_SCHEMA_INVALID",
                    "置信分项最大值合计不能超过 10000 基点",
                    details={
                        "line_no": pattern.line_no,
                        "maximum_bp": pattern.score.maximum_bp,
                    },
                )

    @property
    def mandatory_patterns(self) -> tuple[RelationPattern, ...]:
        return tuple(pattern for pattern in self.patterns if pattern.mandatory)

    def checksum_record(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "checksum": self.checksum,
            "pattern_count": len(self.patterns),
            "mandatory_pattern_lines": [
                pattern.line_no for pattern in self.mandatory_patterns
            ],
        }

    def to_dict(self) -> dict[str, Any]:
        return dict(self.payload)


def score_candidate(
    pattern: RelationPattern,
    head: EntitySpan,
    tail: EntitySpan,
) -> tuple[int, list[dict[str, Any]]]:
    gap = tail.start - head.end
    distance_bp = max(
        0,
        pattern.score.distance_bonus_bp - pattern.score.gap_penalty_bp * gap,
    )
    components = [
        {
            "name": "entity_type",
            "basis_points": pattern.score.type_bp,
            "detail": {
                "head_type": head.type,
                "tail_type": tail.type,
                "expected_head_type": pattern.head_type,
                "expected_tail_type": pattern.tail_type,
            },
        },
        {
            "name": "trigger",
            "basis_points": pattern.score.trigger_bp,
            "detail": {
                "trigger": pattern.trigger,
                "mandatory_pattern": pattern.mandatory,
            },
        },
        {
            "name": "distance",
            "basis_points": distance_bp,
            "detail": {
                "gap_chars": gap,
                "distance_bonus_bp": pattern.score.distance_bonus_bp,
                "gap_penalty_bp": pattern.score.gap_penalty_bp,
            },
        },
    ]
    total = sum(item["basis_points"] for item in components)
    return total, components


def _find_trigger_offset(
    text: str,
    head: EntitySpan,
    tail: EntitySpan,
    pattern: RelationPattern,
) -> int | None:
    if head.end > tail.start:
        return None
    if tail.start - head.end > pattern.max_gap:
        return None
    offset = text.find(pattern.trigger, head.end, tail.start)
    if offset < 0:
        return None
    if offset + len(pattern.trigger) > tail.start:
        return None
    return offset


def _make_candidate(
    *,
    doc_id: str,
    sent_no: int,
    text: str,
    head: EntitySpan,
    tail: EntitySpan,
    pattern: RelationPattern,
    trigger_offset: int,
    path: str,
) -> CandidateTriple:
    confidence_bp, components = score_candidate(pattern, head, tail)
    identity = {
        "doc_id": doc_id,
        "sent_no": sent_no,
        "head_span_id": head.span_id,
        "tail_span_id": tail.span_id,
        "relation_type": pattern.relation_type,
    }
    return CandidateTriple(
        triple_id=stable_id("tr", identity),
        doc_id=doc_id,
        sent_no=sent_no,
        head_span_id=head.span_id,
        tail_span_id=tail.span_id,
        head_start=head.start,
        head_end=head.end,
        tail_start=tail.start,
        tail_end=tail.end,
        head_text=text[head.start : head.end],
        tail_text=text[tail.start : tail.end],
        head_entity_id=head.entity_id,
        tail_entity_id=tail.entity_id,
        relation_type=pattern.relation_type,
        relation_type_id=pattern.relation_type_id,
        confidence_bp=confidence_bp,
        confidence=json_safe_float(confidence_bp),
        score_components=components,
        pattern_line=pattern.line_no,
        pattern_id=pattern.pattern_id,
        trigger=pattern.trigger,
        trigger_offset=trigger_offset,
        path=path,
    )


def _candidate_sort_key(triple: CandidateTriple) -> tuple[Any, ...]:
    return (
        -triple.confidence_bp,
        triple.head_start,
        triple.tail_start,
        triple.relation_type_id,
        triple.sent_no,
        triple.head_end,
        triple.tail_end,
        triple.pattern_line,
        triple.trigger_offset,
        triple.relation_type,
    )


def _deduplicate_candidates(
    candidates: Iterable[CandidateTriple],
) -> list[CandidateTriple]:
    best: dict[tuple[str, int, int, int, int, str], CandidateTriple] = {}
    for candidate in candidates:
        key = (
            candidate.doc_id,
            candidate.sent_no,
            candidate.head_start,
            candidate.head_end,
            candidate.tail_start,
            candidate.relation_type,
        )
        existing = best.get(key)
        if existing is None or _candidate_sort_key(candidate) < _candidate_sort_key(
            existing
        ):
            best[key] = candidate
    return sorted(best.values(), key=_candidate_sort_key)


def _iter_all_pairs(
    spans: list[EntitySpan],
) -> Iterable[tuple[EntitySpan, EntitySpan]]:
    for index, head in enumerate(spans):
        for tail in spans[index + 1 :]:
            yield head, tail


def _iter_neighbor_pairs(
    spans: list[EntitySpan],
    neighbor_limit: int,
) -> Iterable[tuple[EntitySpan, EntitySpan]]:
    if neighbor_limit <= 0:
        return
    for index, head in enumerate(spans):
        for tail in spans[index + 1 : index + 1 + neighbor_limit]:
            yield head, tail


def extract_relations(
    *,
    document: Document,
    entities: DocumentEntities,
    schema: RelationSchema,
    mode: str,
    neighbor_limit: int,
) -> list[CandidateTriple]:
    if mode not in {"exact", "approx"}:
        raise ValueError(f"unsupported relation enumeration mode: {mode}")
    spans_by_sentence: dict[int, list[EntitySpan]] = {}
    sentence_text = {}
    for sentence in document.sentences:
        spans_by_sentence[sentence.sent_no] = []
        sentence_text[sentence.sent_no] = sentence.text
    for span in entities.spans:
        spans_by_sentence.setdefault(span.sent_no, []).append(span)
    for sentence_spans in spans_by_sentence.values():
        sentence_spans.sort(key=lambda span: (span.start, span.end, span.type_id))

    candidates: list[CandidateTriple] = []
    for sent_no in sorted(spans_by_sentence):
        spans = spans_by_sentence[sent_no]
        text = sentence_text[sent_no]
        for pattern in schema.patterns:
            pair_iterator = (
                _iter_all_pairs(spans)
                if mode == "exact" or pattern.mandatory
                else _iter_neighbor_pairs(spans, neighbor_limit)
            )
            for head, tail in pair_iterator:
                if head.type != pattern.head_type or tail.type != pattern.tail_type:
                    continue
                offset = _find_trigger_offset(text, head, tail, pattern)
                if offset is None:
                    continue
                candidates.append(
                    _make_candidate(
                        doc_id=document.doc_id,
                        sent_no=sent_no,
                        text=text,
                        head=head,
                        tail=tail,
                        pattern=pattern,
                        trigger_offset=offset,
                        path=mode,
                    )
                )
    return _deduplicate_candidates(candidates)


@dataclass(frozen=True)
class PathCost:
    exact_pair_scans: int
    exact_comparisons: int
    approximate_pair_scans: int
    approximate_comparisons: int
    mandatory_pair_scans: int
    estimated_pairs: int
    estimated_comparisons: int
    estimated_memory_bytes: int
    exact_memory_bytes: int
    approximate_memory_bytes: int
    per_document: dict[str, dict[str, int]]


def _sentence_pair_count(entity_count: int) -> int:
    return entity_count * (entity_count - 1) // 2


def _neighbor_pair_count(entity_count: int, neighbor_limit: int) -> int:
    if entity_count <= 1:
        return 0
    limit = min(neighbor_limit, entity_count - 1)
    return limit * entity_count - limit * (limit + 1) // 2


def estimate_path_cost(
    *,
    corpus: Corpus,
    entities_by_doc: dict[str, DocumentEntities],
    schema: RelationSchema,
    neighbor_limit: int,
    bytes_per_pair: int,
    shard_overhead_units: int = 8,
) -> PathCost:
    pattern_count = len(schema.patterns)
    mandatory_count = len(schema.mandatory_patterns)
    nonmandatory_count = pattern_count - mandatory_count
    exact_pair_scans = 0
    approximate_pair_scans = 0
    mandatory_pair_scans = 0
    per_document: dict[str, dict[str, int]] = {}
    for document in corpus.documents:
        sentence_counts: dict[int, int] = {}
        for span in entities_by_doc[document.doc_id].spans:
            sentence_counts[span.sent_no] = sentence_counts.get(span.sent_no, 0) + 1
        exact_pairs = sum(
            _sentence_pair_count(count) for count in sentence_counts.values()
        )
        neighbor_pairs = sum(
            _neighbor_pair_count(count, neighbor_limit)
            for count in sentence_counts.values()
        )
        mandatory_pairs = exact_pairs
        exact_pair_scans += exact_pairs
        approximate_pair_scans += neighbor_pairs
        mandatory_pair_scans += mandatory_pairs
        per_document[document.doc_id] = {
            "entity_count": sum(sentence_counts.values()),
            "max_sentence_entity_count": max(sentence_counts.values(), default=0),
            "exact_pair_scans": exact_pairs,
            "neighbor_pair_scans": neighbor_pairs,
            "mandatory_pair_scans": mandatory_pairs,
            "exact_comparisons": exact_pairs * pattern_count,
            "approximate_comparisons": (
                mandatory_pairs * mandatory_count
                + neighbor_pairs * nonmandatory_count
            ),
        }
    shard_overhead = corpus.shard_count * shard_overhead_units
    exact_comparisons = exact_pair_scans * pattern_count + shard_overhead
    approximate_comparisons = (
        mandatory_pair_scans * mandatory_count
        + approximate_pair_scans * nonmandatory_count
        + shard_overhead
    )
    return PathCost(
        exact_pair_scans=exact_pair_scans,
        exact_comparisons=exact_comparisons,
        approximate_pair_scans=approximate_pair_scans,
        approximate_comparisons=approximate_comparisons,
        mandatory_pair_scans=mandatory_pair_scans,
        estimated_pairs=exact_pair_scans,
        estimated_comparisons=exact_comparisons,
        estimated_memory_bytes=max(exact_comparisons, approximate_comparisons)
        * bytes_per_pair,
        exact_memory_bytes=exact_comparisons * bytes_per_pair,
        approximate_memory_bytes=approximate_comparisons * bytes_per_pair,
        per_document=dict(sorted(per_document.items())),
    )

