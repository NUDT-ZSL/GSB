"""Incremental update planning and dependency invalidation propagation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Sequence, Set

from .config import (
    GRAPH_NODES,
    NODE_AGGREGATE,
    NODE_DETECT,
    NODE_MATCH,
    NODE_PREPROCESS,
)
from .graph import ExecutionPlan


@dataclass
class ChangeSet:
    """Declared semantic changes between two runs."""

    new_page_ids: Set[str] = field(default_factory=set)
    charset_versions_bumped: bool = False
    corrected_pages: Dict[str, List[int]] = field(default_factory=dict)
    detect_threshold_changed: bool = False


@dataclass
class InvalidationPlan:
    """The affected node/page/line sets plus reuse/recompute node lists."""

    affected_pages: Set[str]
    affected_lines: Dict[str, List[int]]
    recompute_nodes: List[str]
    reused_nodes: List[str]
    affected_nodes_by_page: Dict[str, List[str]]


class IncrementalPlanner:
    """Computes the minimal suffix that must be recomputed for a change set."""

    def __init__(self, plan: ExecutionPlan) -> None:
        self.plan = plan

    def plan_changes(
        self,
        all_page_ids: Sequence[str],
        change_set: ChangeSet,
    ) -> InvalidationPlan:
        affected_pages: Set[str] = set(change_set.new_page_ids)
        affected_lines: Dict[str, List[int]] = {}
        affected_nodes_by_page: Dict[str, Set[str]] = {
            page_id: set() for page_id in all_page_ids
        }

        if change_set.charset_versions_bumped or change_set.detect_threshold_changed:
            # Library/threshold changes are global: every page is affected.
            affected_pages.update(all_page_ids)

        for page_id, line_indices in change_set.corrected_pages.items():
            affected_pages.add(page_id)
            affected_lines[page_id] = sorted(set(line_indices))

        for page_id in affected_pages:
            entry_nodes: Dict[str, bool] = {node: False for node in GRAPH_NODES}
            if page_id in change_set.new_page_ids:
                # A brand-new page computes every node.
                for node in GRAPH_NODES:
                    entry_nodes[node] = True
            if change_set.detect_threshold_changed:
                # Threshold feeds preprocess and detect and flows downstream.
                entry_nodes[NODE_PREPROCESS] = True
                entry_nodes[NODE_DETECT] = True
            if change_set.charset_versions_bumped:
                # Template version only affects match and its suffix;
                # preprocess/detect/features remain reusable.
                entry_nodes[NODE_MATCH] = True
            if page_id in change_set.corrected_pages:
                # Ground-truth correction only changes the aggregate node.
                entry_nodes[NODE_AGGREGATE] = True
            suffix = set(self.plan.suffix_from(entry_nodes))
            affected_nodes_by_page[page_id] = suffix

        all_affected_nodes: Set[str] = set()
        for nodes in affected_nodes_by_page.values():
            all_affected_nodes.update(nodes)

        recompute_nodes = [
            node for node in self.plan.order if node in all_affected_nodes
        ]
        reused_nodes = [
            node for node in self.plan.order if node not in all_affected_nodes
        ]
        return InvalidationPlan(
            affected_pages=set(sorted(affected_pages)),
            affected_lines=affected_lines,
            recompute_nodes=recompute_nodes,
            reused_nodes=reused_nodes,
            affected_nodes_by_page={
                page_id: sorted(nodes)
                for page_id, nodes in sorted(affected_nodes_by_page.items())
                if nodes
            },
        )
