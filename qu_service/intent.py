"""阶段二：基于词表特征与规则权重的确定性意图打分。

得分 = Σ(命中特征权重 × 位置衰减) + 基础加分 + 规则表加分，
随后用 numpy 做 L2 归一化到 [0,1]。同分主意图按类目名称字典序。
全程无随机数、不依赖线程调度顺序。
"""
from __future__ import annotations

import numpy as np

from .errors import E_SCORE_OUT_OF_RANGE
from .hashing import fingerprint

STAGE = "intent"


def _decay(pos: int) -> float:
    """位置衰减：命中越靠前权重越高，确定性函数。"""
    return 1.0 / (1.0 + pos)


def score_intents(text: str, vocab: dict, rules: list[dict],
                  forced_scores: dict | None = None) -> dict:
    names = sorted(intent["name"] for intent in vocab["intents"])
    raw = {name: 0.0 for name in names}
    hits: dict[str, list] = {name: [] for name in names}

    for intent in vocab["intents"]:
        name = intent["name"]
        score = 0.0
        for feat in intent["features"]:
            pos = text.find(feat["term"])
            if pos >= 0:
                score += float(feat["weight"]) * _decay(pos)
                hits[name].append({"term": feat["term"], "pos": pos})
        if hits[name]:
            score += float(intent.get("base_boost", 0.0))
        raw[name] = score

    # 规则表加分：规则携带 intent_boost 且 pattern 命中时生效
    for rule in rules:
        boost = rule.get("intent_boost")
        if boost and boost.get("intent") in raw and rule.get("pattern", "") in text:
            raw[boost["intent"]] += float(boost["value"])

    # 注入：评分越界样例（强制写入原始分，越界则记录业务码并截断到 [0,1]）
    codes: list[str] = []
    if forced_scores:
        for name, value in forced_scores.items():
            if name not in raw:
                continue
            raw[name] = float(value)
            if value < 0.0 or value > 1.0:
                codes.append(E_SCORE_OUT_OF_RANGE)
                raw[name] = min(1.0, max(0.0, float(value)))

    vec = np.array([raw[name] for name in names], dtype=np.float64)
    norm = float(np.linalg.norm(vec))
    if norm > 0.0:
        vec = vec / norm
    scores = {name: round(float(val), 6) for name, val in zip(names, vec)}

    best = max(scores.values())
    # 同分时按类目名称字典序（names 已排序）
    primary = next(name for name in names if scores[name] == best)

    result = {
        "scores": scores,
        "primary_intent": primary,
        "no_signal": best == 0.0,
        "hits": {name: hits[name] for name in names if hits[name]},
        "codes": codes,
    }
    result["fingerprint"] = fingerprint(result)
    return result
