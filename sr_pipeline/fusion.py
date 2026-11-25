"""跨路候选对齐、去重与加权融合。

处理顺序固定为：

1. 每路独立做 min-max 归一化（领域算子内完成，含零跨度保护）
2. 按 doc_id 归并四路候选，完整保留每路原始分/归一化分/路名
3. 按配置选择去重口径：最高归一化分或首路命中
4. 加权求和得到最终分，逐项贡献可独立重算
5. 同分 doc_id 升序排序，按 top_k 截断并记录门限稳定性
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import PipelineConfig
from .errors import BusinessError, ErrorCode
from .precision import Ops
from .recall import RouteResult


@dataclass
class NormalizedHit:
    """归一化后的单路命中。"""

    route: str
    doc_id: str
    raw: Any
    normalized: Any
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class RouteContribution:
    """候选在某一路上的完整打分记录。"""

    route: str
    raw: Any
    raw_float: float
    normalized: Any
    normalized_float: float
    weight: float
    weighted: Any
    weighted_float: float
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class FusedCandidate:
    """融合后的单个候选。"""

    doc_id: str
    owner_route: str
    final_score: Any
    final_score_float: float
    contributions: list[RouteContribution]

    def recompute_float(self) -> float:
        """依据逐项明细独立重算最终分（浮点口径）。"""
        return sum(item.weighted_float for item in self.contributions)


@dataclass
class FusionResult:
    """单查询融合输出。"""

    query_id: str
    candidates: list[FusedCandidate]
    top_k: int
    threshold: float
    threshold_guard: float
    near_threshold: list[str]
    route_stats: dict[str, dict[str, Any]]
    merged_total: int
    dedup_policy: str


def normalize_route(
    result: RouteResult,
    ops: Ops,
    epsilon: float,
) -> tuple[list[NormalizedHit], dict[str, Any]]:
    """单路 min-max 归一化。

    零跨度（最大=最小）时归一化分全部置 0，避免除零并保持确定性。
    """
    if not result.hits:
        return [], {
            "route": result.route,
            "count": 0,
            "min": 0.0,
            "max": 0.0,
            "zero_span": True,
        }
    raws = [hit.raw for hit in result.hits]
    minimum = min(raws, key=ops.to_float)
    maximum = max(raws, key=ops.to_float)
    span = ops.sub(maximum, minimum)
    zero_span = abs(ops.to_float(span)) <= epsilon
    normalized_hits: list[NormalizedHit] = []
    for hit in result.hits:
        if zero_span:
            normalized = ops.from_float(0.0)
        else:
            normalized = ops.div(ops.sub(hit.raw, minimum), span)
        normalized_hits.append(
            NormalizedHit(
                route=hit.route,
                doc_id=hit.doc_id,
                raw=hit.raw,
                normalized=normalized,
                details=hit.details,
            )
        )
    stats = {
        "route": result.route,
        "count": len(result.hits),
        "min": ops.to_float(minimum),
        "max": ops.to_float(maximum),
        "zero_span": zero_span,
    }
    return normalized_hits, stats


def _merge(
    normalized_by_route: dict[str, list[NormalizedHit]],
    route_order: tuple[str, ...],
) -> dict[str, list[NormalizedHit]]:
    """按 doc_id 归并，命中列表保持 route_order 顺序。"""
    merged: dict[str, list[NormalizedHit]] = {}
    for route in route_order:
        for hit in normalized_by_route.get(route, ()):
            merged.setdefault(hit.doc_id, []).append(hit)
    return merged


def _select_owner(
    hits: list[NormalizedHit],
    dedup_policy: str,
    ops: Ops,
) -> NormalizedHit:
    if dedup_policy == "first_hit":
        return hits[0]
    if dedup_policy == "highest_norm":
        # 同分时按路序（即配置顺序）稳定取第一个，保证可复现
        return max(hits, key=lambda hit: (ops.to_float(hit.normalized),))
    raise BusinessError(
        ErrorCode.INVALID_CONFIG,
        f"未知去重口径: {dedup_policy}",
        field_path="fusion.dedup_policy",
    )


def fuse(
    query: dict,
    route_results: list[RouteResult],
    config: PipelineConfig,
    ops: Ops,
) -> FusionResult:
    """执行归一化、归并去重、加权融合与 top-k 门限裁决。"""
    epsilon = config.fusion.min_max_epsilon
    normalized_by_route: dict[str, list[NormalizedHit]] = {}
    route_stats: dict[str, dict[str, Any]] = {}
    for result in route_results:
        normalized, stats = normalize_route(result, ops, epsilon)
        normalized_by_route[result.route] = normalized
        route_stats[result.route] = stats

    merged = _merge(normalized_by_route, config.fusion.route_order)
    route_weights = {route.name: route.weight for route in config.routes}

    candidates: list[FusedCandidate] = []
    for doc_id, hits in merged.items():
        owner = _select_owner(hits, config.fusion.dedup_policy, ops)
        contributions: list[RouteContribution] = []
        final_score = ops.from_float(0.0)
        for hit in hits:
            weight = route_weights[hit.route]
            weighted = ops.mul(hit.normalized, ops.from_float(weight))
            final_score = ops.add(final_score, weighted)
            contributions.append(
                RouteContribution(
                    route=hit.route,
                    raw=hit.raw,
                    raw_float=ops.to_float(hit.raw),
                    normalized=hit.normalized,
                    normalized_float=ops.to_float(hit.normalized),
                    weight=weight,
                    weighted=weighted,
                    weighted_float=ops.to_float(weighted),
                    details=hit.details,
                )
            )
        candidates.append(
            FusedCandidate(
                doc_id=doc_id,
                owner_route=owner.route,
                final_score=final_score,
                final_score_float=ops.to_float(final_score),
                contributions=contributions,
            )
        )

    # 最终排序：分数降序，doc_id 升序破平
    candidates.sort(key=lambda candidate: (-candidate.final_score_float, candidate.doc_id))
    top_k = min(int(query["top_k"]), len(candidates))
    selected = candidates[:top_k]

    threshold = selected[-1].final_score_float if selected else 0.0
    guard = config.fusion.threshold_guard
    near_threshold: list[str] = []
    if selected:
        cutoff_score = selected[-1].final_score_float
        for candidate in candidates:
            if abs(candidate.final_score_float - cutoff_score) <= guard:
                near_threshold.append(candidate.doc_id)

    return FusionResult(
        query_id=query["query_id"],
        candidates=selected,
        top_k=top_k,
        threshold=threshold,
        threshold_guard=guard,
        near_threshold=near_threshold,
        route_stats=route_stats,
        merged_total=len(merged),
        dedup_policy=config.fusion.dedup_policy,
    )


def result_to_jsonable(result: FusionResult, ops: Ops) -> dict:
    """把融合结果转成可 JSON 序列化的字典，保留领域值与浮点视图。"""
    return {
        "query_id": result.query_id,
        "top_k": result.top_k,
        "threshold": result.threshold,
        "threshold_guard": result.threshold_guard,
        "near_threshold": result.near_threshold,
        "merged_total": result.merged_total,
        "dedup_policy": result.dedup_policy,
        "route_stats": result.route_stats,
        "candidates": [
            {
                "doc_id": candidate.doc_id,
                "owner_route": candidate.owner_route,
                "final_score": candidate.final_score_float,
                "final_score_domain": candidate.final_score,
                "precision_mode": ops.mode,
                "recomputed_score": candidate.recompute_float(),
                "contributions": [
                    {
                        "route": item.route,
                        "raw": item.raw_float,
                        "raw_domain": item.raw,
                        "normalized": item.normalized_float,
                        "normalized_domain": item.normalized,
                        "weight": item.weight,
                        "weighted": item.weighted_float,
                        "weighted_domain": item.weighted,
                        "details": item.details,
                    }
                    for item in candidate.contributions
                ],
            }
            for candidate in result.candidates
        ],
    }
