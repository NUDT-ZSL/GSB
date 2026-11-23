"""重要性打分与记忆预算裁剪。"""

import numpy as np

# 权重：覆盖 token 数、来源轮次跨度、摘要置信度
WEIGHTS = np.array([1.0, 0.5, 10.0], dtype=np.float64)


def score_entry(entry, coverage_tokens):
    """返回 (score, contributions)；总分可由贡献明细独立重算一致。"""
    conf_mean = (entry.conf_lo + entry.conf_hi) / 2.0
    turn_span = entry.turn_end - entry.turn_start + 1
    features = np.array([coverage_tokens, turn_span, conf_mean], dtype=np.float64)
    contributions = {
        "coverage_tokens": round(float(WEIGHTS[0] * features[0]), 6),
        "turn_span": round(float(WEIGHTS[1] * features[1]), 6),
        "confidence": round(float(WEIGHTS[2] * features[2]), 6),
    }
    score = round(float(np.dot(WEIGHTS, features)), 6)
    return score, contributions


def rescore_from_contributions(contributions):
    return round(sum(contributions.values()), 6)


def score_entries(entries, sessions):
    """为每个条目打分并记录贡献明细，返回 {entry_id: {...}}。"""
    coverage = {}
    for session in sessions.values():
        per_turn = {t["turn_no"]: t["token_len"] for t in session["turns"]}
        coverage[session["session_id"]] = per_turn
    report = {}
    for entry in entries:
        per_turn = coverage[entry.session_id]
        covered = sum(per_turn.get(t, 0) for t in entry.source_turns)
        score, contributions = score_entry(entry, covered)
        report[entry.entry_id] = {
            "entry_id": entry.entry_id,
            "session_id": entry.session_id,
            "coverage_tokens": covered,
            "contributions": contributions,
            "score": score,
            "recomputed": rescore_from_contributions(contributions),
        }
    return report


def prune_to_budget(entries, score_report, budget_tokens):
    """按重要性降序裁剪，同分按条目序号升序裁决；保留可核对轨迹。"""
    ranked = sorted(
        entries,
        key=lambda e: (-score_report[e.entry_id]["score"], e.entry_id),
    )
    kept, pruned = [], []
    used = 0
    for entry in ranked:
        cost = len(entry.summary_tokens)
        if used + cost <= budget_tokens:
            used += cost
            kept.append(
                {
                    "entry_id": entry.entry_id,
                    "score": score_report[entry.entry_id]["score"],
                    "summary_cost": cost,
                    "cumulative_cost": used,
                }
            )
        else:
            pruned.append(
                {
                    "entry_id": entry.entry_id,
                    "score": score_report[entry.entry_id]["score"],
                    "summary_cost": cost,
                    "reason": "budget_exceeded",
                    "cumulative_cost_at_reject": used,
                }
            )
    return {"budget_tokens": budget_tokens, "used_tokens": used, "kept": kept, "pruned": pruned}
