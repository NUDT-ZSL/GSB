"""Integer-only cardinality, cost, weight, and materialization decisions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import MATERIALIZE_THRESHOLD, MAX_MATERIALIZED, SELECTIVITY_BP
from .plan import (
    Aggregate,
    CseCandidate,
    Filter,
    Join,
    PlanNode,
    Project,
    Scan,
    collect_scans,
)
from .util import ceil_div


@dataclass(frozen=True)
class Estimate:
    rows: int
    cost: int


@dataclass(frozen=True)
class CandidateDecision:
    candidate: CseCandidate
    estimated_rows: int
    cost: int
    weight: int
    rank: int
    selected: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate.candidate_id,
            "fingerprint": self.candidate.fingerprint,
            "reference_count": self.candidate.reference_count,
            "refs": list(self.candidate.refs),
            "estimated_rows": self.estimated_rows,
            "cost": self.cost,
            "weight": self.weight,
            "rank": self.rank,
            "selected": self.selected,
            "reason": self.reason,
            "plan": self.candidate.plan_json,
        }


class Estimator:
    def __init__(
        self,
        table_rows: dict[str, int],
        field_cardinality: dict[str, int] | None = None,
    ) -> None:
        self.table_rows = {key: max(0, int(value)) for key, value in table_rows.items()}
        self.field_cardinality = {
            key: max(1, int(value))
            for key, value in (field_cardinality or {}).items()
        }

    def estimate(self, node: PlanNode) -> Estimate:
        if isinstance(node, Scan):
            rows = self.table_rows.get(node.table, 0)
            return Estimate(rows=rows, cost=rows)
        if isinstance(node, Filter):
            child = self.estimate(node.child)
            rows = child.rows
            for condition in node.conditions:
                rows = self._apply_selectivity(rows, condition.op, condition.value)
            return Estimate(rows=rows, cost=child.cost + child.rows)
        if isinstance(node, Join):
            left = self.estimate(node.left)
            right = self.estimate(node.right)
            left_card = self.field_cardinality.get(node.left_key, 1)
            right_card = self.field_cardinality.get(node.right_key, 1)
            divisor = max(1, left_card, right_card)
            rows = ceil_div(max(1, left.rows * right.rows), divisor)
            compare_cost = max(1, left.rows * right.rows)
            return Estimate(
                rows=rows,
                cost=left.cost + right.cost + compare_cost,
            )
        if isinstance(node, Aggregate):
            child = self.estimate(node.child)
            if not node.group_by:
                rows = 1 if child.rows else 0
            else:
                groups = 1
                for field in node.group_by:
                    groups *= self.field_cardinality.get(field, max(1, child.rows // 2))
                    if groups > child.rows:
                        break
                rows = min(child.rows, groups) if child.rows else 0
            return Estimate(rows=rows, cost=child.cost + 2 * child.rows)
        if isinstance(node, Project):
            child = self.estimate(node.child)
            return Estimate(rows=child.rows, cost=child.cost + child.rows)
        raise TypeError(f"unknown plan node: {type(node)!r}")

    def _apply_selectivity(self, rows: int, op: str, value: Any) -> int:
        if rows <= 0:
            return 0
        if op == "eq":
            basis_points = SELECTIVITY_BP["eq"]
        elif op == "ne":
            basis_points = SELECTIVITY_BP["ne"]
        elif op in {"lt", "le", "gt", "ge"}:
            basis_points = SELECTIVITY_BP[op]
        elif op == "in":
            size = len(value) if isinstance(value, (list, tuple)) else 1
            basis_points = min(
                SELECTIVITY_BP["in_max"],
                SELECTIVITY_BP["in_per_value"] * size,
            )
            if size == 0:
                return 0
        elif op == "contains":
            basis_points = SELECTIVITY_BP["contains"]
        else:
            raise ValueError(f"unsupported filter operator: {op}")
        return max(1, ceil_div(rows * basis_points, 10_000))


def candidate_weight(reference_count: int, cost: int, estimated_rows: int) -> int:
    """Net static work saved by materializing one result once."""

    return (reference_count - 1) * cost - estimated_rows


def decide_candidates(
    candidates: list[CseCandidate],
    estimator: Estimator,
    threshold: int = MATERIALIZE_THRESHOLD,
    budget: int = MAX_MATERIALIZED,
) -> list[CandidateDecision]:
    """Apply the documented total ordering and deterministic budget."""

    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        estimate = estimator.estimate(candidate.node)
        weight = candidate_weight(
            candidate.reference_count,
            estimate.cost,
            estimate.rows,
        )
        rows.append(
            {
                "candidate": candidate,
                "estimate": estimate,
                "weight": weight,
            }
        )

    eligible = [
        item
        for item in rows
        if item["candidate"].reference_count >= 2
        and item["weight"] >= threshold
    ]
    eligible.sort(
        key=lambda item: (
            -item["weight"],
            -item["estimate"].cost,
            -item["candidate"].reference_count,
            item["candidate"].fingerprint,
        )
    )
    rank_by_fingerprint = {
        item["candidate"].fingerprint: rank
        for rank, item in enumerate(eligible, start=1)
    }

    decisions: list[CandidateDecision] = []
    for item in rows:
        candidate: CseCandidate = item["candidate"]
        estimate: Estimate = item["estimate"]
        weight: int = item["weight"]
        rank = rank_by_fingerprint.get(candidate.fingerprint, 0)
        if candidate.reference_count < 2:
            selected = False
            reason = (
                f"rejected: reference_count={candidate.reference_count} < 2; "
                f"weight=({candidate.reference_count}-1)*{estimate.cost}-"
                f"{estimate.rows}={weight}"
            )
        elif weight < threshold:
            selected = False
            reason = (
                f"rejected: weight=({candidate.reference_count}-1)*{estimate.cost}-"
                f"{estimate.rows}={weight} < threshold={threshold}"
            )
        elif rank > budget:
            selected = False
            reason = (
                f"rejected: budget exhausted; rank={rank} > {budget}; "
                f"weight=({candidate.reference_count}-1)*{estimate.cost}-"
                f"{estimate.rows}={weight}"
            )
        else:
            selected = True
            reason = (
                f"selected: weight=({candidate.reference_count}-1)*{estimate.cost}-"
                f"{estimate.rows}={weight} >= threshold={threshold}; "
                f"rank={rank} <= {budget}"
            )
        decisions.append(
            CandidateDecision(
                candidate=candidate,
                estimated_rows=estimate.rows,
                cost=estimate.cost,
                weight=weight,
                rank=rank,
                selected=selected,
                reason=reason,
            )
        )
    return sorted(decisions, key=lambda item: item.candidate.fingerprint)
