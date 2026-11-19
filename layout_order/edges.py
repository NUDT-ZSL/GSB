"""候选后继边构造与确定性代价打分。

边来源（``edge_flags``）：

* ``region_flow``    跨区衔接（页眉→标题/正文、正文→页脚等）；
* ``vertical_adjacent`` 上下相邻（垂直区间无其他块且水平有重叠）；
* ``same_column``    同栏自上而下衔接；
* ``cross_column``   栏底→另一栏栏顶、通栏标题→各栏等跨栏边；
* ``fallback_chain`` 按 (ny1, nx1, block_id) 全局排序的兜底边，保证单链可行。

同一对 (src, dst) 去重时保留最小来源优先级并合并标志；最终按
(src block_id, dst block_id) 规范化排序，逐条编号，保证逐字节一致。
"""

from __future__ import annotations

from dataclasses import dataclass, asdict

from .config import CostWeights
from .errors import E_INCONSISTENT, E_RESOURCE_LIMIT, LayoutOrderError
from .geometry import BlockFeature, ROUND_DIGITS

REGION_FLOW = "region_flow"
VERTICAL_ADJACENT = "vertical_adjacent"
SAME_COLUMN = "same_column"
CROSS_COLUMN = "cross_column"
FALLBACK_CHAIN = "fallback_chain"

# 来源优先级：数字越小语义越强，去重时保留强者。
FLAG_PRIORITY = {
    REGION_FLOW: 0,
    VERTICAL_ADJACENT: 1,
    SAME_COLUMN: 2,
    CROSS_COLUMN: 3,
    FALLBACK_CHAIN: 4,
}


def _r(value: float) -> float:
    return round(float(value), ROUND_DIGITS)


@dataclass(frozen=True)
class Edge:
    edge_id: str
    src: str
    dst: str
    priority: int
    flags: tuple[str, ...]
    same_column: bool
    cross_region: bool
    upward: bool
    vertical_gap: float
    horizontal_overlap: float
    terms: dict
    total_cost: float

    def to_dict(self) -> dict:
        data = asdict(self)
        data["flags"] = list(self.flags)
        data["terms"] = {key: self.terms[key] for key in sorted(self.terms)}
        return data


@dataclass(frozen=True)
class CandidateGraph:
    edges: tuple[Edge, ...]
    successors: dict
    cross_column_ratio: float

    def cost_lookup(self) -> dict[tuple[str, str], float]:
        return {(edge.src, edge.dst): edge.total_cost for edge in self.edges}


def build_candidates(
    features: list[BlockFeature],
    weights: CostWeights,
    max_edges: int | None = None,
) -> CandidateGraph:
    """构造候选后继边并逐条打分。"""
    weights.validate()
    by_id = {feature.block_id: feature for feature in features}
    ids = tuple(feature.block_id for feature in features)
    if len(ids) != len(set(ids)):
        raise LayoutOrderError(
            E_INCONSISTENT,
            "候选边构造阶段发现重复 block_id",
            {"stage": "candidate_edges", "category": "duplicate_block_id"},
        )

    raw_edges: dict[tuple[str, str], dict] = {}

    def add(src: str, dst: str, flag: str) -> None:
        if src == dst:
            return
        key = (src, dst)
        priority = FLAG_PRIORITY[flag]
        if key not in raw_edges:
            raw_edges[key] = {"priority": priority, "flags": {flag}}
        else:
            entry = raw_edges[key]
            entry["priority"] = min(entry["priority"], priority)
            entry["flags"].add(flag)

    _add_region_flow(features, add)
    _add_vertical_adjacent(features, add)
    _add_same_column(features, add)
    _add_cross_column(features, add)
    _add_fallback_chain(features, add)

    if max_edges is not None and len(raw_edges) > max_edges:
        raise LayoutOrderError(
            E_RESOURCE_LIMIT,
            f"候选边总数 {len(raw_edges)} 超过上限 {max_edges}",
            {
                "stage": "candidate_edges",
                "category": "max_edges",
                "limit_kind": "max_edges",
                "observed": len(raw_edges),
                "limit": max_edges,
            },
        )

    ordered_keys = sorted(raw_edges, key=lambda pair: (pair[0], pair[1]))
    edges: list[Edge] = []
    cross_column_count = 0
    for index, key in enumerate(ordered_keys):
        src_id, dst_id = key
        src = by_id[src_id]
        dst = by_id[dst_id]
        entry = raw_edges[key]
        flags = tuple(sorted(entry["flags"], key=lambda flag: (FLAG_PRIORITY[flag], flag)))
        if CROSS_COLUMN in entry["flags"]:
            cross_column_count += 1
        edge = _score_edge(src, dst, entry["priority"], flags, index, weights)
        edges.append(edge)

    successors: dict[str, list[str]] = {block_id: [] for block_id in ids}
    for edge in edges:
        successors[edge.src].append(edge.dst)
    for dst_list in successors.values():
        dst_list.sort()

    ratio = _r(cross_column_count / len(edges)) if edges else 0.0
    return CandidateGraph(
        edges=tuple(edges),
        successors=dict(successors),
        cross_column_ratio=ratio,
    )


