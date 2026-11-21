"""Exact single-chain reading-order solving over candidate successor edges."""

from __future__ import annotations

from typing import Any

from .canonical import round_float
from .errors import LayoutError


def _block_ids(normalized_page: dict[str, Any]) -> list[int]:
    return [block["block_id"] for block in normalized_page["blocks"]]


def _adjacency(
    normalized_page: dict[str, Any], scored_edges: list[dict[str, Any]]
) -> dict[int, list[tuple[int, float, dict[str, Any]]]]:
    ids = set(_block_ids(normalized_page))
    adjacency: dict[int, list[tuple[int, float, dict[str, Any]]]] = {
        block_id: [] for block_id in ids
    }
    seen: set[tuple[int, int]] = set()
    for edge in scored_edges:
        source = edge["source_block_id"]
        target = edge["target_block_id"]
        if source not in ids or target not in ids:
            raise LayoutError(
                "E_ORDER_INCONSISTENT",
                "costed edge references an unknown block",
                block_id=source,
                page_id=normalized_page.get("page_id"),
            )
        if (source, target) in seen:
            raise LayoutError(
                "E_ORDER_INCONSISTENT",
                "duplicate costed edge",
                block_id=source,
                page_id=normalized_page.get("page_id"),
                details={"target_block_id": target},
            )
        seen.add((source, target))
        adjacency[source].append((target, edge["total_cost"], edge))
    for source in adjacency:
        adjacency[source].sort(key=lambda item: (item[1], item[0]))
    return adjacency


def _first_graph_violation(
    normalized_page: dict[str, Any],
    adjacency: dict[int, list[tuple[int, float, dict[str, Any]]]],
) -> int:
    ids = _block_ids(normalized_page)
    incoming = {block_id: 0 for block_id in ids}
    for source in ids:
        for target, _, _ in adjacency.get(source, []):
            incoming[target] = incoming.get(target, 0) + 1
    for block_id in sorted(ids):
        if not adjacency.get(block_id):
            return block_id
        if incoming.get(block_id, 0) == 0:
            return block_id
    return min(ids)


