"""确定性意图判定核心：精确路径与字符签名近似候选路径。

两条路径共用同一套关键词排序、贪心最长优先扫描与平局规则，
差异仅在候选关键词集合的来源；近似路径额外记录每个剪枝条件
    剪掉的关键词数量与桶内候选数。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .models import IntentConfig, Ticket, canonical_json, sha256_hex


@dataclass(frozen=True)
class KeywordEntry:
    label: str
    keyword: str
    length: int
    first_char: str


@dataclass(frozen=True)
class Hit:
    label: str
    keyword: str
    start: int
    end: int

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "keyword": self.keyword,
            "start": self.start,
            "end": self.end,
        }


@dataclass
class PruneStats:
    """近似路径每个剪枝条件的可观测统计（按工单记录）。"""

    total_keywords: int = 0
    pruned_by_length: int = 0
    pruned_by_first_char: int = 0
    actual_comparisons: int = 0
    bucket_observations: List[Dict[str, int]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "total_keywords": self.total_keywords,
            "pruned_by_length": self.pruned_by_length,
            "pruned_by_first_char": self.pruned_by_first_char,
            "actual_comparisons": self.actual_comparisons,
            "bucket_observations": [dict(item) for item in self.bucket_observations],
        }


@dataclass
class MatchResult:
    ticket_id: str
    intent_label: str
    confidence: float
    hits: List[Hit]
    path: str
    prune_stats: Optional[PruneStats]
    comparisons: int
    fallback_category: Optional[str]
    fallback_from_label: Optional[str]
    cache_key: str
    rule_version: str
    threshold_version: str
    config_version: int
    retired_labels: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ticket_id": self.ticket_id,
            "intent_label": self.intent_label,
            "confidence": round(self.confidence, 6),
            "hits": [hit.to_dict() for hit in self.hits],
            "path": self.path,
            "prune_stats": None if self.prune_stats is None else self.prune_stats.to_dict(),
            "comparisons": self.comparisons,
            "fallback_category": self.fallback_category,
            "fallback_from_label": self.fallback_from_label,
            "cache_key": self.cache_key,
            "rule_version": self.rule_version,
            "threshold_version": self.threshold_version,
            "config_version": self.config_version,
            "retired_labels": list(self.retired_labels),
        }


def build_keyword_entries(config: IntentConfig) -> List[KeywordEntry]:
    """构建去重、确定性排序的关键词表。

    排序口径：关键词长度降序 -> 意图标签字典序 -> 关键词字典序。
    """
    pairs = sorted(
        {(r.label, kw) for r in config.rules for kw in r.keywords if kw != ""},
        key=lambda pair: (-len(pair[1]), pair[0], pair[1]),
    )
    return [
        KeywordEntry(label=label, keyword=kw, length=len(kw), first_char=kw[0])
        for label, kw in pairs
    ]


def build_signature_buckets(entries: Sequence[KeywordEntry]) -> Dict[str, List[KeywordEntry]]:
    """按首字符码点签名分桶，桶内保持全局最长优先顺序。"""
    buckets: Dict[str, List[KeywordEntry]] = {}
    for entry in entries:
        buckets.setdefault(f"U+{ord(entry.first_char):04X}", []).append(entry)
    for bucket_id in buckets:
        buckets[bucket_id].sort(
            key=lambda entry: (-entry.length, entry.label, entry.keyword)
        )
    return dict(sorted(buckets.items(), key=lambda item: item[0]))


def _scan_with_candidates(
    query: str,
    candidate_provider,
    force_skip: Optional[Tuple[str, str]] = None,
) -> Tuple[List[Hit], int]:
    """共享贪心最长优先扫描，候选集合由 provider 按位置给出。

    精确路径每个位置提供全量关键词；近似路径提供剪枝后的桶内候选。
    候选顺序均为全局统一的（长度降序、标签字典序、关键词字典序）。
    """
    hits: List[Hit] = []
    comparisons = 0
    pos = 0
    query_len = len(query)
    while pos < query_len:
        candidates = candidate_provider(pos, query_len)
        matched: Optional[Hit] = None
        for entry in candidates:
            if force_skip is not None and (entry.label, entry.keyword) == force_skip:
                comparisons += 1
                continue
            comparisons += 1
            if query.startswith(entry.keyword, pos):
                matched = Hit(
                    label=entry.label,
                    keyword=entry.keyword,
                    start=pos,
                    end=pos + entry.length,
                )
                break
        if matched is None:
            pos += 1
        else:
            hits.append(matched)
            pos = matched.end
    return hits, comparisons


def longest_first_scan(
    query: str,
    entries: Sequence[KeywordEntry],
    force_skip: Optional[Tuple[str, str]] = None,
) -> Tuple[List[Hit], int]:
    """精确路径使用的全量关键词扫描入口。"""
    return _scan_with_candidates(
        query, lambda _pos, _limit: entries, force_skip=force_skip
    )


def _rank_intents(
    hits: Sequence[Hit], config: IntentConfig
) -> Optional[Tuple[str, float, Dict[str, Dict[str, object]]]]:
    """按命中聚合打分并执行确定性平局规则。"""
    rule_by_label = config.rule_map()
    agg: Dict[str, Dict[str, object]] = {}
    for hit in hits:
        rule = rule_by_label.get(hit.label)
        weight = rule.weight if rule is not None else 1
        bucket = agg.setdefault(
            hit.label,
            {"count": 0, "length_sum": 0, "max_len": 0, "weight": weight},
        )
        bucket["count"] = int(bucket["count"]) + 1
        bucket["length_sum"] = int(bucket["length_sum"]) + len(hit.keyword)
        bucket["max_len"] = max(int(bucket["max_len"]), len(hit.keyword))
    if not agg:
        return None

    scored: List[Tuple[int, int, int, str, float]] = []
    for label, bucket in agg.items():
        count = int(bucket["count"])
        length_sum = int(bucket["length_sum"])
        max_len = int(bucket["max_len"])
        weight = int(bucket["weight"])
        score = count * weight + length_sum
        confidence = _confidence(count, length_sum, max_len, weight)
        # 排序键：score 降序 -> 最长命中关键词长度降序 -> 标签字典序
        scored.append((-score, -max_len, label, 0.0, confidence))
    scored.sort(key=lambda item: (item[0], item[1], item[2]))
    best = scored[0]
    winner = best[2]
    winner_confidence = best[4]
    return winner, winner_confidence, agg


def _confidence(count: int, length_sum: int, max_len: int, weight: int) -> float:
    """确定性置信度：命中次数/长度/权重的有界组合。"""
    raw = (count * weight + length_sum + max_len) / (count * weight + length_sum + max_len + 8.0)
    return round(float(raw), 6)


def make_cache_key(
    ticket: Ticket,
    config: IntentConfig,
    path: str,
) -> str:
    """内容寻址基础缓存键：仅绑定查询文本、标注与判定路径。

    规则版本、门限版本不进入键体，而作为 lineage 元数据随产物保存；
    关键词/门限变化由增量计划基于受影响规则精确失效，从而让
    无关工单跨配置版本复用同一内容产物。
    """
    payload = canonical_json(
        {
            "query_text": ticket.query_text,
            "gold": ticket.intent_label_gold,
            "path": path,
        }
    )
    return sha256_hex(payload)


def _resolve_label(
    winner: Optional[str],
    confidence: float,
    hits: List[Hit],
    config: IntentConfig,
    disable_fallback: bool = False,
) -> Tuple[str, Optional[str], Optional[str], List[str]]:
    """处理置信门限回退与受控集合收缩回退。

    返回 (最终意图, 回退类别, 回退前意图, 命中中已退休标签列表)。
    """
    rule_by_label = config.rule_map()
    controlled = set(config.controlled_labels)
    retired = sorted({hit.label for hit in hits if hit.label not in controlled})

    chosen = winner
    fallback_category: Optional[str] = None
    fallback_from: Optional[str] = None

    if chosen is not None and chosen not in controlled:
        fallback_from = chosen
        rule = rule_by_label.get(chosen)
        target = rule.fallback_label if rule and rule.fallback_label in controlled else None
        if (
            target is None
            and not disable_fallback
            and config.default_fallback_label in controlled
        ):
            target = config.default_fallback_label
        if target is None:
            from .errors import BizError

            raise BizError(
                "E_LABEL_RETIRED",
                "controlled_labels",
                {"retired_label": chosen, "ticket_hits": [hit.to_dict() for hit in hits]},
            )
        chosen = target
        fallback_category = "retired_label"
        return chosen, fallback_category, fallback_from, retired

    if chosen is None:
        if disable_fallback or config.default_fallback_label not in controlled:
            from .errors import BizError

            raise BizError(
                "E_LABEL_RETIRED",
                "default_fallback_label",
                {"reason": "no_fallback_available"},
            )
        chosen = config.default_fallback_label
        fallback_category = "no_keyword_hit"
        return chosen, fallback_category, fallback_from, retired

    rule = rule_by_label[chosen]
    if confidence < rule.threshold:
        fallback_from = chosen
        target = rule.fallback_label if rule.fallback_label in controlled else None
        if target is None and not disable_fallback:
            target = config.default_fallback_label
        if target is None or disable_fallback:
            from .errors import BizError

            raise BizError(
                "E_LABEL_RETIRED",
                "fallback_label",
                {"from_label": fallback_from, "reason": "no_fallback_available"},
            )
        chosen = target
        fallback_category = "below_threshold"
    return chosen, fallback_category, fallback_from, retired


def match_exact(
    ticket: Ticket,
    config: IntentConfig,
    entries: Optional[Sequence[KeywordEntry]] = None,
    force_skip: Optional[Tuple[str, str]] = None,
    disable_fallback: bool = False,
) -> MatchResult:
    """精确路径：对全部关键词逐一比对。"""
    keyword_entries = entries if entries is not None else build_keyword_entries(config)
    hits, comparisons = longest_first_scan(
        ticket.query_text, keyword_entries, force_skip=force_skip
    )
    ranked = _rank_intents(hits, config)
    if ranked is None:
        winner, confidence = None, 0.0
    else:
        winner, confidence, _ = ranked
    final_label, fb_category, fb_from, retired = _resolve_label(
        winner, confidence, hits, config, disable_fallback=disable_fallback
    )
    return MatchResult(
        ticket_id=ticket.ticket_id,
        intent_label=final_label,
        confidence=confidence,
        hits=hits,
        path="exact",
        prune_stats=None,
        comparisons=comparisons,
        fallback_category=fb_category,
        fallback_from_label=fb_from,
        cache_key=make_cache_key(ticket, config, "exact"),
        rule_version=config.rule_version,
        threshold_version=config.threshold_version,
        config_version=config.version,
        retired_labels=retired,
    )


def match_approximate(
    ticket: Ticket,
    config: IntentConfig,
    entries: Optional[Sequence[KeywordEntry]] = None,
    buckets: Optional[Dict[str, List[KeywordEntry]]] = None,
    force_skip: Optional[Tuple[str, str]] = None,
    disable_fallback: bool = False,
) -> MatchResult:
    """近似候选路径：首字符签名分桶 + 长度剪枝后再完整匹配。"""
    keyword_entries = entries if entries is not None else build_keyword_entries(config)
    signature_buckets = (
        buckets if buckets is not None else build_signature_buckets(keyword_entries)
    )
    query = ticket.query_text
    query_len = len(query)
    stats = PruneStats(total_keywords=len(keyword_entries))

    def provider(pos: int, limit: int) -> List[KeywordEntry]:
        bucket_id = f"U+{ord(query[pos]):04X}"
        bucket = signature_buckets.get(bucket_id, [])
        length_pruned = sum(1 for entry in bucket if entry.length > query_len - pos)
        candidates = [entry for entry in bucket if entry.length <= query_len - pos]
        stats.pruned_by_length += length_pruned
        stats.pruned_by_first_char += len(keyword_entries) - len(bucket)
        stats.bucket_observations.append(
            {
                "pos": pos,
                "bucket_id": bucket_id,
                "bucket_size": len(bucket),
                "bucket_candidates": len(candidates),
                "pruned_by_length": length_pruned,
                "pruned_by_first_char": len(keyword_entries) - len(bucket),
            }
        )
        return candidates

    all_hits, total_comparisons = _scan_with_candidates(
        query, provider, force_skip=force_skip
    )
    stats.actual_comparisons = total_comparisons
    ranked = _rank_intents(all_hits, config)
    if ranked is None:
        winner, confidence = None, 0.0
    else:
        winner, confidence, _ = ranked
    final_label, fb_category, fb_from, retired = _resolve_label(
        winner, confidence, all_hits, config, disable_fallback=disable_fallback
    )
    return MatchResult(
        ticket_id=ticket.ticket_id,
        intent_label=final_label,
        confidence=confidence,
        hits=all_hits,
        path="approximate",
        prune_stats=stats,
        comparisons=total_comparisons,
        fallback_category=fb_category,
        fallback_from_label=fb_from,
        cache_key=make_cache_key(ticket, config, "approximate"),
        rule_version=config.rule_version,
        threshold_version=config.threshold_version,
        config_version=config.version,
        retired_labels=retired,
    )
