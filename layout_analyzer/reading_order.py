from __future__ import annotations

from collections import defaultdict
from typing import Any

from .model import Block, GraphEdge, OrderConflict, OrderResult


def build_reading_order(blocks: list[Block], reference_width: int) -> OrderResult:
    by_uid = {block.uid: block for block in blocks}
    bodies = sorted(
        [block for block in blocks if block.block_type == "body_column"],
        key=lambda block: (-block.x_center, block.uid),
    )
    edges: list[GraphEdge] = []
    conflicts: list[OrderConflict] = []
    unresolved: set[str] = set()

    if not bodies:
        conflicts.append(
            OrderConflict(
                kind="missing_main_chain",
                involved=[],
                message="no body column was classified; vertical right-to-left chain is undefined",
            )
        )
        unresolved.update(block.uid for block in blocks)
    else:
        tie_tolerance = max(1.5, reference_width * 0.0012)
        for left, right in zip(bodies, bodies[1:]):
            if abs(left.x_center - right.x_center) <= tie_tolerance:
                conflict = OrderConflict(
                    kind="indistinct_column_x_order",
                    involved=[left.uid, right.uid],
                    message="two body columns have indistinguishable horizontal centers",
                    candidate_edges=[
                        GraphEdge(left.uid, right.uid, "vertical_columns_right_to_left"),
                        GraphEdge(right.uid, left.uid, "vertical_columns_right_to_left"),
                    ],
                )
                conflicts.append(conflict)
                unresolved.update([left.uid, right.uid])
                continue
            edges.append(
                GraphEdge(
                    source=left.uid,
                    target=right.uid,
                    rule="vertical_columns_right_to_left",
                    evidence={
                        "source_x_center": round(left.x_center, 3),
                        "target_x_center": round(right.x_center, 3),
                        "horizontal_delta": round(left.x_center - right.x_center, 3),
                    },
                )
            )

    attachment_groups: dict[str, list[tuple[Block, float, str]]] = defaultdict(list)
    annotation_tie_tolerance = max(2.0, reference_width * 0.0028)
    object_tie_tolerance = max(3.0, reference_width * 0.0035)

    for block in blocks:
        if block.block_type != "inline_annotation":
            continue
        scores, candidates = _horizontal_anchor_scores(block, bodies)
        if not candidates:
            conflict = OrderConflict(
                kind="orphan_annotation",
                involved=[block.uid],
                message="annotation has no body-column candidate",
            )
            conflicts.append(conflict)
            unresolved.add(block.uid)
            continue
        best = candidates[0]
        tied = [
            item
            for item in candidates
            if abs(item[1] - best[1]) <= annotation_tie_tolerance
        ]
        if len(tied) > 1:
            candidate_edges = [
                GraphEdge(
                    body.uid,
                    block.uid,
                    "annotation_attaches_to_max_horizontal_relation",
                    {
                        "x_overlap_px": round(overlap, 3),
                        "horizontal_gap_px": round(gap, 3),
                        "anchor_score": round(score, 3),
                        "tie_tolerance_px": round(annotation_tie_tolerance, 3),
                    },
                )
                for body, score, overlap, gap in tied
            ]
            conflicts.append(
                OrderConflict(
                    kind="ambiguous_annotation_anchor",
                    involved=[block.uid] + [body.uid for body, _, _, _ in tied],
                    message=(
                        "annotation horizontal relation is tied across multiple body columns; "
                        "no attachment edge is inserted"
                    ),
                    candidate_edges=candidate_edges,
                )
            )
            unresolved.add(block.uid)
            continue
        body, score, overlap, gap = best
        attachment_groups[body.uid].append(
            (
                block,
                overlap,
                "annotation_attaches_to_max_horizontal_relation",
            )
        )
        edges.append(
            GraphEdge(
                source=body.uid,
                target=block.uid,
                rule="annotation_attaches_to_max_horizontal_relation",
                evidence={
                    "x_overlap_px": round(overlap, 3),
                    "horizontal_gap_px": round(gap, 3),
                    "anchor_score": round(score, 3),
                    "anchor_x_center": round(body.x_center, 3),
                },
            )
        )

    for block in blocks:
        if block.block_type not in {"illustration", "seal"}:
            continue
        candidates = _nearest_body_candidates(block, bodies)
        if not candidates:
            conflicts.append(
                OrderConflict(
                    kind="orphan_non_text_object",
                    involved=[block.uid],
                    message="illustration/seal has no nearby body column",
                )
            )
            unresolved.add(block.uid)
            continue
        best = candidates[0]
        tied = [item for item in candidates if abs(item[1] - best[1]) <= object_tie_tolerance]
        if len(tied) > 1:
            conflicts.append(
                OrderConflict(
                    kind="ambiguous_object_anchor",
                    involved=[block.uid] + [body.uid for body, _, _ in tied],
                    message="illustration/seal is equidistant from multiple body columns",
                    candidate_edges=[
                        GraphEdge(
                            body.uid,
                            block.uid,
                            "figure_or_seal_after_nearest_body_column",
                            {
                                "horizontal_distance_px": round(distance, 3),
                                "tie_tolerance_px": round(object_tie_tolerance, 3),
                            },
                        )
                        for body, distance, _ in tied
                    ],
                )
            )
            unresolved.add(block.uid)
            continue
        body, distance, overlap = best
        attachment_groups[body.uid].append(
            (
                block,
                overlap,
                "figure_or_seal_after_nearest_body_column",
            )
        )
        edges.append(
            GraphEdge(
                source=body.uid,
                target=block.uid,
                rule="figure_or_seal_after_nearest_body_column",
                evidence={
                    "horizontal_distance_px": round(distance, 3),
                    "x_overlap_px": round(overlap, 3),
                    "anchor_x_center": round(body.x_center, 3),
                },
            )
        )

    for anchor_uid, attached in attachment_groups.items():
        ordered = sorted(attached, key=lambda item: (item[0].y_center, -item[0].x_center, item[0].uid))
        for previous, current in zip(ordered, ordered[1:]):
            edges.append(
                GraphEdge(
                    source=previous[0].uid,
                    target=current[0].uid,
                    rule="attachment_top_to_bottom_after_anchor_column",
                    evidence={
                        "anchor": anchor_uid,
                        "previous_y_center": round(previous[0].y_center, 3),
                        "current_y_center": round(current[0].y_center, 3),
                    },
                )
            )
        anchor_index = next(
            (index for index, body in enumerate(bodies) if body.uid == anchor_uid),
            None,
        )
        if anchor_index is not None and anchor_index + 1 < len(bodies):
            next_body = bodies[anchor_index + 1]
            edges.append(
                GraphEdge(
                    source=ordered[-1][0].uid,
                    target=next_body.uid,
                    rule="attachments_precede_next_main_column",
                    evidence={
                        "anchor": anchor_uid,
                        "next_body": next_body.uid,
                        "attachment_count": len(ordered),
                    },
                )
            )

    headers = [block for block in blocks if block.block_type == "header_note"]
    if headers and bodies:
        body_top = min(body.bbox[1] for body in bodies)
        body_bottom = max(body.bbox[3] for body in bodies)
        above = [block for block in headers if block.bbox[3] <= body_top + 1.0]
        below = [block for block in headers if block.bbox[1] >= body_bottom - 1.0]
        middle = [block for block in headers if block not in above and block not in below]
        for block in middle:
            conflicts.append(
                OrderConflict(
                    kind="ambiguous_header_position",
                    involved=[block.uid],
                    message="header note vertically overlaps the main body span; before/after rule is undefined",
                )
            )
            unresolved.add(block.uid)
        previous: Block | None = None
        for header in sorted(above, key=lambda block: (block.y_center, -block.x_center)):
            if previous is not None:
                edges.append(
                    GraphEdge(
                        previous.uid,
                        header.uid,
                        "headers_before_body_top_to_bottom",
                        {
                            "previous_y_center": round(previous.y_center, 3),
                            "current_y_center": round(header.y_center, 3),
                        },
                    )
                )
            previous = header
        if above:
            edges.append(
                GraphEdge(
                    above[-1].uid,
                    bodies[0].uid,
                    "header_above_main_chain_reads_before",
                    {
                        "header_bottom": round(above[-1].bbox[3], 3),
                        "body_top": round(body_top, 3),
                    },
                )
            )
        previous = None
        for header in sorted(below, key=lambda block: (block.y_center, -block.x_center)):
            if previous is not None:
                edges.append(
                    GraphEdge(
                        previous.uid,
                        header.uid,
                        "headers_after_body_top_to_bottom",
                        {
                            "previous_y_center": round(previous.y_center, 3),
                            "current_y_center": round(header.y_center, 3),
                        },
                    )
                )
            previous = header
        if below:
            edges.append(
                GraphEdge(
                    bodies[-1].uid,
                    below[0].uid,
                    "header_below_main_chain_reads_after",
                    {
                        "body_bottom": round(body_bottom, 3),
                        "header_top": round(below[0].bbox[1], 3),
                    },
                )
            )

    graph_nodes = {block.uid for block in blocks} - unresolved
    graph_edges = [edge for edge in edges if edge.source in graph_nodes and edge.target in graph_nodes]
    order, cycle_nodes, cycle_edges = _topological_sort(graph_nodes, graph_edges, by_uid)
    if cycle_nodes:
        unresolved.update(cycle_nodes)
        conflicts.append(
            OrderConflict(
                kind="directed_cycle",
                involved=sorted(cycle_nodes),
                message="reading-order constraints contain a directed cycle",
                candidate_edges=cycle_edges,
            )
        )
    for block in blocks:
        block.order_index = None
    for index, uid in enumerate(order):
        by_uid[uid].order_index = index
    return OrderResult(
        order=order,
        edges=graph_edges,
        unresolved=sorted(unresolved),
        conflicts=conflicts,
    )


