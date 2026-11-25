"""精度档误差传播诊断。

float64 全精度路径作为参考真值，定点路径在四个级别上对账：

1. 各路原始分（raw）
2. 归一化分（normalized）
3. 加权融合分（weighted / final）
4. top-k 门限比较（threshold 与近门限候选）

每级输出绝对偏差、相对偏差与相对上一级的误差放大比，
并汇总主导误差来源与门限附近的数值稳定性保护结果。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import PipelineConfig
from .errors import BusinessError, ErrorCode
from .fusion import FusionResult
from .precision import MODE_FLOAT64, MODE_FIXED

RELATIVE_FLOOR = 1e-12


def _abs_error(actual: float, reference: float) -> float:
    return abs(actual - reference)


def _rel_error(actual: float, reference: float) -> float:
    denominator = max(abs(reference), RELATIVE_FLOOR)
    return abs(actual - reference) / denominator


def _amplification(downstream_rel: float, upstream_rel: float) -> float:
    """下游相对偏差 / 上游相对偏差；上游近零时按地板值处理。"""
    if upstream_rel <= RELATIVE_FLOOR:
        return 0.0 if downstream_rel <= RELATIVE_FLOOR else float("inf")
    return downstream_rel / upstream_rel


@dataclass
class LevelError:
    level: str
    route: str | None
    doc_id: str
    reference: float
    actual: float
    abs_error: float
    rel_error: float
    amplification: float

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "level": self.level,
            "doc_id": self.doc_id,
            "reference": self.reference,
            "actual": self.actual,
            "abs_error": self.abs_error,
            "rel_error": self.rel_error,
            "amplification": self.amplification,
        }
        if self.route is not None:
            payload["route"] = self.route
        return payload


@dataclass
class QueryDiagnosis:
    query_id: str
    ordering_preserved: bool
    dedup_preserved: bool
    levels: list[LevelError] = field(default_factory=list)
    dominant_sources: list[dict[str, Any]] = field(default_factory=list)
    threshold: dict[str, Any] = field(default_factory=dict)


def _index_contributions(result: FusionResult) -> dict[tuple[str, str], Any]:
    indexed: dict[tuple[str, str], Any] = {}
    for candidate in result.candidates:
        for contribution in candidate.contributions:
            indexed[(candidate.doc_id, contribution.route)] = contribution
    return indexed


def diagnose_query(
    query_id: str,
    reference: FusionResult,
    actual: FusionResult,
    config: PipelineConfig,
) -> QueryDiagnosis:
    """对单查询生成完整误差账本并校验顺序/去重不变量。"""
    if config.precision.mode != MODE_FIXED:
        raise BusinessError(
            ErrorCode.INVALID_CONFIG,
            "诊断要求对照路径为 fixed 定点档",
            field_path="precision.mode",
        )

    ref_order = [candidate.doc_id for candidate in reference.candidates]
    actual_order = [candidate.doc_id for candidate in actual.candidates]
    ordering_preserved = ref_order == actual_order
    ref_owners = {c.doc_id: c.owner_route for c in reference.candidates}
    actual_owners = {c.doc_id: c.owner_route for c in actual.candidates}
    dedup_preserved = ref_owners == actual_owners and len(actual_order) == len(
        set(actual_order)
    )
    if not ordering_preserved or not dedup_preserved:
        raise BusinessError(
            ErrorCode.PRECISION_DRIFT,
            "切换精度档改变了候选顺序或去重结果",
            field_path=f"query={query_id}",
            context={
                "reference_order": ref_order,
                "actual_order": actual_order,
                "ordering_preserved": ordering_preserved,
                "dedup_preserved": dedup_preserved,
            },
        )

    diagnosis = QueryDiagnosis(
        query_id=query_id,
        ordering_preserved=True,
        dedup_preserved=True,
    )
    ref_contrib = _index_contributions(reference)
    actual_contrib = _index_contributions(actual)
    route_totals: dict[str, float] = {}

    for key in sorted(ref_contrib):
        ref_item = ref_contrib[key]
        actual_item = actual_contrib[key]
        doc_id, route = key
        raw_rel = _rel_error(actual_item.raw_float, ref_item.raw_float)
        norm_rel = _rel_error(
            actual_item.normalized_float, ref_item.normalized_float
        )
        weighted_rel = _rel_error(
            actual_item.weighted_float, ref_item.weighted_float
        )
        raw_level = LevelError(
            level="raw",
            route=route,
            doc_id=doc_id,
            reference=ref_item.raw_float,
            actual=actual_item.raw_float,
            abs_error=_abs_error(actual_item.raw_float, ref_item.raw_float),
            rel_error=raw_rel,
            amplification=0.0,
        )
        norm_level = LevelError(
            level="normalized",
            route=route,
            doc_id=doc_id,
            reference=ref_item.normalized_float,
            actual=actual_item.normalized_float,
            abs_error=_abs_error(
                actual_item.normalized_float, ref_item.normalized_float
            ),
            rel_error=norm_rel,
            amplification=_amplification(norm_rel, raw_rel),
        )
        weighted_level = LevelError(
            level="weighted",
            route=route,
            doc_id=doc_id,
            reference=ref_item.weighted_float,
            actual=actual_item.weighted_float,
            abs_error=_abs_error(
                actual_item.weighted_float, ref_item.weighted_float
            ),
            rel_error=weighted_rel,
            amplification=_amplification(weighted_rel, norm_rel),
        )
        diagnosis.levels.extend([raw_level, norm_level, weighted_level])
        route_totals[route] = route_totals.get(route, 0.0) + weighted_level.abs_error

    ref_final = {c.doc_id: c for c in reference.candidates}
    for candidate in actual.candidates:
        ref_candidate = ref_final[candidate.doc_id]
        upstream = max(
            (
                level.rel_error
                for level in diagnosis.levels
                if level.doc_id == candidate.doc_id
                and level.level == "weighted"
            ),
            default=0.0,
        )
        final_rel = _rel_error(
            candidate.final_score_float, ref_candidate.final_score_float
        )
        diagnosis.levels.append(
            LevelError(
                level="final",
                route=None,
                doc_id=candidate.doc_id,
                reference=ref_candidate.final_score_float,
                actual=candidate.final_score_float,
                abs_error=_abs_error(
                    candidate.final_score_float, ref_candidate.final_score_float
                ),
                rel_error=final_rel,
                amplification=_amplification(final_rel, upstream),
            )
        )

    diagnosis.dominant_sources = [
        {"route": route, "weighted_abs_error": total}
        for route, total in sorted(
            route_totals.items(), key=lambda item: (-item[1], item[0])
        )
    ]

    threshold_abs = _abs_error(actual.threshold, reference.threshold)
    guard = config.fusion.threshold_guard
    diagnosis.threshold = {
        "reference_threshold": reference.threshold,
        "actual_threshold": actual.threshold,
        "abs_error": threshold_abs,
        "rel_error": _rel_error(actual.threshold, reference.threshold),
        "guard_band": guard,
        "stable": threshold_abs <= guard,
        "reference_near_threshold": reference.near_threshold,
        "actual_near_threshold": actual.near_threshold,
        "near_threshold_match": reference.near_threshold
        == actual.near_threshold,
    }
    return diagnosis


def diagnosis_to_dict(diagnosis: QueryDiagnosis) -> dict[str, Any]:
    return {
        "query_id": diagnosis.query_id,
        "ordering_preserved": diagnosis.ordering_preserved,
        "dedup_preserved": diagnosis.dedup_preserved,
        "dominant_sources": diagnosis.dominant_sources,
        "threshold": diagnosis.threshold,
        "levels": [level.to_dict() for level in diagnosis.levels],
        "reference_mode": MODE_FLOAT64,
        "actual_mode": MODE_FIXED,
    }
