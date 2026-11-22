"""内容寻址缓存与增量失效传播。

缓存产物以 sha256 内容键落盘；关键词增删、门限调整、标注修正都会
改变对应工单的缓存键，从而精确计算受影响集合，复用未受影响产物。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .matcher import (
    Hit,
    MatchResult,
    build_keyword_entries,
    build_signature_buckets,
    longest_first_scan,
    _rank_intents,
    _resolve_label,
    make_cache_key,
    match_approximate,
    match_exact,
)
from .models import IntentConfig, Ticket, canonical_json, sha256_hex


class ContentStore:
    """本地内容寻址存储，路径与读写均使用 UTF-8、无系统时间依赖。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def has(self, key: str) -> bool:
        return self._path(key).exists()

    def put(self, key: str, payload: Dict[str, Any]) -> Path:
        target = self._path(key)
        target.write_text(
            canonical_json(payload) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return target

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        target = self._path(key)
        if not target.exists():
            return None
        return json.loads(target.read_text(encoding="utf-8"))

    def keys(self) -> List[str]:
        return sorted(path.stem for path in self.root.glob("*.json"))


@dataclass
class IncrementalPlan:
    recompute_ticket_ids: List[str] = field(default_factory=list)
    reused_ticket_ids: List[str] = field(default_factory=list)
    affected_rule_labels: List[str] = field(default_factory=list)
    key_changes: List[Dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "recompute_ticket_ids": list(self.recompute_ticket_ids),
            "reused_ticket_ids": list(self.reused_ticket_ids),
            "affected_rule_labels": list(self.affected_rule_labels),
            "key_changes": [dict(item) for item in self.key_changes],
        }


def result_cache_key(result: MatchResult) -> str:
    return result.cache_key


def plan_incremental(
    old_tickets: Sequence[Ticket],
    new_tickets: Sequence[Ticket],
    old_config: IntentConfig,
    new_config: IntentConfig,
    path: str,
    store: ContentStore,
    extra_invalid_ticket_ids: Optional[Sequence[str]] = None,
) -> IncrementalPlan:
    """对比新旧缓存键，得到重算清单、复用清单与受影响规则集合。"""
    old_by_id = {ticket.ticket_id: ticket for ticket in old_tickets}
    new_by_id = {ticket.ticket_id: ticket for ticket in new_tickets}
    plan = IncrementalPlan()
    extra = set(extra_invalid_ticket_ids or [])

    for ticket_id in sorted(new_by_id):
        new_ticket = new_by_id[ticket_id]
        new_key = make_cache_key(new_ticket, new_config, path)
        old_ticket = old_by_id.get(ticket_id)
        old_key = (
            make_cache_key(old_ticket, old_config, path)
            if old_ticket is not None
            else None
        )
        if old_key != new_key or ticket_id in extra or not store.has(new_key):
            plan.recompute_ticket_ids.append(ticket_id)
            if old_key != new_key:
                plan.key_changes.append(
                    {
                        "ticket_id": ticket_id,
                        "old_key": old_key or "",
                        "new_key": new_key,
                    }
                )
        else:
            plan.reused_ticket_ids.append(ticket_id)

    plan.affected_rule_labels = _affected_rules(old_config, new_config)
    return plan


def tickets_hitting_rules(
    tickets: Sequence[Ticket],
    config: IntentConfig,
    rule_labels: Sequence[str],
) -> List[str]:
    """返回查询文本中包含任一受影响规则关键词的工单 ID（确定性排序）。"""
    target = set(rule_labels)
    keywords_by_label = {
        rule.label: tuple(sorted(set(rule.keywords)))
        for rule in config.rules
        if rule.label in target
    }
    hit_ids: List[str] = []
    for ticket in tickets:
        if any(
            keyword in ticket.query_text
            for keywords in keywords_by_label.values()
            for keyword in keywords
        ):
            hit_ids.append(ticket.ticket_id)
    return sorted(hit_ids)


def _affected_rules(old_config: IntentConfig, new_config: IntentConfig) -> List[str]:
    labels = sorted(
        {rule.label for rule in old_config.rules}
        | {rule.label for rule in new_config.rules}
    )
    old_map = old_config.rule_map()
    new_map = new_config.rule_map()
    affected: List[str] = []
    for label in labels:
        old_rule = old_map.get(label)
        new_rule = new_map.get(label)
        if old_rule is None or new_rule is None:
            affected.append(label)
            continue
        if (
            sorted(set(old_rule.keywords)) != sorted(set(new_rule.keywords))
            or old_rule.threshold != new_rule.threshold
            or old_rule.weight != new_rule.weight
            or old_rule.fallback_label != new_rule.fallback_label
        ):
            affected.append(label)
    return affected


def run_incremental(
    tickets: Sequence[Ticket],
    config: IntentConfig,
    path: str,
    plan: IncrementalPlan,
    store: ContentStore,
) -> Tuple[List[MatchResult], List[MatchResult]]:
    """按增量计划重算受影响工单、复用未受影响工单。

    返回 (全量有序结果, 本次实际重算结果)。
    """
    recompute_ids = set(plan.recompute_ticket_ids)
    all_results: List[MatchResult] = []
    recomputed: List[MatchResult] = []
    for ticket in tickets:
        key = make_cache_key(ticket, config, path)
        if ticket.ticket_id in recompute_ids or not store.has(key):
            result = _compute(ticket, config, path)
            store.put(key, _artifact_from_result(result))
            recomputed.append(result)
        else:
            payload = store.get(key)
            if payload is None:
                result = _compute(ticket, config, path)
                store.put(key, _artifact_from_result(result))
                recomputed.append(result)
            else:
                result = _result_from_cache(ticket, payload, path, config)
        all_results.append(result)
    return all_results, recomputed


def _artifact_from_result(result: MatchResult) -> Dict[str, Any]:
    """落盘只保存与配置版本无关的匹配事实，lineage 在加载时按当前配置重建。"""
    return {
        "ticket_id": result.ticket_id,
        "path": result.path,
        "confidence": round(result.confidence, 6),
        "hits": [hit.to_dict() for hit in result.hits],
        "prune_stats": None if result.prune_stats is None else result.prune_stats.to_dict(),
    }


def _compute(ticket: Ticket, config: IntentConfig, path: str) -> MatchResult:
    if path == "exact":
        return match_exact(ticket, config)
    if path == "approximate":
        return match_approximate(ticket, config)
    raise ValueError(f"unknown path: {path}")


def _result_from_cache(
    ticket: Ticket,
    payload: Dict[str, Any],
    path: str,
    config: IntentConfig,
) -> MatchResult:
    hits = [Hit(**hit) for hit in payload["hits"]]
    ranked = _rank_intents(hits, config)
    if ranked is None:
        winner, confidence = None, 0.0
    else:
        winner, confidence, _ = ranked
    final_label, fb_category, fb_from, retired = _resolve_label(
        winner, confidence, hits, config
    )
    if path == "exact":
        comparisons = _estimate_comparisons(ticket, config, path)
        prune = None
    else:
        # 近似路径的剪枝统计与比较次数依赖当前分桶，复用命中事实但重算口径
        fresh = match_approximate(ticket, config)
        comparisons = fresh.comparisons
        prune = fresh.prune_stats
    return MatchResult(
        ticket_id=ticket.ticket_id,
        intent_label=final_label,
        confidence=confidence,
        hits=hits,
        path=payload["path"],
        prune_stats=prune,
        comparisons=comparisons,
        fallback_category=fb_category,
        fallback_from_label=fb_from,
        cache_key=make_cache_key(ticket, config, path),
        rule_version=config.rule_version,
        threshold_version=config.threshold_version,
        config_version=config.version,
        retired_labels=retired,
    )


def _estimate_comparisons(ticket: Ticket, config: IntentConfig, path: str) -> int:
    """复用加载时按当前关键词表重算比较次数，保证跨版本结果逐字节一致。"""
    entries = build_keyword_entries(config)
    if path == "exact":
        _, comparisons = longest_first_scan(ticket.query_text, entries)
        return comparisons
    buckets = build_signature_buckets(entries)
    total = 0
    pos = 0
    query = ticket.query_text
    while pos < len(query):
        bucket = buckets.get(f"U+{ord(query[pos]):04X}", [])
        candidates = [entry for entry in bucket if entry.length <= len(query) - pos]
        _, comparisons = longest_first_scan(query, candidates)
        total += comparisons
        pos += 1
    return total


def fingerprint_results(results: Sequence[MatchResult]) -> str:
    """对结果集合做逐字节指纹，用于全量/增量一致性证明。"""
    payload = canonical_json([result.to_dict() for result in results])
    return sha256_hex(payload)