def _horizontal_anchor_scores(
    block: Block,
    bodies: list[Block],
) -> tuple[dict[str, tuple[float, float, float]], list[tuple[Block, float, float, float]]]:
    scores: dict[str, tuple[float, float, float]] = {}
    candidates: list[tuple[Block, float, float, float]] = []
    for body in bodies:
        overlap = max(0.0, min(block.bbox[2], body.bbox[2]) - max(block.bbox[0], body.bbox[0]))
        gap = max(0.0, body.bbox[0] - block.bbox[2], block.bbox[0] - body.bbox[2])
        score = overlap if overlap > 0 else -gap
        scores[body.uid] = (score, overlap, gap)
        candidates.append((body, score, overlap, gap))
    candidates.sort(key=lambda item: (-item[1], item[0].x_center))
    return scores, candidates


def _nearest_body_candidates(
    block: Block,
    bodies: list[Block],
) -> list[tuple[Block, float, float]]:
    candidates: list[tuple[Block, float, float]] = []
    for body in bodies:
        overlap = max(0.0, min(block.bbox[2], body.bbox[2]) - max(block.bbox[0], body.bbox[0]))
        gap = max(0.0, body.bbox[0] - block.bbox[2], block.bbox[0] - body.bbox[2])
        candidates.append((body, gap, overlap))
    candidates.sort(key=lambda item: (item[1], -item[2], item[0].x_center))
    return candidates