def _add_region_flow(features: list[BlockFeature], add) -> None:
    by_region: dict[str, list[BlockFeature]] = {}
    for feature in features:
        by_region.setdefault(feature.region, []).append(feature)

    def top(region: str) -> BlockFeature | None:
        members = by_region.get(region)
        if not members:
            return None
        return sorted(members, key=lambda item: (item.ny1, item.nx1, item.block_id))[0]

    def bottom(region: str) -> BlockFeature | None:
        members = by_region.get(region)
        if not members:
            return None
        return sorted(members, key=lambda item: (item.ny2, item.nx1, item.block_id))[-1]

    header_bottom = bottom("header")
    title_top = top("title")
    body_top = top("body")
    footer_top = top("footer")

    if header_bottom is not None:
        for target in (title_top, body_top):
            if target is not None:
                add(header_bottom.block_id, target.block_id, REGION_FLOW)
    title_bottom = bottom("title")
    if title_bottom is not None and body_top is not None:
        add(title_bottom.block_id, body_top.block_id, REGION_FLOW)
    body_bottom = bottom("body")
    if body_bottom is not None and footer_top is not None:
        add(body_bottom.block_id, footer_top.block_id, REGION_FLOW)
    if title_bottom is not None and footer_top is not None and body_bottom is None:
        add(title_bottom.block_id, footer_top.block_id, REGION_FLOW)


def _add_vertical_adjacent(features: list[BlockFeature], add) -> None:
    ordered = sorted(features, key=lambda item: (item.ny1, item.nx1, item.block_id))
    for index, upper in enumerate(ordered):
        for lower in ordered[index + 1 :]:
            if lower.ny1 < upper.ny2:
                continue
            overlap = _horizontal_overlap(upper, lower)
            if overlap <= 0:
                continue
            between = any(
                candidate is not upper
                and candidate is not lower
                and upper.ny2 <= candidate.ny1
                and candidate.ny2 <= lower.ny1
                for candidate in ordered
            )
            if not between:
                add(upper.block_id, lower.block_id, VERTICAL_ADJACENT)


def _add_same_column(features: list[BlockFeature], add) -> None:
    by_column: dict[int, list[BlockFeature]] = {}
    for feature in features:
        if feature.column >= 0:
            by_column.setdefault(feature.column, []).append(feature)
    for members in by_column.values():
        ordered = sorted(members, key=lambda item: (item.ny1, item.nx1, item.block_id))
        for upper, lower in zip(ordered, ordered[1:]):
            add(upper.block_id, lower.block_id, SAME_COLUMN)


def _add_cross_column(features: list[BlockFeature], add) -> None:
    by_column: dict[int, list[BlockFeature]] = {}
    for feature in features:
        if feature.column >= 0:
            by_column.setdefault(feature.column, []).append(feature)
    columns = sorted(by_column)
    column_bottom = {
        column: sorted(
            by_column[column], key=lambda item: (item.ny2, item.nx1, item.block_id)
        )[-1]
        for column in columns
    }
    column_top = {
        column: sorted(
            by_column[column], key=lambda item: (item.ny1, item.nx1, item.block_id)
        )[0]
        for column in columns
    }
    for left in columns:
        for right in columns:
            if left == right:
                continue
            # 只允许“左栏底→右栏顶”的顺读跨栏，避免提前跳栏破坏同栏保序。
            if right == left + 1:
                add(
                    column_bottom[left].block_id,
                    column_top[right].block_id,
                    CROSS_COLUMN,
                )

    spanning = [
        feature
        for feature in features
        if feature.is_spanning and feature.region != "footer"
    ]
    spanning_sorted = sorted(spanning, key=lambda item: (item.ny2, item.block_id))
    if spanning_sorted:
        last_spanning = spanning_sorted[-1]
        for column in columns:
            add(last_spanning.block_id, column_top[column].block_id, CROSS_COLUMN)

    footer_members = [feature for feature in features if feature.region == "footer"]
    if footer_members:
        footer_top = sorted(
            footer_members, key=lambda item: (item.ny1, item.nx1, item.block_id)
        )[0]
        for column in columns:
            add(column_bottom[column].block_id, footer_top.block_id, CROSS_COLUMN)


