from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np

from .hashing import sha256_json


@dataclass(frozen=True)
class VocabTerm:
    term: str
    category: str
    weight: float


CONTROLLED_VOCAB: tuple[VocabTerm, ...] = (
    VocabTerm("crimson", "color", 1.10),
    VocabTerm("azure", "color", 1.08),
    VocabTerm("emerald", "color", 1.08),
    VocabTerm("amber", "color", 1.02),
    VocabTerm("violet", "color", 1.05),
    VocabTerm("ivory", "color", 0.95),
    VocabTerm("obsidian", "color", 1.12),
    VocabTerm("绯红", "color", 1.10),
    VocabTerm("湛蓝", "color", 1.08),
    VocabTerm("翠绿", "color", 1.08),
    VocabTerm("琥珀", "color", 1.02),
    VocabTerm("紫罗兰", "color", 1.05),
    VocabTerm("象牙白", "color", 0.95),
    VocabTerm("曜石黑", "color", 1.12),
    VocabTerm("grid", "pattern", 1.15),
    VocabTerm("lattice", "pattern", 1.12),
    VocabTerm("waves", "pattern", 1.08),
    VocabTerm("blocks", "pattern", 1.18),
    VocabTerm("diagonal", "pattern", 1.04),
    VocabTerm("concentric", "pattern", 1.06),
    VocabTerm("网格", "pattern", 1.15),
    VocabTerm("晶格", "pattern", 1.12),
    VocabTerm("波浪", "pattern", 1.08),
    VocabTerm("块面", "pattern", 1.18),
    VocabTerm("对角", "pattern", 1.04),
    VocabTerm("同心", "pattern", 1.06),
    VocabTerm("city", "scene", 1.00),
    VocabTerm("forest", "scene", 1.02),
    VocabTerm("ocean", "scene", 1.03),
    VocabTerm("desert", "scene", 1.01),
    VocabTerm("studio", "scene", 0.98),
    VocabTerm("skyline", "scene", 1.05),
    VocabTerm("城市", "scene", 1.00),
    VocabTerm("森林", "scene", 1.02),
    VocabTerm("海洋", "scene", 1.03),
    VocabTerm("沙漠", "scene", 1.01),
    VocabTerm("摄影棚", "scene", 0.98),
    VocabTerm("天际线", "scene", 1.05),
    VocabTerm("soft", "mood", 0.96),
    VocabTerm("sharp", "mood", 1.04),
    VocabTerm("glowing", "mood", 1.08),
    VocabTerm("muted", "mood", 0.97),
    VocabTerm("vivid", "mood", 1.06),
    VocabTerm("柔和", "mood", 0.96),
    VocabTerm("锐利", "mood", 1.04),
    VocabTerm("发光", "mood", 1.08),
    VocabTerm("低饱和", "mood", 0.97),
    VocabTerm("鲜艳", "mood", 1.06),
)


def count_terms(text: str, prompt: str = "") -> tuple[dict[str, int], int]:
    combined = f"{text}\n{prompt}".lower()
    counts: dict[str, int] = {}
    total = 0
    for item in CONTROLLED_VOCAB:
        term = item.term.lower()
        if term.isascii():
            pattern = rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])"
            count = len(re.findall(pattern, combined))
        else:
            count = combined.count(term)
        if count:
            counts[item.term] = count
            total += count
    return counts, total


def condition_vector(text: str, prompt: str, condition_config: dict) -> tuple[np.ndarray, dict]:
    """Weighted mean pooling of deterministic controlled-vocab embeddings."""
    dim = int(condition_config["dim"])
    seed = int(condition_config["vocab_seed"])
    scale = float(condition_config["embedding_scale"])
    rng = np.random.default_rng(seed)
    embeddings = rng.normal(0.0, scale, size=(len(CONTROLLED_VOCAB), dim)).astype(np.float64)

    counts, total_terms = count_terms(text, prompt)
    weighted_sum = np.zeros(dim, dtype=np.float64)
    weight_total = 0.0
    term_records = []
    for index, item in enumerate(CONTROLLED_VOCAB):
        count = counts.get(item.term, 0)
        if count:
            weight = float(count) * item.weight
            weighted_sum += weight * embeddings[index]
            weight_total += weight
            term_records.append({"term": item.term, "count": count, "weight": item.weight})

    if weight_total <= 0.0:
        # A deterministic neutral condition keeps malformed/uncontrolled text
        # reproducible while still allowing the task to be diagnosed.
        vector = np.zeros(dim, dtype=np.float64)
        normalization = "neutral_zero_no_controlled_terms"
    else:
        vector = weighted_sum / weight_total
        norm = float(np.linalg.norm(vector))
        if condition_config.get("normalize") == "weighted_mean_l2" and norm > 1e-12:
            vector = vector / norm
        normalization = condition_config.get("normalize", "weighted_mean")

    record = {
        "controlled_vocab_size": len(CONTROLLED_VOCAB),
        "matched_terms": term_records,
        "matched_token_count": total_terms,
        "weight_total": weight_total,
        "normalization": normalization,
        "vector_checksum": sha256_json([round(float(v), 12) for v in vector]),
    }
    return vector, record
