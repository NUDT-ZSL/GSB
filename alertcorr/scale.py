"""数据规模变化下的关联算法取舍。

按故障域评估: 单域事件数或最大依赖闭包规模超过配置阈值时,
该域从单流精确嵌套切换到按依赖距离分桶的近似关联。
切换记录实际模式、触发条件、被否决模式及原因;
近似模式在确定性影子样本上估计召回率并给出误差上界。
"""

from dataclasses import dataclass

from .correlate import correlate_approx, correlate_exact
from .model import sha256_text


@dataclass(frozen=True)
class ScaleConfig:
    cluster_window_ms: int = 60000
    causal_window_ms: int = 300000
    tolerance: int = 5000
    exact_max_domain_events: int = 500
    exact_max_closure: int = 12
    approx_sample_mod: int = 4
    shadow_mod: int = 4
    top_k: int = 5


def decide_modes(topology, events, cfg):
    """返回 {fault_domain: decision_dict}。"""
    domain_events = {}
    for ev in events:
        domain = topology.services[ev.service].fault_domain
        domain_events[domain] = domain_events.get(domain, 0) + 1
    closure_cache = {name: topology.upstream_closure(name)
                     for name in topology.services}
    domain_closure = {}
    for name, closure in closure_cache.items():
        domain = topology.services[name].fault_domain
        domain_closure[domain] = max(domain_closure.get(domain, 0), len(closure))

    decisions = {}
    for domain in sorted(set(list(domain_events) + list(domain_closure))):
        n_events = domain_events.get(domain, 0)
        max_closure = domain_closure.get(domain, 0)
        over_events = n_events > cfg.exact_max_domain_events
        over_closure = max_closure > cfg.exact_max_closure
        if over_events or over_closure:
            reasons = []
            if over_events:
                reasons.append(
                    "domain_events %d > threshold %d"
                    % (n_events, cfg.exact_max_domain_events))
            if over_closure:
                reasons.append(
                    "max_dependency_closure %d > threshold %d"
                    % (max_closure, cfg.exact_max_closure))
            decisions[domain] = {
                "domain": domain,
                "mode": "approx",
                "trigger": {
                    "domain_events": n_events,
                    "max_dependency_closure": max_closure,
                    "threshold_events": cfg.exact_max_domain_events,
                    "threshold_closure": cfg.exact_max_closure,
                },
                "rejected_mode": "exact",
                "rejected_reason": "; ".join(reasons),
            }
        else:
            decisions[domain] = {
                "domain": domain,
                "mode": "exact",
                "trigger": {
                    "domain_events": n_events,
                    "max_dependency_closure": max_closure,
                    "threshold_events": cfg.exact_max_domain_events,
                    "threshold_closure": cfg.exact_max_closure,
                },
                "rejected_mode": "approx",
                "rejected_reason": "within exact-mode thresholds; "
                                   "approximation unnecessary",
            }
    return decisions


def estimate_approx_quality(clusters, topology, cfg, approx_result,
                            approx_stats):
    """在确定性影子样本上重跑精确模式, 估计近似模式召回率与误差上界。

    误差上界: 被跳过的远桶候选最大可能得分(stats 解析上界)超出
    近似 top-k 最低得分的部分, 取影子样本最大值; 为 0 表示被跳过候选
    不可能进入 top-k。
    """
    active_ids = sorted(cid for cid, c in clusters.items()
                        if c["status"] == "active")
    shadow = [cid for cid in active_ids
              if int(sha256_text(cid)[:8], 16) % cfg.shadow_mod == 0]
    if not shadow:
        return {"shadow_size": 0, "recall_at_k": None, "error_bound": None,
                "error_bound_definition":
                    "max(0, skipped_score_upper_bound - topk_min_score) "
                    "over shadow sample"}
    exact_shadow = correlate_exact(clusters, topology, cfg.causal_window_ms,
                                   cfg.tolerance, cluster_ids=shadow)
    hits = 0
    total = 0
    skipped_upper = approx_stats.get("skipped_score_upper_bound", 0)
    max_bound = 0
    for cid in shadow:
        exact_top = [c["candidate_cluster_id"]
                     for c in exact_shadow[cid][:cfg.top_k]]
        approx_top = [c["candidate_cluster_id"]
                      for c in approx_result.get(cid, [])[:cfg.top_k]]
        hits += len(set(exact_top) & set(approx_top))
        total += len(exact_top)
        approx_list = approx_result.get(cid, [])
        if len(approx_list) >= cfg.top_k:
            topk_min = approx_list[cfg.top_k - 1]["score"]
            max_bound = max(max_bound,
                            max(0, skipped_upper - topk_min))
    recall = hits / total if total else None
    return {
        "shadow_size": len(shadow),
        "recall_at_k": recall,
        "recall_hits": hits,
        "recall_total": total,
        "error_bound": max_bound,
        "error_bound_definition":
            "max(0, skipped_score_upper_bound - topk_min_score) "
            "over shadow sample; 0 means skipped far-bucket candidates "
            "cannot enter top-k",
    }