def solve_order(
    normalized_page: dict[str, Any],
    scored_edges: list[dict[str, Any]],
    limits: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Solve the minimum-cost Hamiltonian path exactly with Held-Karp DP.

    The objective is only the declared sum of adjacent-edge cost components.
    Ties are resolved by lexicographic block-id sequence.
    """

    page_id = normalized_page.get("page_id")
    block_ids = sorted(_block_ids(normalized_page))
    if len(set(block_ids)) != len(block_ids):
        raise LayoutError(
            "E_ORDER_INCONSISTENT",
            "duplicate block ids in page",
            block_id=block_ids[0],
            page_id=page_id,
        )
    count = len(block_ids)
    if count == 0:
        raise LayoutError(
            "E_ORDER_INCONSISTENT",
            "page has no blocks",
            page_id=page_id,
        )
    if count == 1:
        return {
            "page_id": page_id,
            "sequence": block_ids,
            "total_cost": 0.0,
            "edges": [],
            "tie_break": "lexicographic_block_id_sequence",
        }
    if limits and count > int(limits.get("max_blocks_per_page", count)):
        raise LayoutError(
            "E_RESOURCE_LIMIT",
            "solver page block count exceeds configured limit",
            field="limits.max_blocks_per_page",
            page_id=page_id,
            details={"block_count": count},
        )
    adjacency = _adjacency(normalized_page, scored_edges)
    index = {block_id: pos for pos, block_id in enumerate(block_ids)}
    full_mask = (1 << count) - 1
    # (mask,last_index) -> (cost, index_path); equal-cost paths keep the
    # lexicographically smallest block-id prefix.
    dp: dict[tuple[int, int], tuple[float, tuple[int, ...]]] = {}
    for position in range(count):
        dp[(1 << position, position)] = (0.0, (position,))
    for mask in range(1, full_mask + 1):
        for last in range(count):
            state = dp.get((mask, last))
            if state is None:
                continue
            cost, path = state
            for target_id, edge_cost, _ in adjacency[block_ids[last]]:
                target = index[target_id]
                bit = 1 << target
                if mask & bit:
                    continue
                new_mask = mask | bit
                new_cost = round_float(cost + edge_cost, 12)
                new_path = path + (target,)
                key = (new_mask, target)
                previous = dp.get(key)
                if previous is None:
                    dp[key] = (new_cost, new_path)
                elif new_cost < previous[0]:
                    dp[key] = (new_cost, new_path)
                elif new_cost == previous[0] and new_path < previous[1]:
                    dp[key] = (new_cost, new_path)
    candidates = []
    for last in range(count):
        state = dp.get((full_mask, last))
        if state is None:
            continue
        path = tuple(block_ids[position] for position in state[1])
        candidates.append((state[0], path))
    if not candidates:
        raise LayoutError(
            "E_ORDER_INCONSISTENT",
            "candidate graph has no single-chain Hamiltonian path",
            block_id=_first_graph_violation(normalized_page, adjacency),
            page_id=page_id,
            details={"block_count": count},
        )
    candidates.sort(key=lambda item: (item[0], item[1]))
    total_cost, sequence = candidates[0]
    edge_lookup = {
        (edge["source_block_id"], edge["target_block_id"]): edge
        for edge in scored_edges
    }
    chosen_edges = []
    for source, target in zip(sequence, sequence[1:]):
        edge = edge_lookup.get((source, target))
        if edge is None:
            raise LayoutError(
                "E_ORDER_INCONSISTENT",
                "solver chose an edge absent from candidate cost details",
                block_id=source,
                page_id=page_id,
                details={"target_block_id": target},
            )
        chosen_edges.append(edge)
    result = {
        "page_id": page_id,
        "sequence": sequence,
        "total_cost": round_float(total_cost, 8),
        "edges": chosen_edges,
        "tie_break": "lexicographic_block_id_sequence",
    }
    validate_order(
        normalized_page,
        sequence,
        scored_edges,
        expected_total=result["total_cost"],
        recovered_edges=chosen_edges,
    )
    return result


def validate_order(
    normalized_page: dict[str, Any],
    sequence: list[int],
    scored_edges: list[dict[str, Any]],
    *,
    expected_total: float | None = None,
    recovered_edges: list[dict[str, Any]] | None = None,
    tolerance: float = 1e-8,
) -> float:
    page_id = normalized_page.get("page_id")
    block_ids = _block_ids(normalized_page)
    expected_set = set(block_ids)
    if len(sequence) != len(block_ids):
        raise LayoutError(
            "E_ORDER_INCONSISTENT",
            "reading order does not cover every block exactly once",
            block_id=min(sequence, default=min(block_ids)),
            page_id=page_id,
            details={"expected_count": len(block_ids), "actual_count": len(sequence)},
        )
    seen: set[int] = set()
    for position, block_id in enumerate(sequence):
        if block_id not in expected_set:
            raise LayoutError(
                "E_ORDER_INCONSISTENT",
                "reading order contains an unknown block",
                block_id=block_id,
                page_id=page_id,
                details={"position": position},
            )
        if block_id in seen:
            raise LayoutError(
                "E_ORDER_INCONSISTENT",
                "reading order contains a duplicate block",
                block_id=block_id,
                page_id=page_id,
                details={"position": position},
            )
        seen.add(block_id)
    missing = sorted(expected_set - seen)
    if missing:
        raise LayoutError(
            "E_ORDER_INCONSISTENT",
            "reading order omits a block",
            block_id=missing[0],
            page_id=page_id,
        )
    lookup = {
        (edge["source_block_id"], edge["target_block_id"]): edge
        for edge in scored_edges
    }
    total = 0.0
    used_edges: list[dict[str, Any]] = []
    for source, target in zip(sequence, sequence[1:]):
        edge = lookup.get((source, target))
        if edge is None:
            raise LayoutError(
                "E_ORDER_INCONSISTENT",
                "consecutive reading-order pair has no candidate edge",
                block_id=source,
                page_id=page_id,
                details={"target_block_id": target},
            )
        detail_sum = round_float(
            sum(
                float(edge["components"][name])
                for name in sorted(edge["components"])
            ),
            8,
        )
        if abs(detail_sum - float(edge["total_cost"])) > tolerance:
            raise LayoutError(
                "E_ORDER_INCONSISTENT",
                "edge cost cannot be reconstructed from details",
                block_id=source,
                page_id=page_id,
                details={"target_block_id": target},
            )
        total += float(edge["total_cost"])
        used_edges.append(edge)
    total = round_float(total, 8)
    if expected_total is not None and abs(total - float(expected_total)) > tolerance:
        raise LayoutError(
            "E_ORDER_INCONSISTENT",
            "reading-order total cost differs from edge-detail sum",
            block_id=sequence[0],
            page_id=page_id,
            details={"expected_total": expected_total, "recomputed_total": total},
        )
    if recovered_edges is not None:
        if len(recovered_edges) != len(used_edges):
            raise LayoutError(
                "E_ORDER_INCONSISTENT",
                "recovered edge list length is inconsistent",
                block_id=sequence[0],
                page_id=page_id,
            )
        for got, want in zip(recovered_edges, used_edges):
            if (
                got["source_block_id"] != want["source_block_id"]
                or got["target_block_id"] != want["target_block_id"]
            ):
                raise LayoutError(
                    "E_ORDER_INCONSISTENT",
                    "recovered edge chain is inconsistent",
                    block_id=want["source_block_id"],
                    page_id=page_id,
                )
    return total


def rule_hits(
    normalized_page: dict[str, Any], chosen_edges: list[dict[str, Any]]
) -> dict[str, Any]:
    component_sums = {
        name: 0.0
        for name in (
            "type_order_violation",
            "column_transition",
            "vertical_gap",
            "horizontal_misalignment",
            "cross_zone",
        )
    }
    counts = {
        "same_column_edges": 0,
        "cross_column_edges": 0,
        "cross_zone_edges": 0,
        "vertical_adjacent_edges": 0,
        "backward_edges": 0,
        "type_order_violations": 0,
    }
    for edge in chosen_edges:
        for name, value in edge["components"].items():
            component_sums[name] = round_float(
                component_sums.get(name, 0.0) + float(value), 8
            )
        features = edge["features"]
        counts["same_column_edges"] += int(bool(features["same_column"]))
        counts["cross_column_edges"] += int(bool(features["cross_column"]))
        counts["cross_zone_edges"] += int(bool(features["cross_zone"]))
        counts["vertical_adjacent_edges"] += int(bool(features["vertical_adjacent"]))
        counts["backward_edges"] += int(bool(features["backward"]))
        counts["type_order_violations"] += int(
            float(edge["components"]["type_order_violation"]) > 0
        )
    return {
        "edge_count": len(chosen_edges),
        "counts": counts,
        "component_sums": component_sums,
        "page_id": normalized_page.get("page_id"),
    }


def deviation_spectrum(
    sequence: list[int], true_sequence: list[int]
) -> dict[str, Any]:
    position = {block_id: index for index, block_id in enumerate(sequence)}
    spectrum: dict[str, int] = {}
    deviations: list[int] = []
    for true_index, block_id in enumerate(true_sequence):
        delta = position[block_id] - true_index
        deviations.append(delta)
        key = str(delta)
        spectrum[key] = spectrum.get(key, 0) + 1
    abs_values = [abs(value) for value in deviations]
    return {
        "signed": deviations,
        "spectrum": dict(sorted(spectrum.items(), key=lambda item: int(item[0]))),
        "mean_abs": round_float(
            sum(abs_values) / len(abs_values) if abs_values else 0.0, 8
        ),
        "max_abs": max(abs_values, default=0),
        "exact_match": deviations == [0] * len(deviations),
    }


def compute_metrics(
    normalized_page: dict[str, Any],
    page_with_true_order: dict[str, Any],
    sequence: list[int],
    total_cost: float,
    chosen_edges: list[dict[str, Any]],
) -> dict[str, Any]:
    true_pairs = [
        (block["block_id"], block.get("reading_order_true"))
        for block in page_with_true_order.get("blocks", [])
    ]
    if any(order is None for _, order in true_pairs):
        true_sequence: list[int] = []
        spectrum = None
        inversions = None
    else:
        true_sequence = [
            block_id for block_id, _ in sorted(true_pairs, key=lambda item: item[1])
        ]
        spectrum = deviation_spectrum(sequence, true_sequence)
        rank = {block_id: index for index, block_id in enumerate(true_sequence)}
        inversions = sum(
            1
            for left in range(len(sequence))
            for right in range(left + 1, len(sequence))
            if rank[sequence[left]] > rank[sequence[right]]
        )
    return {
        "page_id": normalized_page.get("page_id"),
        "block_count": len(sequence),
        "total_cost": round_float(total_cost, 8),
        "total_cost_recomputed": round_float(
            sum(float(edge["total_cost"]) for edge in chosen_edges), 8
        ),
        "reading_order_true": true_sequence,
        "position_deviation": spectrum,
        "pairwise_inversions": inversions,
        "exact_order_match": None if spectrum is None else spectrum["exact_match"],
        "rule_hits": rule_hits(normalized_page, chosen_edges),
    }