def _topological_sort(
    nodes: set[str],
    edges: list[GraphEdge],
    by_uid: dict[str, Block],
) -> tuple[list[str], set[str], list[GraphEdge]]:
    graph = {node: [] for node in nodes}
    indegree = {node: 0 for node in nodes}
    for edge in edges:
        graph[edge.source].append(edge.target)
        indegree[edge.target] += 1
    ready = [
        node
        for node in nodes
        if indegree[node] == 0
    ]
    ready.sort(key=lambda uid: (by_uid[uid].bbox[1], -by_uid[uid].bbox[0], uid))
    order: list[str] = []
    while ready:
        node = ready.pop(0)
        order.append(node)
        for target in sorted(graph[node], key=lambda uid: (by_uid[uid].bbox[1], -by_uid[uid].bbox[0], uid)):
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
                ready.sort(key=lambda uid: (by_uid[uid].bbox[1], -by_uid[uid].bbox[0], uid))
    leftover = nodes - set(order)
    if not leftover:
        return order, set(), []
    cycle_nodes = _cycle_nodes(leftover, graph)
    cycle_edges = [
        edge
        for edge in edges
        if edge.source in cycle_nodes and edge.target in cycle_nodes
    ]
    return order, cycle_nodes or leftover, cycle_edges


def _cycle_nodes(nodes: set[str], graph: dict[str, list[str]]) -> set[str]:
    # Tarjan SCC over the unresolved remainder.
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    cycles: set[str] = set()

    def visit(node: str) -> None:
        nonlocal index
        indices[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for target in graph.get(node, []):
            if target not in nodes:
                continue
            if target not in indices:
                visit(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[target])
        if lowlinks[node] == indices[node]:
            component: list[str] = []
            while True:
                member = stack.pop()
                on_stack.remove(member)
                component.append(member)
                if member == node:
                    break
            if len(component) > 1:
                cycles.update(component)

    for node in sorted(nodes):
        if node not in indices:
            visit(node)
    return cycles
