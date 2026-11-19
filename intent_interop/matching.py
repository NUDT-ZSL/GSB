"""Deterministic longest-first intent matching."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

import numpy as np

from .errors import IntentError
from .models import (
    ConfigDocument,
    Hit,
    IntentSpec,
    MatchResult,
    QuerySample,
)


@dataclass(frozen=True)
class ResourceLimits:
    max_keywords: int = 64
    max_query_codepoints: int = 256
    max_match_entries: int = 4096
    policy: str = "error"

    def validate(self) -> None:
        for name in (
            "max_keywords",
            "max_query_codepoints",
            "max_match_entries",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise IntentError(
                    "E_CONFIG_INVALID",
                    f"{name} 必须为正整数",
                    stage="match",
                    field=f"resource_limits.{name}",
                    value=value,
                )
        if self.policy not in {"error", "prefix"}:
            raise IntentError(
                "E_CONFIG_INVALID",
                "资源超限策略必须为 error 或 prefix",
                stage="match",
                field="resource_limits.policy",
                value=self.policy,
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_keywords": self.max_keywords,
            "max_query_codepoints": self.max_query_codepoints,
            "max_match_entries": self.max_match_entries,
            "policy": self.policy,
        }


@dataclass(frozen=True)
class Occurrence:
    intent: str
    keyword: str
    start: int
    end: int
    weight: float

    @property
    def sort_key(self) -> tuple[int, str, int, str]:
        return (-len(self.keyword), self.intent, self.start, self.keyword)

    def to_hit(self) -> Hit:
        return Hit(
            intent=self.intent,
            keyword=self.keyword,
            start=self.start,
            end=self.end,
            weight=self.weight,
        )


@dataclass(frozen=True)
class MatchBatch:
    document: ConfigDocument
    results: tuple[MatchResult, ...]
    self_check: dict[str, Any]
    degradation_categories: tuple[str, ...] = ()
    resource_limits: ResourceLimits = field(default_factory=ResourceLimits)

    def to_report(self) -> dict[str, Any]:
        return {
            "snapshot": self.document.snapshot_summary(),
            "resource_limits": self.resource_limits.to_dict(),
            "degradation_categories": list(self.degradation_categories),
            "self_check": self.self_check,
            "results": [result.to_dict() for result in self.results],
        }


def _require_matchable(document: ConfigDocument) -> None:
    if document.source_version != "v3":
        raise IntentError(
            "E_CONFIG_INVALID",
            "判定前必须将配置迁移到 v3 规范模型",
            stage="match",
            field="source_version",
            value=document.source_version,
        )
    for spec in document.intents:
        if (
            spec.threshold is None
            or spec.fallback_intent is None
            or spec.label_path is None
            or spec.slots is None
            or spec.weight_vector is None
        ):
            raise IntentError(
                "E_CONFIG_INVALID",
                "配置缺少 v3 判定字段",
                stage="match",
                field="intent",
                intent=spec.intent,
                line_no=spec.line_no,
            )
        if len(spec.weight_vector) != len(spec.keywords):
            raise IntentError(
                "E_CONFIG_INVALID",
                "weight_vector 长度与关键词数量不一致",
                stage="match",
                field="weight_vector",
                intent=spec.intent,
                line_no=spec.line_no,
            )


def _total_keywords(document: ConfigDocument) -> int:
    return sum(len(spec.keywords) for spec in document.intents)


def _find_occurrences(
    spec: IntentSpec,
    query_text: str,
    *,
    prefix_only: bool,
) -> list[Occurrence]:
    occurrences: list[Occurrence] = []
    weights = spec.weight_vector or (1.0,) * len(spec.keywords)
    for keyword, weight in zip(spec.keywords, weights):
        if prefix_only:
            if query_text.startswith(keyword):
                occurrences.append(
                    Occurrence(
                        intent=spec.intent,
                        keyword=keyword,
                        start=0,
                        end=len(keyword),
                        weight=weight,
                    )
                )
            continue
        start = query_text.find(keyword)
        while start != -1:
            occurrences.append(
                Occurrence(
                    intent=spec.intent,
                    keyword=keyword,
                    start=start,
                    end=start + len(keyword),
                    weight=weight,
                )
            )
            start = query_text.find(keyword, start + 1)
    return occurrences


def _overlaps(left: Occurrence, right: Occurrence) -> bool:
    return left.start < right.end and right.start < left.end


def _select_non_overlapping(
    occurrences: Iterable[Occurrence],
) -> tuple[Occurrence | None, tuple[Hit, ...]]:
    ordered = sorted(occurrences, key=lambda occurrence: occurrence.sort_key)
    selected: list[Occurrence] = []
    for occurrence in ordered:
        if all(not _overlaps(occurrence, chosen) for chosen in selected):
            selected.append(occurrence)
    top = ordered[0] if ordered else None
    selected.sort(
        key=lambda occurrence: (
            occurrence.start,
            occurrence.end,
            occurrence.intent,
            occurrence.keyword,
        )
    )
    return top, tuple(occurrence.to_hit() for occurrence in selected)


def _confidence(top: Occurrence | None, query_text: str) -> float:
    if top is None:
        return 0.0
    coverage = len(top.keyword) / len(query_text)
    score = np.clip(
        np.float64(coverage) * np.float64(top.weight),
        np.float64(0.0),
        np.float64(1.0),
    )
    return round(float(score), 6)


def _ensure_controlled_fallback(
    *,
    document: ConfigDocument,
    fallback: str,
    query_id: str,
    trigger_label: str | None,
    category: str,
) -> str:
    if fallback not in document.controlled_labels:
        raise IntentError(
            "E_LABEL_RETIRED",
            "回退标签不在受控标签集合中",
            stage="match",
            query_id=query_id,
            field="fallback_intent",
            category="fallback_not_controlled",
            intent=trigger_label,
            fallback_intent=fallback,
        )
    return fallback


def match_query(
    sample: QuerySample,
    document: ConfigDocument,
    *,
    limits: ResourceLimits,
    prefix_only: bool = False,
    retired_policy: str = "fallback",
    degradation_categories: tuple[str, ...] = (),
) -> MatchResult:
    query_codepoints = len(sample.query_text)
    if query_codepoints > limits.max_query_codepoints:
        raise IntentError(
            "E_RESOURCE_LIMIT",
            "单条查询码点长度超限",
            stage="match",
            query_id=sample.query_id,
            line_no=sample.line_no,
            field="query_codepoints",
            limit=limits.max_query_codepoints,
            actual=query_codepoints,
        )

    occurrences: list[Occurrence] = []
    for spec in document.intents:
        occurrences.extend(
            _find_occurrences(spec, sample.query_text, prefix_only=prefix_only)
        )
    categories = list(degradation_categories)
    if len(occurrences) > limits.max_match_entries:
        if limits.policy == "error":
            raise IntentError(
                "E_RESOURCE_LIMIT",
                "驻留匹配中间表超限",
                stage="match",
                query_id=sample.query_id,
                line_no=sample.line_no,
                field="match_table_entries",
                limit=limits.max_match_entries,
                actual=len(occurrences),
            )
        occurrences = [occurrence for occurrence in occurrences if occurrence.start == 0]
        categories.append("match_table_prefix_only")
        if len(occurrences) > limits.max_match_entries:
            raise IntentError(
                "E_RESOURCE_LIMIT",
                "前缀降级后驻留匹配中间表仍超限",
                stage="match",
                query_id=sample.query_id,
                line_no=sample.line_no,
                field="match_table_entries",
                limit=limits.max_match_entries,
                actual=len(occurrences),
                category="prefix_degradation_insufficient",
            )

    top, hits = _select_non_overlapping(occurrences)
    confidence = _confidence(top, sample.query_text)
    if top is None:
        fallback = document.global_fallback
        _ensure_controlled_fallback(
            document=document,
            fallback=fallback,
            query_id=sample.query_id,
            trigger_label=None,
            category="no_match",
        )
        return MatchResult(
            query_id=sample.query_id,
            query_text=sample.query_text,
            locale=sample.locale,
            label_raw=sample.label_raw,
            matched_intent=None,
            intent=fallback,
            confidence=0.0,
            threshold=0.0,
            fallback_category="no_match",
            trigger_label=None,
            hits=hits,
            code=None,
            degradation_categories=tuple(categories),
            top_hit=None,
        )

    specs = {spec.intent: spec for spec in document.intents}
    top_spec = specs[top.intent]
    threshold = top_spec.threshold or 0.0
    fallback_intent = top_spec.fallback_intent or document.global_fallback

    if confidence + 1e-12 < threshold:
        _ensure_controlled_fallback(
            document=document,
            fallback=fallback_intent,
            query_id=sample.query_id,
            trigger_label=top.intent,
            category="below_threshold",
        )
        return MatchResult(
            query_id=sample.query_id,
            query_text=sample.query_text,
            locale=sample.locale,
            label_raw=sample.label_raw,
            matched_intent=top.intent,
            intent=fallback_intent,
            confidence=confidence,
            threshold=threshold,
            fallback_category="below_threshold",
            trigger_label=top.intent,
            hits=hits,
            code=None,
            degradation_categories=tuple(categories),
            top_hit=top.to_hit(),
        )

    if top.intent not in document.controlled_labels:
        if retired_policy == "reject":
            raise IntentError(
                "E_LABEL_RETIRED",
                f"命中标签已退出受控集合：{top.intent}",
                stage="match",
                query_id=sample.query_id,
                line_no=sample.line_no,
                intent=top.intent,
                field="controlled_labels",
                category="label_retired",
            )
        if retired_policy != "fallback":
            raise IntentError(
                "E_CONFIG_INVALID",
                "retired_policy 必须为 fallback 或 reject",
                stage="match",
                field="retired_policy",
                value=retired_policy,
            )
        _ensure_controlled_fallback(
            document=document,
            fallback=fallback_intent,
            query_id=sample.query_id,
            trigger_label=top.intent,
            category="label_retired",
        )
        return MatchResult(
            query_id=sample.query_id,
            query_text=sample.query_text,
            locale=sample.locale,
            label_raw=sample.label_raw,
            matched_intent=top.intent,
            intent=fallback_intent,
            confidence=confidence,
            threshold=threshold,
            fallback_category="label_retired",
            trigger_label=top.intent,
            hits=hits,
            code="E_LABEL_RETIRED",
            degradation_categories=tuple(categories),
            top_hit=top.to_hit(),
        )

    return MatchResult(
        query_id=sample.query_id,
        query_text=sample.query_text,
        locale=sample.locale,
        label_raw=sample.label_raw,
        matched_intent=top.intent,
        intent=top.intent,
        confidence=confidence,
        threshold=threshold,
        fallback_category=None,
        trigger_label=None,
        hits=hits,
        code=None,
        degradation_categories=tuple(categories),
        top_hit=top.to_hit(),
    )


def self_check(
    samples: Iterable[QuerySample],
    results: Iterable[MatchResult],
) -> dict[str, Any]:
    sample_list = list(samples)
    result_list = list(results)
    sample_by_id = {sample.query_id: sample for sample in sample_list}
    result_by_id: dict[str, MatchResult] = {}
    for result in result_list:
        if result.query_id in result_by_id:
            raise IntentError(
                "E_INCONSISTENT",
                "判定记录 query_id 重复",
                stage="self_check",
                query_id=result.query_id,
                category="duplicate_result",
            )
        result_by_id[result.query_id] = result

    expected_ids = set(sample_by_id)
    actual_ids = set(result_by_id)
    if expected_ids != actual_ids:
        raise IntentError(
            "E_INCONSISTENT",
            "判定记录未覆盖全部查询",
            stage="self_check",
            category="coverage",
            missing=sorted(expected_ids - actual_ids),
            unexpected=sorted(actual_ids - expected_ids),
        )

    direct = {result.query_id for result in result_list if not result.is_fallback}
    fallback = {result.query_id for result in result_list if result.is_fallback}
    if direct & fallback:
        raise IntentError(
            "E_INCONSISTENT",
            "直接命中集合与回退集合相交",
            stage="self_check",
            category="fallback_partition",
            query_ids=sorted(direct & fallback),
        )
    if direct | fallback != expected_ids:
        raise IntentError(
            "E_INCONSISTENT",
            "直接命中集合与回退集合未覆盖全部判定记录",
            stage="self_check",
            category="fallback_partition",
            uncovered=sorted(expected_ids - (direct | fallback)),
        )

    for result in result_list:
        sample = sample_by_id[result.query_id]
        query_length = len(sample.query_text)
        if result.query_text != sample.query_text:
            raise IntentError(
                "E_INCONSISTENT",
                "判定结果查询文本与样本不一致",
                stage="self_check",
                query_id=result.query_id,
                category="query_text",
            )
        ranges: list[tuple[int, int, str]] = []
        for hit in result.hits:
            if hit.start < 0 or hit.end > query_length or hit.start >= hit.end:
                raise IntentError(
                    "E_INCONSISTENT",
                    "命中偏移越界",
                    stage="self_check",
                    query_id=result.query_id,
                    category="offset_bounds",
                    hit=hit.to_dict(),
                    query_codepoints=query_length,
                )
            ranges.append((hit.start, hit.end, hit.keyword))
        ranges.sort()
        for previous, current in zip(ranges, ranges[1:]):
            if previous[1] > current[0]:
                raise IntentError(
                    "E_INCONSISTENT",
                    "命中关键词偏移重叠",
                    stage="self_check",
                    query_id=result.query_id,
                    category="offset_overlap",
                    previous=previous,
                    current=current,
                )
        if result.is_fallback and result.fallback_category not in {
            "no_match",
            "below_threshold",
            "label_retired",
        }:
            raise IntentError(
                "E_INCONSISTENT",
                "存在未知回退类别",
                stage="self_check",
                query_id=result.query_id,
                category="fallback_category",
                value=result.fallback_category,
            )
        if not result.is_fallback and result.matched_intent != result.intent:
            raise IntentError(
                "E_INCONSISTENT",
                "直接命中的 matched_intent 与 intent 不一致",
                stage="self_check",
                query_id=result.query_id,
                category="direct_intent",
            )
        if result.top_hit is not None and result.top_hit not in result.hits:
            raise IntentError(
                "E_INCONSISTENT",
                "top_hit 不在非重叠命中表中",
                stage="self_check",
                query_id=result.query_id,
                category="top_hit",
            )

    return {
        "status": "OK",
        "checks": [
            "offset_bounds",
            "offset_non_overlap",
            "fallback_direct_disjoint",
            "fallback_direct_cover_all",
            "query_text_identity",
            "fallback_category_known",
        ],
        "query_count": len(sample_list),
        "direct_count": len(direct),
        "fallback_count": len(fallback),
        "covered_count": len(direct | fallback),
    }


def match_document(
    samples: Iterable[QuerySample],
    document: ConfigDocument,
    *,
    limits: ResourceLimits | None = None,
    retired_policy: str = "fallback",
    injector: Any | None = None,
) -> MatchBatch:
    limits = limits or ResourceLimits()
    limits.validate()
    _require_matchable(document)
    if retired_policy not in {"fallback", "reject"}:
        raise IntentError(
            "E_CONFIG_INVALID",
            "retired_policy 必须为 fallback 或 reject",
            stage="match",
            field="retired_policy",
            value=retired_policy,
        )

    sample_list = list(samples)
    degradation_categories: list[str] = []
    prefix_only = False
    keyword_count = _total_keywords(document)
    if keyword_count > limits.max_keywords:
        if limits.policy == "error":
            raise IntentError(
                "E_RESOURCE_LIMIT",
                "配置关键词总数超限",
                stage="match",
                field="total_keywords",
                limit=limits.max_keywords,
                actual=keyword_count,
            )
        prefix_only = True
        degradation_categories.append("keyword_budget_prefix_only")

    if injector is not None:
        injector.fire(
            "match",
            {
                "query_id": sample_list[0].query_id if sample_list else None,
                "line_no": sample_list[0].line_no if sample_list else None,
                "document": document,
            },
        )

    results: list[MatchResult] = []
    for sample in sample_list:
        results.append(
            match_query(
                sample,
                document,
                limits=limits,
                prefix_only=prefix_only,
                retired_policy=retired_policy,
                degradation_categories=tuple(degradation_categories),
            )
        )
    summary = self_check(sample_list, results)
    return MatchBatch(
        document=document,
        results=tuple(results),
        self_check=summary,
        degradation_categories=tuple(degradation_categories),
        resource_limits=limits,
    )
