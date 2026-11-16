"""Deterministic cost model, batch enumeration and adaptive path selection."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

from .execution import FUSED, NAIVE, build_fusion_segments
from .model import DataBlock, width_of
from .parser import Node
from .typecheck import TypeAnalyzer

DEFAULT_BATCH_CANDIDATES = (64, 256, 1024, 4096)
# Cost difference below this fraction of the mean cost triggers a stable tie-break.
COST_TIE_THRESHOLD = 0.02
ROW_WORK_WEIGHT = 1
MATERIALIZATION_WEIGHT = 8


@dataclass(frozen=True)
class ScaleParameters:
    row_count: int
    batch_size: int
    column_count: int
    expression_depth: int
    intermediate_count: int
    fused_segment_count: int


@dataclass(frozen=True)
class PathEstimate:
    path: str
    batch_size: int
    batch_count: int
    peak_bytes: int
    estimated_work: int
    memory_basis: str


@dataclass(frozen=True)
class CandidateRanking:
    batch_size: int
    batch_count: int
    naive_peak_bytes: int
    fused_peak_bytes: int
    naive_work: int
    fused_work: int
    selected_path: str
    reason: str
    reason_code: str


@dataclass(frozen=True)
class Decision:
    selected_path: str
    batch_size: int
    batch_count: int
    reason: str
    reason_code: str
    threshold: float
    scale: ScaleParameters
    estimates: tuple[PathEstimate, ...]
    ranking: tuple[CandidateRanking, ...]

    def to_dict(self) -> dict:
        return {
            "selected_path": self.selected_path,
            "batch_size": self.batch_size,
            "batch_count": self.batch_count,
            "reason": self.reason,
            "reason_code": self.reason_code,
            "cost_tie_threshold": self.threshold,
            "scale": asdict(self.scale),
            "estimates": [asdict(item) for item in self.estimates],
            "ranking": [asdict(item) for item in self.ranking],
        }


def _batch_count(row_count: int, batch_size: int) -> int:
    return math.ceil(row_count / batch_size) if row_count else 1


def _intermediate_widths(root: Node, analyzer: TypeAnalyzer) -> list[int]:
    return [
        width_of(analyzer.typed[id(node)].result_type) + 1
        for node in root.walk_postorder()
        if node.kind not in ("column", "literal")
    ]


def estimate_peak_bytes(
    root: Node,
    analyzer: TypeAnalyzer,
    row_count: int,
    batch_size: int,
    path: str,
) -> int:
    """Integer byte estimate using an explicit fixed observation口径.

    Truth source: every retained materialization is counted as
    ``rows_in_batch * (fixed dtype width + one nullmap byte)``. Input columns are
    read via zero-copy slices and excluded; output result allocation is included.
    The estimate is a pure function, so repeated calls return identical integers.
    """
    effective_batch = min(row_count, batch_size) if row_count else 0
    widths = _intermediate_widths(root, analyzer)
    if not widths:
        return 0
    if path == NAIVE:
        # Naive retains every operator result concurrently inside one batch.
        intermediate_peak = effective_batch * sum(widths)
    elif path == FUSED:
        segments = build_fusion_segments(root, analyzer)
        root_widths = [
            width_of(analyzer.typed[id(segment[-1])].result_type) + 1
            for segment in segments
        ]
        intermediate_peak = effective_batch * sum(root_widths)
    else:  # pragma: no cover
        raise ValueError(f"unknown path {path}")
    result_width = widths[-1]
    # Full result is assembled once across the whole block for both paths.
    return int(row_count * result_width + intermediate_peak)


def _work_estimate(
    root: Node,
    analyzer: TypeAnalyzer,
    row_count: int,
    batch_size: int,
    path: str,
) -> int:
    operator_count = sum(
        1 for node in root.walk_postorder() if node.kind not in ("column", "literal")
    )
    compute = ROW_WORK_WEIGHT * row_count * operator_count
    if path == NAIVE:
        materialized = operator_count
    else:
        materialized = len(build_fusion_segments(root, analyzer))
    materialization = (
        MATERIALIZATION_WEIGHT
        * row_count
        * materialized
        * (width_of(analyzer.typed[id(root)].result_type) + 1)
    )
    loop_overhead = _batch_count(row_count, batch_size) * operator_count
    return int(compute + materialization + loop_overhead)


def _choose_path(naive_work: int, fused_work: int, naive_bytes: int, fused_bytes: int) -> tuple[str, str, str]:
    if naive_work == 0 or fused_work == 0:
        return FUSED, "no operator work; stable default fused path", "EMPTY"
    mean = (naive_work + fused_work) / 2.0
    gap = abs(naive_work - fused_work)
    if gap < COST_TIE_THRESHOLD * mean:
        if fused_bytes != naive_bytes:
            chosen = FUSED if fused_bytes < naive_bytes else NAIVE
            return (
                chosen,
                f"cost gap {gap} below threshold {COST_TIE_THRESHOLD:.2%}; chose lower peak memory",
                "TIE_MEMORY",
            )
        # Final stable决胜 rule: lexical path name, fused before naive.
        chosen = min((NAIVE, FUSED))
        return chosen, "cost and memory tied; stable lexical tie-break selected fused", "TIE_LEXICAL"
    if fused_work < naive_work:
        return FUSED, f"fused estimated work {fused_work} < naive {naive_work}", "LOWER_WORK"
    return NAIVE, f"naive estimated work {naive_work} < fused {fused_work}", "LOWER_WORK"


def choose_plan(
    root: Node,
    block: DataBlock,
    analyzer: TypeAnalyzer,
    batch_candidates: tuple[int, ...] = DEFAULT_BATCH_CANDIDATES,
) -> Decision:
    row_count = block.row_count
    valid_candidates = tuple(sorted({size for size in batch_candidates if 0 < size <= max(row_count, 1)}))
    if not valid_candidates:
        valid_candidates = (max(row_count, 1),)
    operator_nodes = [node for node in root.walk_postorder() if node.kind not in ("column", "literal")]
    scale = ScaleParameters(
        row_count=row_count,
        batch_size=valid_candidates[0],
        column_count=len(block.columns),
        expression_depth=root.depth(),
        intermediate_count=len(operator_nodes),
        fused_segment_count=len(build_fusion_segments(root, analyzer)),
    )
    estimates: list[PathEstimate] = []
    rankings: list[CandidateRanking] = []
    for batch_size in valid_candidates:
        batch_count = _batch_count(row_count, batch_size)
        naive_bytes = estimate_peak_bytes(root, analyzer, row_count, batch_size, NAIVE)
        fused_bytes = estimate_peak_bytes(root, analyzer, row_count, batch_size, FUSED)
        naive_work = _work_estimate(root, analyzer, row_count, batch_size, NAIVE)
        fused_work = _work_estimate(root, analyzer, row_count, batch_size, FUSED)
        selected, reason, reason_code = _choose_path(naive_work, fused_work, naive_bytes, fused_bytes)
        estimates.extend(
            [
                PathEstimate(NAIVE, batch_size, batch_count, naive_bytes, naive_work,
                             "rows*(dtype_width+1); full result plus retained intermediates"),
                PathEstimate(FUSED, batch_size, batch_count, fused_bytes, fused_work,
                             "rows*(dtype_width+1); full result plus fused segment roots"),
            ]
        )
        rankings.append(
            CandidateRanking(
                batch_size=batch_size,
                batch_count=batch_count,
                naive_peak_bytes=naive_bytes,
                fused_peak_bytes=fused_bytes,
                naive_work=naive_work,
                fused_work=fused_work,
                selected_path=selected,
                reason=reason,
                reason_code=reason_code,
            )
        )
    # Rank candidates by selected-path peak memory, then total work, then ascending batch size.
    def ranking_key(item: CandidateRanking):
        selected_bytes = item.fused_peak_bytes if item.selected_path == FUSED else item.naive_peak_bytes
        selected_work = item.fused_work if item.selected_path == FUSED else item.naive_work
        return (selected_bytes, selected_work, item.batch_size)

    rankings = sorted(rankings, key=ranking_key)
    best = rankings[0]
    selected_path = best.selected_path
    scale = ScaleParameters(
        row_count=row_count,
        batch_size=best.batch_size,
        column_count=len(block.columns),
        expression_depth=root.depth(),
        intermediate_count=len(operator_nodes),
        fused_segment_count=len(build_fusion_segments(root, analyzer)),
    )
    return Decision(
        selected_path=selected_path,
        batch_size=best.batch_size,
        batch_count=best.batch_count,
        reason=best.reason,
        reason_code=best.reason_code,
        threshold=COST_TIE_THRESHOLD,
        scale=scale,
        estimates=tuple(estimates),
        ranking=tuple(rankings),
    )