def _add_fallback_chain(features: list[BlockFeature], add) -> None:
    """全连接兜底：任意两个不同块之间至少存在一个方向的候选边。

    仅向下的边会被通栏标题/页眉页脚截断，贪心链间接龙可能需要向上回到另一栏
    栏顶；因此补一组反向兜底边（带 ``upward`` 惩罚），保证候选图始终存在
    哈密顿路径，且排序确定、规模为 O(n^2)。
    """
    ordered = sorted(features, key=lambda item: (item.ny1, item.nx1, item.block_id))
    for index, earlier in enumerate(ordered):
        for later in ordered[index + 1 :]:
            add(earlier.block_id, later.block_id, FALLBACK_CHAIN)
            add(later.block_id, earlier.block_id, FALLBACK_CHAIN)


def _horizontal_overlap(src: BlockFeature, dst: BlockFeature) -> float:
    return max(0.0, min(src.nx2, dst.nx2) - max(src.nx1, dst.nx1))


def _score_edge(
    src: BlockFeature,
    dst: BlockFeature,
    priority: int,
    flags: tuple[str, ...],
    index: int,
    weights: CostWeights,
) -> Edge:
    same_column = src.column >= 0 and src.column == dst.column
    cross_region = src.region != dst.region
    forward_cross_column = (
        src.column >= 0
        and dst.column >= 0
        and dst.column == src.column + 1
        and dst.ny1 < src.ny1
    )
    # 顺读跨栏（左栏底→右栏顶）天然伴随垂直回跳，列序距离已编码其代价，
    # 不再叠加向上惩罚；其余向上边仍受重罚。
    upward = dst.ny1 < src.ny1 and not forward_cross_column
    gap = _r(dst.ny1 - src.ny2)
    overlap = _r(min(1.0, max(0.0, _horizontal_overlap(src, dst))))
    column_severity = _column_severity(src, dst)

    type_severity = _type_severity(src.block_type, dst.block_type, weights)
    terms = {
        "type": _r(weights.w_type * type_severity),
        "column": _r(weights.w_column * column_severity),
        "gap": _r(weights.w_gap * max(gap, 0.0)),
        "overlap": _r(weights.w_overlap * (1.0 - overlap)),
        "upward": _r(weights.w_upward * (1.0 if upward else 0.0)),
    }
    total_cost = _r(sum(terms.values()))
    return Edge(
        edge_id=f"e{index:06d}",
        src=src.block_id,
        dst=dst.block_id,
        priority=priority,
        flags=flags,
        same_column=same_column,
        cross_region=cross_region,
        upward=upward,
        vertical_gap=gap,
        horizontal_overlap=overlap,
        terms=terms,
        total_cost=total_cost,
    )


def _type_severity(src_type: str, dst_type: str, weights: CostWeights) -> float:
    forbidden = weights.forbidden_transitions.get(src_type, [])
    if dst_type in forbidden:
        return weights.forbidden_penalty
    ranks = weights.type_ranks
    if src_type in ranks and dst_type in ranks:
        rank_diff = ranks[src_type] - ranks[dst_type]
        if rank_diff > 0:
            return weights.type_violation_penalty * rank_diff
    return 0.0


def _column_severity(src: BlockFeature, dst: BlockFeature) -> float:
    """列关系严重度：同栏/通栏为 0；跨栏按列序距离与回跳惩罚递增。

    顺读到右侧相邻栏为 1.0；向左回跳或一次跨越多栏按列号距离放大，
    为求解器提供确定性的“从左到右”列序信号。
    """
    if src.column >= 0 and src.column == dst.column:
        return 0.0
    if src.is_spanning or dst.is_spanning:
        return 0.0
    if src.column < 0 or dst.column < 0:
        return 1.0
    distance = abs(dst.column - src.column)
    if dst.column < src.column:
        return float(distance) + 1.0
    return float(distance)


def recompute_edge_cost(edge: Edge, features: list[BlockFeature], weights: CostWeights) -> float:
    """依据分项明细独立重算总代价，用于跨模块一致性自检。"""
    weights.validate()
    by_id = {feature.block_id: feature for feature in features}
    src = by_id[edge.src]
    dst = by_id[edge.dst]
    severity = _type_severity(src.block_type, dst.block_type, weights)
    expected_terms = {
        "type": _r(weights.w_type * severity),
        "column": _r(weights.w_column * _column_severity(src, dst)),
        "gap": _r(weights.w_gap * max(edge.vertical_gap, 0.0)),
        "overlap": _r(weights.w_overlap * (1.0 - edge.horizontal_overlap)),
        "upward": _r(weights.w_upward * (1.0 if edge.upward else 0.0)),
    }
    for key, value in expected_terms.items():
        if edge.terms.get(key) != value:
            raise LayoutOrderError(
                E_INCONSISTENT,
                f"边 {edge.edge_id} 的分项 {key} 重算不一致",
                {
                    "stage": "edge_recompute",
                    "category": "edge_terms_mismatch",
                    "edge_id": edge.edge_id,
                    "term": key,
                },
            )
    return _r(sum(expected_terms.values()))
