"""跨路径一致性裁决：精确路径 vs 近似候选路径。

输出意图一致率、码点偏移差异、置信度偏差谱，并把差异归因为
候选剪枝遗漏或平局规则应用不同；禁止用容差掩盖真实差异。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .errors import BizError
from .matcher import MatchResult


@dataclass(frozen=True)
class Divergence:
    ticket_id: str
    kind: str
    exact_intent: str
    approx_intent: str
    exact_hits: List[dict]
    approx_hits: List[dict]
    keyword: Optional[str]
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ticket_id": self.ticket_id,
            "kind": self.kind,
            "exact_intent": self.exact_intent,
            "approx_intent": self.approx_intent,
            "exact_hits": self.exact_hits,
            "approx_hits": self.approx_hits,
            "keyword": self.keyword,
            "detail": self.detail,
        }


def _hit_index(result: MatchResult) -> Dict[Tuple[str, str, int, int], dict]:
    return {
        (hit.label, hit.keyword, hit.start, hit.end): hit.to_dict()
        for hit in result.hits
    }


def _classify_divergence(
    exact: MatchResult, approximate: MatchResult
) -> Tuple[str, Optional[str], Dict[str, Any]]:
    """区分差异来源：候选剪枝遗漏 vs 平局规则应用不同。"""
    exact_keys = set(_hit_index(exact).keys())
    approx_keys = set(_hit_index(approximate).keys())
    missing = sorted(exact_keys - approx_keys)
    extra = sorted(approx_keys - exact_keys)

    exact_hit_labels = {hit.label for hit in exact.hits}
    approx_hit_labels = {hit.label for hit in approximate.hits}

    detail = {
        "missing_hits": [
            {"label": m[0], "keyword": m[1], "start": m[2], "end": m[3]}
            for m in missing
        ],
        "extra_hits": [
            {"label": e[0], "keyword": e[1], "start": e[2], "end": e[3]}
            for e in extra
        ],
        "exact_hit_labels": sorted(exact_hit_labels),
        "approx_hit_labels": sorted(approx_hit_labels),
        "exact_confidence": round(exact.confidence, 6),
        "approx_confidence": round(approximate.confidence, 6),
    }

    if missing:
        first = missing[0]
        return "candidate_pruning_miss", first[1], detail

    # 命中完全相同但结论不同，只能来自平局/打分口径差异
    if exact.hits and exact.hits == approximate.hits and exact.intent_label != approximate.intent_label:
        return "tie_break_mismatch", exact.hits[0].keyword, detail

    # 命中集合相同（顺序差异）但意图不同，仍归为平局规则差异
    if exact_hit_labels == approx_hit_labels and exact.intent_label != approximate.intent_label:
        keyword = exact.hits[0].keyword if exact.hits else None
        return "tie_break_mismatch", keyword, detail

    keyword = missing[0][1] if missing else None
    return "candidate_pruning_miss", keyword, detail


def adjudicate(
    exact_results: Sequence[MatchResult],
    approximate_results: Sequence[MatchResult],
    raise_on_divergence: bool = True,
) -> Dict[str, Any]:
    """对同一工单集合的两条路径结果做全量裁决。"""
    exact_by_id = {result.ticket_id: result for result in exact_results}
    approx_by_id = {result.ticket_id: result for result in approximate_results}
    if set(exact_by_id) != set(approx_by_id):
        raise BizError(
            "E_PATH_DIVERGENCE",
            "tickets",
            {
                "exact_only": sorted(set(exact_by_id) - set(approx_by_id)),
                "approx_only": sorted(set(approx_by_id) - set(exact_by_id)),
            },
        )

    total = len(exact_by_id)
    intent_matches = 0
    offset_diffs: List[Dict[str, Any]] = []
    confidence_gaps: List[float] = []
    divergences: List[Divergence] = []

    for ticket_id in sorted(exact_by_id):
        exact = exact_by_id[ticket_id]
        approximate = approx_by_id[ticket_id]
        confidence_gaps.append(round(abs(exact.confidence - approximate.confidence), 6))

        exact_idx = _hit_index(exact)
        approx_idx = _hit_index(approximate)
        for key in sorted(set(exact_idx) | set(approx_idx)):
            if key not in exact_idx or key not in approx_idx:
                offset_diffs.append(
                    {
                        "ticket_id": ticket_id,
                        "keyword": key[1],
                        "exact": exact_idx.get(key),
                        "approx": approx_idx.get(key),
                    }
                )

        if (
            exact.intent_label == approximate.intent_label
            and exact_idx == approx_idx
            and exact.confidence == approximate.confidence
        ):
            intent_matches += 1
            continue

        kind, keyword, detail = _classify_divergence(exact, approximate)
        if exact.intent_label != approximate.intent_label:
            detail["comparable"] = False
        else:
            detail["comparable"] = True
            detail["reason"] = "offset_or_hit_difference_with_same_intent"
        divergence = Divergence(
            ticket_id=ticket_id,
            kind=kind,
            exact_intent=exact.intent_label,
            approx_intent=approximate.intent_label,
            exact_hits=[hit.to_dict() for hit in exact.hits],
            approx_hits=[hit.to_dict() for hit in approximate.hits],
            keyword=keyword,
            detail=detail,
        )
        divergences.append(divergence)

    confidence_spectrum = _confidence_spectrum(confidence_gaps)
    report = {
        "ticket_total": total,
        "intent_match_total": intent_matches,
        "intent_consistency_rate": round(intent_matches / total, 6) if total else 1.0,
        "offset_difference_total": len(offset_diffs),
        "offset_differences": offset_diffs,
        "confidence_gap_spectrum": confidence_spectrum,
        "divergence_total": len(divergences),
        "divergences": [item.to_dict() for item in divergences],
    }
    if divergences and raise_on_divergence:
        first = divergences[0]
        raise BizError(
            "E_PATH_DIVERGENCE",
            f"tickets[{first.ticket_id}]",
            {
                "first_divergence": first.to_dict(),
                "divergence_total": len(divergences),
            },
        )
    return report


def _confidence_spectrum(gaps: Sequence[float]) -> Dict[str, Any]:
    """置信度偏差谱：最大值、均值、分桶计数，全部确定性计算。"""
    if not gaps:
        return {"max": 0.0, "mean": 0.0, "buckets": {}}
    ordered = sorted(gaps)
    buckets = {
        "eq_0": 0,
        "le_1e-6": 0,
        "le_1e-4": 0,
        "le_1e-2": 0,
        "gt_1e-2": 0,
    }
    for gap in ordered:
        if gap == 0.0:
            buckets["eq_0"] += 1
        if gap <= 1e-6:
            buckets["le_1e-6"] += 1
        if gap <= 1e-4:
            buckets["le_1e-4"] += 1
        if gap <= 1e-2:
            buckets["le_1e-2"] += 1
        else:
            buckets["gt_1e-2"] += 1
    mean = round(sum(ordered) / len(ordered), 6)
    percentiles = np.percentile(np.asarray(ordered, dtype=np.float64), [50, 90, 99])
    return {
        "max": ordered[-1],
        "mean": mean,
        "p50": round(float(percentiles[0]), 6),
        "p90": round(float(percentiles[1]), 6),
        "p99": round(float(percentiles[2]), 6),
        "buckets": buckets,
    }
