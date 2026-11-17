"""窗口内重排与多样性惩罚，折减全程定点整数执行。"""

from .fixedpoint import FixedSpec, to_fixed


def _cosine(vec_a, vec_b):
    dot = sum(x * y for x, y in zip(vec_a, vec_b))
    norm_a = sum(x * x for x in vec_a)
    norm_b = sum(x * x for x in vec_b)
    if norm_a <= 0 or norm_b <= 0:
        return 0, None
    return dot, (norm_a, norm_b)


def group_similarity(codes_a, codes_b, groups, frac_bits):
    """分组定点余弦相似度，返回 (最大相似度整数, 命中分组名)。

    不同分组各自做整数余弦（量纲冲突的分组不会混在同一几何空间），再取
    最相似的一组。sqrt 仅用于把整数相似度换算回 [0, 2^F] 定点段。
    """
    one_sq = 1 << (2 * frac_bits)
    best = 0
    best_group = None
    for group in groups:
        idxs = group["indices"]
        vec_a = [codes_a[j] for j in idxs]
        vec_b = [codes_b[j] for j in idxs]
        dot, norms = _cosine(vec_a, vec_b)
        if norms is None:
            continue
        ratio_den = norms[0] * norms[1]
        if ratio_den <= 0 or dot <= 0:
            sim_q = 0
        else:
            sim_q = _isqrt((dot * dot * one_sq) // ratio_den)
        if sim_q > best:
            best, best_group = sim_q, group["name"]
    return best, best_group


def _isqrt(value):
    if value <= 0:
        return 0
    x = value
    y = (x + 1) // 2
    while y < x:
        x = y
        y = (x + value // x) // 2
    return x


def rerank_request(item_ids, codes, scores, groups, config, spec: FixedSpec,
                   threshold_override=None):
    """按融合分排序，窗口内贪心做多样性折减后重新插入。"""
    frac = spec.frac_bits
    one = 1 << frac
    penalty_q, _ = to_fixed(config["diversity_penalty"], frac, spec.rounding)
    threshold_value = config.get("similarity_threshold", 0.75)
    if threshold_override is not None:
        threshold_value = threshold_override
    threshold_q, _ = to_fixed(threshold_value, frac, spec.rounding)

    ordered = sorted(range(len(item_ids)), key=lambda i: (-scores[i], item_ids[i]))
    window = min(config["rerank_window"], len(ordered))
    window_idx = ordered[:window]
    outside = ordered[window:]

    penalties = []
    if config["diversity_penalty"] <= 0.0 or penalty_q == 0:
        final_order = ordered
    else:
        chosen = []
        remaining = list(window_idx)
        current_scores = dict(enumerate(scores))
        while remaining:
            remaining.sort(key=lambda i: (-current_scores[i], item_ids[i]))
            pick = remaining.pop(0)
            chosen.append(pick)
            for cand in list(remaining):
                sim_q, group_name = group_similarity(
                    codes[pick], codes[cand], groups, frac
                )
                if sim_q >= threshold_q:
                    deduction, _ = _div_exact(
                        current_scores[cand] * penalty_q, one, spec.rounding
                    )
                    before = current_scores[cand]
                    current_scores[cand] = before - deduction
                    penalties.append(
                        {
                            "selected_item": item_ids[pick],
                            "candidate_item": item_ids[cand],
                            "group": group_name,
                            "similarity": sim_q,
                            "threshold": threshold_q,
                            "deduction": deduction,
                            "score_before": before,
                            "score_after": current_scores[cand],
                        }
                    )
        scores = [current_scores.get(i, scores[i]) for i in range(len(scores))]
        # 窗口内外统一按折减后分数、物品 ID 决断，保证同分字典序全局一致
        final_order = sorted(chosen + outside, key=lambda i: (-scores[i], item_ids[i]))

    ranking = [
        {"rank": rank + 1, "item_id": item_ids[i], "score": scores[i]}
        for rank, i in enumerate(final_order)
    ]
    return {
        "order": final_order,
        "ranking": ranking,
        "penalties": penalties,
        "penalty_q": penalty_q,
        "threshold_q": threshold_q,
        "window": window,
    }


def _div_exact(num, den, rounding):
    from .fixedpoint import round_div

    return round_div(num, den, rounding)
