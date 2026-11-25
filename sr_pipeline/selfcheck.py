"""跨模块一致性自检。

自检不依赖外部参考数据，只利用链路自身产物做不变量校验：

1. 四路处理的文档集合与索引 doc_id 集合一致
2. 最终融合分可由逐项加权贡献独立重算
3. 增量失效的重算/复用清单覆盖全部受影响查询且互不重叠
4. 融合候选集无重复 doc_id
"""

from __future__ import annotations

from typing import Any, Iterable

from .errors import BusinessError, ErrorCode
from .fusion import FusionResult
from .index import SearchIndex
from .recall import RouteResult

RECOMPUTE_TOL = 1e-9


def check_route_doc_sets(
    index: SearchIndex, route_results: Iterable[RouteResult]
) -> None:
    """四路看到的文档集合必须是索引文档集合的子集且来源一致。"""
    index_set = set(index.doc_ids)
    seen_by_route: dict[str, set[str]] = {}
    for result in route_results:
        route_docs = {hit.doc_id for hit in result.hits}
        missing = route_docs - index_set
        if missing:
            raise BusinessError(
                ErrorCode.SELF_CHECK_FAILED,
                f"路 {result.route} 出现索引外文档: {sorted(missing)}",
                field_path=f"route={result.route}",
            )
        seen_by_route[result.route] = route_docs
        eligible = set(result.eligible_doc_ids)
        outside = eligible - index_set
        if outside:
            raise BusinessError(
                ErrorCode.SELF_CHECK_FAILED,
                f"路 {result.route} 截断前候选包含索引外文档: {sorted(outside)}",
                field_path=f"route={result.route}",
            )
    # 向量近似路扫描全量文档（截断前），必须严格等于索引集合
    for full_route in ("vector_ann",):
        eligible_map = {
            result.route: set(result.eligible_doc_ids) for result in route_results
        }
        if full_route in eligible_map and eligible_map[full_route] != index_set:
            raise BusinessError(
                ErrorCode.SELF_CHECK_FAILED,
                f"路 {full_route} 文档集合与索引不一致",
                field_path=f"route={full_route}",
            )
    # 其余各路至少要有命中，且全部来自索引文档集合
    for route, docs_in_route in seen_by_route.items():
        if not docs_in_route:
            raise BusinessError(
                ErrorCode.SELF_CHECK_FAILED,
                f"路 {route} 没有任何候选命中",
                field_path=f"route={route}",
            )


def check_recomputable(result: FusionResult, tol: float = RECOMPUTE_TOL) -> None:
    """最终分数必须等于逐项加权贡献之和。"""
    doc_ids = [candidate.doc_id for candidate in result.candidates]
    if len(doc_ids) != len(set(doc_ids)):
        raise BusinessError(
            ErrorCode.SELF_CHECK_FAILED,
            f"查询 {result.query_id} 融合结果存在重复 doc_id",
            field_path=f"query={result.query_id}",
        )
    for candidate in result.candidates:
        recomputed = candidate.recompute_float()
        if abs(recomputed - candidate.final_score_float) > tol:
            raise BusinessError(
                ErrorCode.SELF_CHECK_FAILED,
                (
                    f"查询 {result.query_id} 候选 {candidate.doc_id} "
                    f"重算分 {recomputed} 与最终分 {candidate.final_score_float} 不一致"
                ),
                field_path=f"query={result.query_id},doc={candidate.doc_id}",
            )
        owner_routes = {item.route for item in candidate.contributions}
        if candidate.owner_route not in owner_routes:
            raise BusinessError(
                ErrorCode.SELF_CHECK_FAILED,
                f"候选 {candidate.doc_id} 的归属路 {candidate.owner_route} 无命中记录",
                field_path=f"doc={candidate.doc_id}",
            )


def check_invalidation_coverage(
    affected: set[str], recomputed: set[str], reused: set[str]
) -> None:
    """重算 ∪ 复用必须恰好覆盖受影响查询集合，且互不重叠。"""
    overlap = recomputed & reused
    if overlap:
        raise BusinessError(
            ErrorCode.SELF_CHECK_FAILED,
            f"重算与复用清单存在交集: {sorted(overlap)}",
            field_path="recompute_reuse_overlap",
        )
    if not affected:
        if recomputed:
            raise BusinessError(
                ErrorCode.SELF_CHECK_FAILED,
                "无受影响查询时不允许出现重算项",
                field_path="invalidation_coverage",
            )
        return
    if recomputed != affected:
        missing = sorted(affected - recomputed)
        extra = sorted(recomputed - affected)
        raise BusinessError(
            ErrorCode.SELF_CHECK_FAILED,
            "重算集合必须恰好等于受影响查询集合",
            field_path="invalidation_coverage",
            context={"missing": missing, "extra": extra},
        )
    if not reused.isdisjoint(affected):
        raise BusinessError(
            ErrorCode.SELF_CHECK_FAILED,
            "复用集合与受影响查询存在交集",
            field_path="invalidation_coverage",
            context={"overlap": sorted(reused & affected)},
        )


def self_check_summary(
    index: SearchIndex,
    route_results_by_query: dict[str, list[RouteResult]],
    fusion_results: dict[str, FusionResult],
) -> dict[str, Any]:
    """运行全部只读自检，返回可归档的检查摘要。"""
    for query_id, route_results in route_results_by_query.items():
        check_route_doc_sets(index, route_results)
    for result in fusion_results.values():
        check_recomputable(result)
    return {
        "status": "ok",
        "index_docs": len(index.doc_ids),
        "queries_checked": len(fusion_results),
        "checks": [
            "route_doc_sets_consistent",
            "final_score_recomputable",
            "candidate_doc_ids_unique",
            "owner_route_recorded",
        ],
    }
