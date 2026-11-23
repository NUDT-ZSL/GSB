"""Cost estimation, dual-budget admission and explicit conflict arbitration."""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import FusionConfig
from .errors import ErrorCode
from .models import CHANNELS, Document, Query


@dataclass
class CostEstimate:
    query_id: str
    caps: dict[str, int]
    recall_cost: int
    fusion_cost: int
    eligible_docs: int
    admitted: bool
    rejection_code: str | None = None
    rejection_reason: str | None = None
    remaining_after: dict[str, int] = field(default_factory=dict)
    two_stage_fallback: bool = False


@dataclass
class ArbitrationRecord:
    trigger: str
    basis: str
    sacrificed_metric: str
    chosen_batch_size: int
    detail: dict


def count_eligible(documents: list[Document], query: Query) -> int:
    return sum(1 for doc in documents if _passes(doc, query))


def _passes(doc: Document, query: Query) -> bool:
    if query.category_filter is not None and doc.category != query.category_filter:
        return False
    if query.min_quality is not None and doc.quality_score < query.min_quality:
        return False
    return True


def estimate_caps(
    documents: list[Document], query: Query, config: FusionConfig
) -> tuple[dict[str, int], int, int]:
    """Estimate per-channel caps and raw costs before running recall."""
    eligible = count_eligible(documents, query)
    caps: dict[str, int] = {}
    for channel in CHANNELS:
        channel_cfg = config.channels[channel]
        if not channel_cfg.enabled:
            caps[channel] = 0
            continue
        if channel == "category" and query.category_filter is None:
            caps[channel] = 0
        else:
            caps[channel] = min(channel_cfg.candidate_limit, eligible)
    recall_cost = sum(caps.values())
    if config.scheduler.two_stage_enabled:
        coarse = min(sum(caps.values()), config.scheduler.fine_pool_size)
        fusion_cost = coarse + min(coarse, query.top_k)
    else:
        fusion_cost = min(sum(caps.values()), eligible)
    return caps, recall_cost, fusion_cost


def shrink_caps(
    caps: dict[str, int], config: FusionConfig, floor: int
) -> dict[str, int]:
    """Deterministically shrink every positive cap by the configured step."""
    step = config.scheduler.expansion_step
    shrunk: dict[str, int] = {}
    for channel, cap in caps.items():
        if cap <= floor:
            shrunk[channel] = cap
        else:
            shrunk[channel] = max(floor, cap - step)
    return shrunk


def costs_for_caps(caps: dict[str, int], config: FusionConfig, query: Query) -> tuple[int, int]:
    recall_cost = sum(caps.values())
    if config.scheduler.two_stage_enabled:
        coarse = min(recall_cost, config.scheduler.fine_pool_size)
        fusion_cost = coarse + min(coarse, query.top_k)
    else:
        fusion_cost = recall_cost
    return recall_cost, fusion_cost


