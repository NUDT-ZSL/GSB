"""单语/跨语言检索、融合打分、稳定归一化与相关等级判定。"""

from .bridge import expand_query

STAGE = "retrieval"


def _stable_normalize(values):
    """min-max 归一化；常量集合统一归一为 0.0，避免除零且结果确定。"""
    if not values:
        return {}
    low = min(values.values())
    high = max(values.values())
    if high == low:
        return {key: 0.0 for key in values}
    span = high - low
    return {key: round((value - low) / span, 9) for key, value in values.items()}


def _predict_grade(score, thresholds):
    if score >= thresholds["grade3"]:
        return 3
    if score >= thresholds["grade2"]:
        return 2
    if score >= thresholds["grade1"]:
        return 1
    return 0


def score_query(query, multi_index, translation_index, config):
    """处理单个查询，返回完整的排序结果与分项分解。"""
    language = query["detected_lang"]
    index = multi_index.get(language)
    bm25 = config["bm25"]
    native_terms = sorted(set(query["tokens"]))

    # 单语检索：原生词项在同语言索引上的 BM25 得分
    mono_detail, mono_totals = index.bm25_scores(
        native_terms, bm25["k1"], bm25["b"]
    )

    # 跨语言桥接：扩展词项在“另一语言”索引上检索
    expansions, suppressed = expand_query(
        query["tokens"], translation_index, config
    )
    other_languages = [
        lang for lang in config["languages"]["allowed"] if lang != language
    ]
    cross_totals = {}
    cross_detail = {}
    cross_hits = {}
    for expansion in expansions:
        target = expansion["target_term"]
        weight = expansion["weight"]
        for other in other_languages:
            other_index = multi_index.get(other)
            detail, totals = other_index.bm25_scores(
                [target], bm25["k1"], bm25["b"]
            )
            for doc_id, raw_score in totals.items():
                weighted = round(raw_score * weight, 9)
                cross_totals[doc_id] = round(
                    cross_totals.get(doc_id, 0.0) + weighted, 9
                )
                cross_detail.setdefault(doc_id, []).append({
                    "term": target,
                    "source_term": expansion["source_term"],
                    "origin": "translation",
                    "bm25": raw_score,
                    "weight": weight,
                    "weighted_score": weighted,
                })
                cross_hits.setdefault(doc_id, set()).add(target)

    candidate_ids = set(mono_totals) | set(cross_totals)
    mono_norm = _stable_normalize(
        {doc_id: mono_totals.get(doc_id, 0.0) for doc_id in candidate_ids}
    )
    cross_norm = _stable_normalize(
        {doc_id: cross_totals.get(doc_id, 0.0) for doc_id in candidate_ids}
    )

    fusion = config["fusion"]
    mono_weight = fusion["mono_weight"]
    cross_weight = fusion["cross_weight"]
    weight_sum = mono_weight + cross_weight
    ranked = []
    for doc_id in candidate_ids:
        fused_raw = (
            mono_weight * mono_norm[doc_id]
            + cross_weight * cross_norm[doc_id]
        ) / weight_sum
        ranked.append({
            "doc_id": doc_id,
            "mono_bm25": round(mono_totals.get(doc_id, 0.0), 9),
            "cross_bm25": round(cross_totals.get(doc_id, 0.0), 9),
            "mono_normalized": mono_norm[doc_id],
            "cross_normalized": cross_norm[doc_id],
            "fused_score": round(fused_raw, 9),
            "predicted_grade": 0,
            "native_hits": sorted(mono_detail.get(doc_id, {}).keys()),
            "native_term_scores": mono_detail.get(doc_id, {}),
            "cross_hits": sorted(cross_hits.get(doc_id, set())),
            "cross_term_scores": sorted(
                cross_detail.get(doc_id, []),
                key=lambda item: (item["source_term"], item["term"]),
            ),
        })

    # 确定性排序：融合分降序 -> 单语原始分降序 -> 文档 ID 字典序
    ranked.sort(key=lambda item: (
        -item["fused_score"], -item["mono_bm25"], item["doc_id"]
    ))
    for item in ranked:
        item["predicted_grade"] = _predict_grade(
            item["fused_score"], fusion["grade_thresholds"]
        )

    top_k = config["retrieval"]["top_k"]
    return {
        "query_id": query["query_id"],
        "detected_lang": language,
        "declared_lang": query["declared_lang"],
        "sort_key": f"{STAGE}:{query['query_id']}",
        "native_terms": native_terms,
        "expansion_terms": expansions,
        "suppressed_translations": suppressed,
        # 报告仅展示 top_k；全量排序结果单独保留，供评估覆盖全部真值文档
        "candidates": ranked[:top_k],
        "all_candidates": ranked,
        "total_candidates": len(ranked),
    }
