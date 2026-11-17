"""跨语言翻译桥接：扩展词项生成、置信度衰减与翻译抑制记录。"""


def build_translation_index(translation_pairs):
    """source -> 候选列表，按置信度降序、目标词字典序固定排序。"""
    index = {}
    for pair in translation_pairs:
        index.setdefault(pair["source"], []).append(
            (pair["target"], float(pair["confidence"]))
        )
    for candidates in index.values():
        candidates.sort(key=lambda item: (-item[1], item[0]))
    return index


def expand_query(tokens, translation_index, config):
    """为查询词项生成跨语言扩展。

    返回 (expansions, suppressed)：
    - expansions: 采纳的扩展词项（带来源词、置信度与衰减权重），去重；
    - suppressed: 因超过上限或目标重复而被抑制的候选，全部显式记录。
    原生词项与扩展词项通过 origin 字段严格区分，互不混淆。
    """
    bridge_config = config["bridge"]
    max_translations = bridge_config["max_translations_per_term"]
    decay = float(bridge_config["decay_weight"])

    expansions = []
    suppressed = []
    seen_targets = set()
    for term in tokens:
        candidates = translation_index.get(term, [])
        accepted_for_term = 0
        for rank, (target, confidence) in enumerate(candidates):
            record = {
                "source_term": term,
                "target_term": target,
                "origin": "translation",
                "confidence": round(confidence, 6),
                "weight": round(decay * confidence, 9),
            }
            if rank >= max_translations:
                suppressed.append(
                    {**record, "reason": "max_translations_exceeded"}
                )
                continue
            if target in seen_targets:
                suppressed.append({**record, "reason": "duplicate_target"})
                continue
            seen_targets.add(target)
            expansions.append(record)
            accepted_for_term += 1
    expansions.sort(key=lambda item: (item["source_term"], item["target_term"]))
    suppressed.sort(
        key=lambda item: (
            item["source_term"], item["target_term"], item["reason"]
        )
    )
    return expansions, suppressed