class AdmissionController:
    """Sequential, deterministic dual-budget admission over ordered queries."""

    def __init__(self, documents: list[Document], config: FusionConfig):
        self.documents = documents
        self.config = config
        self.remaining_recall = config.budgets.total_candidates
        self.remaining_fusion = config.budgets.fusion_comparisons
        self.records: list[CostEstimate] = []
        self.rejected: list[CostEstimate] = []
        self.arbitrations: list[ArbitrationRecord] = []

    def admit(self, query: Query, batch_size: int) -> CostEstimate:
        caps, recall_cost, fusion_cost = estimate_caps(
            self.documents, query, self.config
        )
        eligible = count_eligible(self.documents, query)
        record = CostEstimate(
            query_id=query.query_id,
            caps=dict(caps),
            recall_cost=recall_cost,
            fusion_cost=fusion_cost,
            eligible_docs=eligible,
            admitted=False,
        )

        fit = recall_cost <= self.remaining_recall and fusion_cost <= self.remaining_fusion
        if not fit:
            caps, recall_cost, fusion_cost = self._arbitrate_shrink(
                query, caps, record, batch_size
            )
            if (
                self.config.scheduler.two_stage_enabled
                and recall_cost <= self.remaining_recall
                and fusion_cost > self.remaining_fusion
                and recall_cost <= self.remaining_fusion
            ):
                # Conflict rule 7: two-stage coarse+fine ranking breaches the
                # fusion budget; fall back to single-stage (sacrifice precision).
                self.arbitrations.append(
                    ArbitrationRecord(
                        trigger="two_stage_fusion_budget_conflict",
                        basis=(
                            "two-stage fusion cost "
                            f"{record.fusion_cost} > remaining fusion "
                            f"{self.remaining_fusion}; single-stage cost "
                            f"{recall_cost} fits"
                        ),
                        sacrificed_metric="precision",
                        chosen_batch_size=batch_size,
                        detail={
                            "query_id": query.query_id,
                            "action": "fallback_single_stage",
                            "two_stage_fusion_cost": record.fusion_cost,
                            "single_stage_fusion_cost": recall_cost,
                        },
                    )
                )
                fusion_cost = recall_cost
                record.two_stage_fallback = True

        if recall_cost <= self.remaining_recall and fusion_cost <= self.remaining_fusion:
            record.admitted = True
            record.caps = caps
            record.recall_cost = recall_cost
            record.fusion_cost = fusion_cost
            self.remaining_recall -= recall_cost
            self.remaining_fusion -= fusion_cost
            record.remaining_after = {
                "total_candidates": self.remaining_recall,
                "fusion_comparisons": self.remaining_fusion,
            }
        else:
            self._reject(record, recall_cost, fusion_cost)

        self.records.append(record)
        if not record.admitted:
            self.rejected.append(record)
        return record

    def _arbitrate_shrink(
        self,
        query: Query,
        caps: dict[str, int],
        record: CostEstimate,
        batch_size: int,
    ) -> tuple[dict[str, int], int, int]:
        floor = self.config.scheduler.min_channel_floor
        current = dict(caps)
        recall_cost, fusion_cost = costs_for_caps(current, self.config, query)
        for _ in range(10):
            if (
                recall_cost <= self.remaining_recall
                and fusion_cost <= self.remaining_fusion
            ):
                break
            shrunk = shrink_caps(current, self.config, floor)
            if shrunk == current:
                break
            current = shrunk
            recall_cost, fusion_cost = costs_for_caps(current, self.config, query)
        if current != caps:
            self.arbitrations.append(
                ArbitrationRecord(
                    trigger="budget_pressure",
                    basis=(
                        "conflict_priority="
                        + ">".join(self.config.scheduler.conflict_priority)
                        + "; shrink caps before rejecting when precision is sacrificable"
                    ),
                    sacrificed_metric="precision",
                    chosen_batch_size=batch_size,
                    detail={
                        "query_id": query.query_id,
                        "original_caps": caps,
                        "shrunk_caps": current,
                        "remaining_recall": self.remaining_recall,
                        "remaining_fusion": self.remaining_fusion,
                    },
                )
            )
        return current, recall_cost, fusion_cost

    def _reject(
        self, record: CostEstimate, recall_cost: int, fusion_cost: int
    ) -> None:
        # Explicit priority: compare which budget is violated first per config.
        recall_over = recall_cost > self.remaining_recall
        fusion_over = fusion_cost > self.remaining_fusion
        priority = self.config.scheduler.conflict_priority
        if recall_over and fusion_over:
            code = (
                ErrorCode.E_RECALL_BUDGET
                if priority.index("precision") < priority.index("memory")
                else ErrorCode.E_FUSION_BUDGET
            )
        elif recall_over:
            code = ErrorCode.E_RECALL_BUDGET
        else:
            code = ErrorCode.E_FUSION_BUDGET
        record.rejection_code = code
        record.rejection_reason = (
            f"need recall={recall_cost}, fusion={fusion_cost}; "
            f"remaining recall={self.remaining_recall}, fusion={self.remaining_fusion}"
        )
        record.remaining_after = {
            "total_candidates": self.remaining_recall,
            "fusion_comparisons": self.remaining_fusion,
        }

    def rejection_report(self) -> list[dict]:
        return [
            {
                "query_id": record.query_id,
                "code": record.rejection_code,
                "reason": record.rejection_reason,
                "remaining_budget": record.remaining_after,
                "estimated_caps": record.caps,
            }
            for record in self.rejected
        ]
