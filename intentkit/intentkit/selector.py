"""基于数据规模观测的自适应路径取舍器。

同一观测输入下输出逐字节一致的取舍记录；决策只依赖整数/浮点标量，
不读取系统时间或字典遍历顺序。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Sequence

from .matcher import build_keyword_entries, build_signature_buckets
from .models import IntentConfig, Ticket


@dataclass(frozen=True)
class ScaleObservation:
    ticket_count: int
    keyword_total: int
    avg_query_codepoints: float
    avg_keyword_codepoints: float
    distinct_first_chars: int
    enabled_intent_total: int

    def to_dict(self) -> dict:
        return {
            "ticket_count": self.ticket_count,
            "keyword_total": self.keyword_total,
            "avg_query_codepoints": round(self.avg_query_codepoints, 6),
            "avg_keyword_codepoints": round(self.avg_keyword_codepoints, 6),
            "distinct_first_chars": self.distinct_first_chars,
            "enabled_intent_total": self.enabled_intent_total,
        }


@dataclass(frozen=True)
class SelectionDecision:
    path: str
    streaming: bool
    batch_size: int
    estimated_exact_comparisons: int
    estimated_approx_comparisons: int
    estimated_resident_keywords: int
    observations: ScaleObservation
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "streaming": self.streaming,
            "batch_size": self.batch_size,
            "estimated_exact_comparisons": self.estimated_exact_comparisons,
            "estimated_approx_comparisons": self.estimated_approx_comparisons,
            "estimated_resident_keywords": self.estimated_resident_keywords,
            "observations": self.observations.to_dict(),
            "reasons": list(self.reasons),
        }


def observe(tickets: Sequence[Ticket], config: IntentConfig) -> ScaleObservation:
    """收集规模观测：工单数、关键词总量、平均码点长度等。"""
    entries = build_keyword_entries(config)
    ticket_count = len(tickets)
    if ticket_count:
        avg_query = sum(len(ticket.query_text) for ticket in tickets) / ticket_count
    else:
        avg_query = 0.0
    if entries:
        avg_keyword = sum(entry.length for entry in entries) / len(entries)
    else:
        avg_keyword = 0.0
    first_chars = {entry.first_char for entry in entries}
    return ScaleObservation(
        ticket_count=ticket_count,
        keyword_total=len(entries),
        avg_query_codepoints=avg_query,
        avg_keyword_codepoints=avg_keyword,
        distinct_first_chars=len(first_chars),
        enabled_intent_total=len(config.rules),
    )


def estimate_exact_comparisons(obs: ScaleObservation) -> int:
    """精确路径估计：每工单每码点都需面对全量关键词。"""
    return int(
        round(obs.ticket_count * obs.avg_query_codepoints * obs.keyword_total)
    )


def estimate_approx_comparisons(
    config: IntentConfig, obs: ScaleObservation
) -> int:
    """近似路径估计：首字符分桶后平均桶大小 + 长度剪枝。"""
    entries = build_keyword_entries(config)
    buckets = build_signature_buckets(entries)
    if not buckets:
        return 0
    avg_bucket = sum(len(bucket) for bucket in buckets.values()) / len(buckets)
    length_ratio = min(
        1.0,
        obs.avg_query_codepoints
        / max(obs.avg_query_codepoints + obs.avg_keyword_codepoints, 1.0),
    )
    return int(
        round(
            obs.ticket_count
            * obs.avg_query_codepoints
            * avg_bucket
            * length_ratio
        )
    )


def choose_path(
    tickets: Sequence[Ticket],
    config: IntentConfig,
    exact_threshold: int = 40,
    streaming_ticket_threshold: int = 128,
) -> SelectionDecision:
    """按规模观测做确定性取舍，并按需选择分批流式执行。

    exact_threshold：当关键词总量不超过该值且工单数不大时，
    精确路径的可预测性优于近似路径的剪枝开销。
    """
    obs = observe(tickets, config)
    exact_est = estimate_exact_comparisons(obs)
    approx_est = estimate_approx_comparisons(config, obs)
    reasons: List[str] = []

    small_keyword_set = obs.keyword_total <= exact_threshold
    small_ticket_set = obs.ticket_count <= streaming_ticket_threshold

    if small_keyword_set and small_ticket_set:
        path = "exact"
        reasons.append("keyword_total_within_exact_threshold")
        reasons.append("ticket_count_within_streaming_threshold")
    elif approx_est < exact_est:
        path = "approximate"
        reasons.append("approximate_estimate_lower")
    else:
        path = "exact"
        reasons.append("exact_estimate_not_worse")

    rule_batch_sizes = [rule.batch_size for rule in config.rules]
    configured_batch = min(rule_batch_sizes) if rule_batch_sizes else streaming_ticket_threshold
    streaming = obs.ticket_count > streaming_ticket_threshold
    batch_size = configured_batch if streaming else obs.ticket_count
    if streaming:
        reasons.append("streaming_batches_enabled")

    return SelectionDecision(
        path=path,
        streaming=streaming,
        batch_size=max(batch_size, 1),
        estimated_exact_comparisons=exact_est,
        estimated_approx_comparisons=approx_est,
        estimated_resident_keywords=obs.keyword_total,
        observations=obs,
        reasons=reasons,
    )


def iter_batches(tickets: Sequence[Ticket], batch_size: int) -> List[List[Ticket]]:
    """确定性分批：保持工单原始顺序，不重排、不丢弃。"""
    return [
        list(tickets[start:start + batch_size])
        for start in range(0, len(tickets), batch_size)
    ]
