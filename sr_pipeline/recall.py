"""四路确定性召回打分。

每路召回返回 :class:`RouteHit` 列表，记录 doc_id、领域内原始分与
重算所需的明细项。所有排序同分都按 doc_id 升序打破平局，
浮点与定点路径共用本模块代码，差异只来自算子实现。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import PipelineConfig, RouteConfig
from .index import SearchIndex, cosine_dot
from .precision import Ops
from .samplegen import term_prefix


@dataclass
class RouteHit:
    """单路召回的单个候选命中。"""

    route: str
    doc_id: str
    raw: Any  # 领域内原始分（float 或定点整数）
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class RouteResult:
    """一路召回的完整输出。"""

    route: str
    hits: list[RouteHit]
    eligible_doc_ids: tuple[str, ...] = ()


def _tie_sorted(hits: list[RouteHit], ops: Ops) -> list[RouteHit]:
    """同分按 doc_id 升序的确定性排序。"""
    return sorted(
        hits,
        key=lambda hit: (-ops.to_float(hit.raw), hit.doc_id),
    )


def _cap(hits: list[RouteHit], cap: int) -> list[RouteHit]:
    return hits[:cap]


def inverted_recall(
    query: dict,
    index: SearchIndex,
    route_config: RouteConfig,
    ops: Ops,
) -> RouteResult:
    """词项倒排：命中词项的权重 / sqrt(doc_len) 求和。"""
    accumulator: dict[str, Any] = {}
    per_term: dict[str, dict[str, float]] = {}
    for term_id in dict.fromkeys(query["terms"]):
        for doc_id, contribution in index.inverted.get(term_id, ()):
            accumulator[doc_id] = ops.add(
                accumulator.get(doc_id, ops.from_float(0.0)), contribution
            )
            per_term.setdefault(doc_id, {})[str(term_id)] = ops.to_float(
                contribution
            )
    hits = [
        RouteHit(
            route=route_config.name,
            doc_id=doc_id,
            raw=raw,
            details={"matched_terms": per_term[doc_id]},
        )
        for doc_id, raw in accumulator.items()
    ]
    ordered = _tie_sorted(hits, ops)
    return RouteResult(
        route_config.name,
        _cap(ordered, route_config.candidate_cap),
        tuple(hit.doc_id for hit in ordered),
    )


def prefix_recall(
    query: dict,
    index: SearchIndex,
    route_config: RouteConfig,
    ops: Ops,
) -> RouteResult:
    """词项前缀：同一前缀桶内的文档命中词数求和，再除以 sqrt(doc_len)。"""
    prefix_len = route_config.prefix_len or 3
    buckets = {
        term_prefix(term_id, prefix_len)
        for term_id in query["terms"]
    }
    counts: dict[str, int] = {}
    for bucket in buckets:
        for doc_id, hit_count in index.prefix.get(bucket, ()):
            counts[doc_id] = counts.get(doc_id, 0) + int(ops.to_float(hit_count))
    hits: list[RouteHit] = []
    doc_lengths = index.doc_lengths
    for doc_id, hit_count in counts.items():
        sqrt_len = ops.sqrt(ops.from_float(float(doc_lengths[doc_id])))
        raw = ops.div(ops.from_float(float(hit_count)), sqrt_len)
        hits.append(
            RouteHit(
                route=route_config.name,
                doc_id=doc_id,
                raw=raw,
                details={
                    "prefix_buckets": sorted(buckets),
                    "hit_count": hit_count,
                    "doc_len": doc_lengths[doc_id],
                },
            )
        )
    ordered = _tie_sorted(hits, ops)
    return RouteResult(
        route_config.name,
        _cap(ordered, route_config.candidate_cap),
        tuple(hit.doc_id for hit in ordered),
    )


def category_hot_recall(
    query: dict,
    index: SearchIndex,
    route_config: RouteConfig,
    ops: Ops,
) -> RouteResult:
    """类别热门：召回与查询命中文档同类别的文档，赋予类别热度分。

    查询先经倒排确定“触达类别集合”，类别内全部文档成为候选，
    原始分即预计算的类别热度，保证同分平局规则可覆盖大量候选。
    """
    touched_categories: set[int] = set()
    for term_id in query["terms"]:
        for doc_id, _ in index.inverted.get(term_id, ()):
            touched_categories.add(index.doc_categories[doc_id])
    hits: list[RouteHit] = []
    for doc_id in index.doc_ids:
        category = index.doc_categories[doc_id]
        if category in touched_categories:
            raw = index.category_hotness[category]
            hits.append(
                RouteHit(
                    route=route_config.name,
                    doc_id=doc_id,
                    raw=raw,
                    details={"category": category},
                )
            )
    ordered = _tie_sorted(hits, ops)
    return RouteResult(
        route_config.name,
        _cap(ordered, route_config.candidate_cap),
        tuple(hit.doc_id for hit in ordered),
    )


def vector_ann_recall(
    query: dict,
    index: SearchIndex,
    route_config: RouteConfig,
    ops: Ops,
) -> RouteResult:
    """向量近似：归一化向量点积（余弦），全量扫描但顺序确定。"""
    query_vector = index.query_vectors[query["query_id"]]
    hits: list[RouteHit] = []
    for doc_id in index.doc_ids:
        raw = cosine_dot(ops, query_vector, index.doc_vectors[doc_id])
        hits.append(
            RouteHit(
                route=route_config.name,
                doc_id=doc_id,
                raw=raw,
                details={"metric": "cosine_dot"},
            )
        )
    ordered = _tie_sorted(hits, ops)
    return RouteResult(
        route_config.name,
        _cap(ordered, route_config.candidate_cap),
        tuple(hit.doc_id for hit in ordered),
    )


_ROUTE_FUNCS = {
    "inverted": inverted_recall,
    "prefix": prefix_recall,
    "category_hot": category_hot_recall,
    "vector_ann": vector_ann_recall,
}


def run_all_routes(
    query: dict,
    index: SearchIndex,
    config: PipelineConfig,
    ops: Ops,
) -> list[RouteResult]:
    """按融合 route_order 依次跑全部启用路，输出顺序即配置顺序。"""
    results: list[RouteResult] = []
    for route_config in config.enabled_routes():
        func = _ROUTE_FUNCS[route_config.name]
        results.append(func(query, index, route_config, ops))
    return results
