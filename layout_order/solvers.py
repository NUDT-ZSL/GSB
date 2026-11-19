"""受版面约束的全局顺序求解。

三条路径都在同一候选图上求解，且输出同一结构，保证可互相核对：

* ``exact``  ：Held-Karp 动态规划求最小代价哈密顿路径（每块恰有一个前驱、
  一个后继，且构成覆盖全部块的单一链路）；
* ``banded`` ：按垂直带切分，带内精确求解，带间按带序与列序拼接；
* ``greedy``：确定性最近后继贪心，卡住时按 (ny1, nx1, block_id) 续接。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .edges import CROSS_COLUMN, CandidateGraph
from .errors import E_ORDER_INCONSISTENT, LayoutOrderError
from .geometry import BlockFeature

# 分带：相邻带垂直中心间距（归一化）阈值与带内最大块数。
BAND_CENTER_GAP = 0.18


@dataclass
class SolveResult:
    path: str
    order: list
    total_cost: float
    comparisons: int
    resident_bytes: int
    bands: list = field(default_factory=list)
    column_split_events: int = 0
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "order": list(self.order),
            "total_cost": self.total_cost,
            "comparisons": self.comparisons,
            "resident_bytes": self.resident_bytes,
            "bands": [list(band) for band in self.bands],
            "column_split_events": self.column_split_events,
            "detail": dict(self.detail),
        }


def estimate_exact(n: int, edges: int) -> dict:
    """Held-Karp 的比较次数与驻留规模估计（确定性，不实际分配内存）。"""
    states = n * (2 ** n) if n >= 1 else 0
    return {
        "estimated_comparisons": edges * (2 ** max(n - 1, 0)),
        "estimated_states": states,
        "estimated_resident_bytes": states * 16,
    }


def estimate_banded(band_sizes: list[int], edges: int) -> dict:
    comparisons = sum(edges * (2 ** max(size - 1, 0)) for size in band_sizes)
    states = sum(size * (2 ** size) for size in band_sizes)
    return {
        "estimated_comparisons": comparisons,
        "estimated_states": states,
        "estimated_resident_bytes": states * 16,
    }


def estimate_greedy(n: int, edges: int) -> dict:
    return {
        "estimated_comparisons": edges,
        "estimated_states": n,
        "estimated_resident_bytes": n * 128,
    }


def solve_exact(
    features: list[BlockFeature], graph: CandidateGraph
) -> SolveResult:
    block_ids = [feature.block_id for feature in features]
    n = len(block_ids)
    if n == 1:
        return SolveResult(
            path="exact",
            order=list(block_ids),
            total_cost=0.0,
            comparisons=0,
            resident_bytes=128,
            detail={"dp_states": 1},
        )

    index_of = {block_id: idx for idx, block_id in enumerate(block_ids)}
    adjacency: list[dict[int, float]] = [dict() for _ in range(n)]
    for edge in graph.edges:
        adjacency[index_of[edge.src]][index_of[edge.dst]] = edge.total_cost

    full_mask = (1 << n) - 1
    # dp[(mask, end)] = (cost, prev)
    dp: dict[tuple[int, int], tuple[float, int]] = {}
    comparisons = 0
    for start in range(n):
        dp[(1 << start, start)] = (0.0, -1)

    for mask in range(1, full_mask + 1):
        for end in range(n):
            state = (mask, end)
            if state not in dp:
                continue
            current_cost, _ = dp[state]
            for nxt, edge_cost in adjacency[end].items():
                if mask & (1 << nxt):
                    continue
                comparisons += 1
                new_mask = mask | (1 << nxt)
                candidate = current_cost + edge_cost
                previous = dp.get((new_mask, nxt))
                if previous is None or candidate < previous[0]:
                    dp[(new_mask, nxt)] = (candidate, end)

    best_cost: float | None = None
    best_end = -1
    for end in range(n):
        state = (full_mask, end)
        if state not in dp:
            continue
        cost, _ = dp[state]
        if best_cost is None or cost < best_cost:
            best_cost = cost
            best_end = end

    if best_cost is None:
        raise LayoutOrderError(
            E_ORDER_INCONSISTENT,
            "精确求解未找到覆盖全部块的单一链路",
            {"category": "no_hamiltonian_path", "missing": sorted(block_ids)},
        )

    order_indices: list[int] = []
    mask = full_mask
    end = best_end
    while end != -1:
        order_indices.append(end)
        _, predecessor = dp[(mask, end)]
        mask ^= 1 << end
        end = predecessor
    order_indices.reverse()
    order = [block_ids[idx] for idx in order_indices]
    validate_chain(order, block_ids, graph, round_cost=best_cost)
    return SolveResult(
        path="exact",
        order=order,
        total_cost=round(best_cost, 9),
        comparisons=comparisons,
        resident_bytes=len(dp) * 16,
        detail={"dp_states": len(dp)},
    )


def partition_bands(
    features: list[BlockFeature], max_band_size: int
) -> list[list[BlockFeature]]:
    """保序切带：页眉/页脚各自成带，正文先按栏、栏内再垂直切分。

    水平切带会把同一栏的上下块切到不同带，破坏同栏保序；这里改为每个物理栏
    独立按 (中心间距, 带容量) 切带，带序为 页眉 -> 左栏各带 -> 右栏各带 ->
    页脚，天然匹配“栏内自上而下、栏间自左向右”的阅读约束。
    """
    bands: list[list[BlockFeature]] = []
    header = sorted(
        [feature for feature in features if feature.region == "header"],
        key=lambda item: (item.ny1, item.block_id),
    )
    footer = sorted(
        [feature for feature in features if feature.region == "footer"],
        key=lambda item: (item.ny1, item.block_id),
    )
    if header:
        bands.append(header)

    # 通栏标题按垂直序单独成带；通栏图注属于正文流，归入后续正文带序列。
    spanning = sorted(
        [
            feature
            for feature in features
            if feature.region == "title" and feature.is_spanning
        ],
        key=lambda item: (item.ny1, item.nx1, item.block_id),
    )
    for feature in spanning:
        bands.append([feature])

    spanning_body = sorted(
        [
            feature
            for feature in features
            if feature.region == "body" and feature.is_spanning
        ],
        key=lambda item: (item.ny1, item.nx1, item.block_id),
    )

    by_column: dict[int, list[BlockFeature]] = {}
    for feature in features:
        if feature.column >= 0:
            by_column.setdefault(feature.column, []).append(feature)
    for column in sorted(by_column):
        members = sorted(
            by_column[column],
            key=lambda item: (item.ny1, item.nx1, item.block_id),
        )
        current: list[BlockFeature] = []
        for feature in members:
            if not current:
                current = [feature]
                continue
            gap = feature.cy - current[-1].cy
            if len(current) >= max_band_size or gap > BAND_CENTER_GAP:
                bands.append(current)
                current = [feature]
            else:
                current.append(feature)
        if current:
            bands.append(current)

    # 通栏正文块（如图注）按垂直位置插入带序列，保证落在所有栏内容之后、页脚之前。
    for feature in spanning_body:
        bands.append([feature])

    if footer:
        bands.append(footer)
    return bands


def solve_banded(
    features: list[BlockFeature],
    graph: CandidateGraph,
    max_band_size: int,
) -> SolveResult:
    block_ids = [feature.block_id for feature in features]
    by_id = {feature.block_id: feature for feature in features}
    bands = partition_bands(features, max_band_size)
    cost_lookup = graph.cost_lookup()

    order: list[str] = []
    comparisons = 0
    resident = 0
    band_orders: list[list[str]] = []
    split_events = 0
    for band in bands:
        band_ids = [feature.block_id for feature in band]
        if len(band_ids) == 1:
            band_order = list(band_ids)
            comparisons += 0
            resident += 128
        else:
            sub_graph = _induced_graph(band, graph)
            sub_result = solve_exact(band, sub_graph)
            band_order = sub_result.order
            comparisons += sub_result.comparisons
            resident += sub_result.resident_bytes
        band_orders.append(band_order)
        order.extend(band_order)

    for previous_band, current_band in zip(band_orders, band_orders[1:]):
        previous_columns = {by_id[block_id].column for block_id in previous_band}
        current_columns = {by_id[block_id].column for block_id in current_band}
        if previous_columns & current_columns:
            split_events += 1
        comparisons += 1

    validate_chain(order, block_ids, graph, round_cost=None)
    total_cost = _chain_cost(order, cost_lookup)
    return SolveResult(
        path="banded",
        order=order,
        total_cost=total_cost,
        comparisons=comparisons,
        resident_bytes=resident,
        bands=[[feature.block_id for feature in band] for band in bands],
        column_split_events=split_events,
        detail={"band_sizes": [len(band) for band in bands]},
    )


def solve_greedy(
    features: list[BlockFeature], graph: CandidateGraph
) -> SolveResult:
    ordered_features = sorted(
        features, key=lambda item: (item.ny1, item.nx1, item.block_id)
    )
    block_ids = [feature.block_id for feature in ordered_features]
    by_id = {feature.block_id: feature for feature in features}
    adjacency: dict[str, list[tuple[str, float]]] = {}
    for edge in graph.edges:
        adjacency.setdefault(edge.src, []).append((edge.dst, edge.total_cost))
    for targets in adjacency.values():
        targets.sort(key=lambda pair: (pair[1], pair[0]))

    remaining = set(block_ids)
    segments: list[list[str]] = []
    comparisons = 0
    fallback_events = 0
    while remaining:
        start = min(
            remaining,
            key=lambda block_id: (
                by_id[block_id].ny1,
                by_id[block_id].nx1,
                block_id,
            ),
        )
        segment = [start]
        remaining.remove(start)
        current = start
        while remaining:
            chosen = None
            for candidate, cost in adjacency.get(current, []):
                comparisons += 1
                if candidate in remaining:
                    chosen = candidate
                    break
            if chosen is None:
                break
            segment.append(chosen)
            remaining.remove(chosen)
            current = chosen
        segments.append(segment)

    # 链间接龙：每段末端在候选边集合内选择通向另一段首块的最低代价边；
    # 全连接兜底边保证该边必然存在。
    order = list(segments[0])
    leftover = segments[1:]
    while leftover:
        tail = order[-1]
        best_index = -1
        best_cost: float | None = None
        for index, segment in enumerate(leftover):
            comparisons += 1
            head = segment[0]
            if (tail, head) not in graph.cost_lookup():
                continue
            cost = graph.cost_lookup()[(tail, head)]
            if best_cost is None or cost < best_cost or (
                cost == best_cost and head < leftover[best_index][0]
            ):
                best_cost = cost
                best_index = index
        if best_index < 0:
            raise LayoutOrderError(
                E_ORDER_INCONSISTENT,
                f"贪心链间接龙失败，尾块 {tail} 无候选边通向剩余链",
                {"category": "greedy_stitch_failed", "block_id": tail},
            )
        chosen_segment = leftover.pop(best_index)
        fallback_events += 1
        order.extend(chosen_segment)

    validate_chain(order, block_ids, graph, round_cost=None)
    total_cost = _chain_cost(order, graph.cost_lookup())
    return SolveResult(
        path="greedy",
        order=order,
        total_cost=total_cost,
        comparisons=comparisons,
        resident_bytes=len(block_ids) * 128,
        detail={"fallback_events": fallback_events},
    )


def _induced_graph(
    band: list[BlockFeature], graph: CandidateGraph
) -> CandidateGraph:
    allowed = {feature.block_id for feature in band}
    edges = tuple(edge for edge in graph.edges if edge.src in allowed and edge.dst in allowed)
    successors: dict[str, list[str]] = {block_id: [] for block_id in allowed}
    cross_count = 0
    for edge in edges:
        successors[edge.src].append(edge.dst)
        if CROSS_COLUMN in edge.flags:
            cross_count += 1
    for targets in successors.values():
        targets.sort()
    ratio = round(cross_count / len(edges), 9) if edges else 0.0
    return CandidateGraph(edges=edges, successors=successors, cross_column_ratio=ratio)


def _chain_cost(order: list[str], cost_lookup: dict[tuple[str, str], float]) -> float:
    total = 0.0
    for src, dst in zip(order, order[1:]):
        if (src, dst) not in cost_lookup:
            raise LayoutOrderError(
                E_ORDER_INCONSISTENT,
                f"顺序中的相邻对 ({src} -> {dst}) 不在候选边集合内",
                {
                    "category": "edge_not_in_candidates",
                    "src": src,
                    "dst": dst,
                },
            )
        total += cost_lookup[(src, dst)]
    return round(total, 9)


def validate_chain(
    order: list[str],
    expected_ids: list[str],
    graph: CandidateGraph,
    round_cost: float | None,
) -> None:
    """单链 / 全覆盖 / 候选边归属 / 代价一致性校验。"""
    expected_set = set(expected_ids)
    actual_set = set(order)
    missing = sorted(expected_set - actual_set)
    duplicates = sorted(
        {block_id for block_id in order if order.count(block_id) > 1}
    )
    extra = sorted(actual_set - expected_set)
    if missing or duplicates or extra or len(order) != len(expected_ids):
        raise LayoutOrderError(
            E_ORDER_INCONSISTENT,
            "求解结果未构成覆盖全部块的单一链路",
            {
                "category": "chain_coverage",
                "missing": missing,
                "duplicates": duplicates,
                "extra": extra,
            },
        )
    edge_pairs = {(edge.src, edge.dst) for edge in graph.edges}
    for src, dst in zip(order, order[1:]):
        if (src, dst) not in edge_pairs:
            raise LayoutOrderError(
                E_ORDER_INCONSISTENT,
                f"顺序边 ({src} -> {dst}) 不属于候选边集合",
                {
                    "category": "edge_not_in_candidates",
                    "src": src,
                    "dst": dst,
                },
            )
    if round_cost is not None:
        recomputed = _chain_cost(order, graph.cost_lookup())
        if abs(recomputed - round_cost) > 1e-9:
            raise LayoutOrderError(
                E_ORDER_INCONSISTENT,
                "求解总代价与逐边重算代价不一致",
                {
                    "category": "cost_mismatch",
                    "solver_cost": round_cost,
                    "recomputed_cost": recomputed,
                },
            )
