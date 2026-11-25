"""四路召回共享的索引结构。

索引按精度档构建：浮点档保存 float64 量，定点档保存量化整数，
后续召回代码只通过算子访问，保证两条路径算子语义一致。

四类结构：

- 倒排索引：term -> [(doc_id, 词项权重 / sqrt(doc_len))]
- 前缀索引：prefix -> [(doc_id, 命中词项数)]
- 类别热度：category -> 热度分（类别内文档平均长度的确定性归一）
- 向量索引：L2 归一化后的文档向量，余弦相似度退化为点积
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import PipelineConfig
from .precision import Ops
from .samplegen import term_prefix


@dataclass
class SearchIndex:
    """绑定精度算子的内存索引。"""

    config: PipelineConfig
    ops: Ops
    doc_ids: tuple[str, ...]
    doc_lengths: dict[str, int]
    doc_categories: dict[str, int]
    inverted: dict[int, list[tuple[str, Any]]]
    prefix: dict[int, list[tuple[str, Any]]]
    category_hotness: dict[int, Any]
    doc_vectors: dict[str, tuple[Any, ...]]
    query_vectors: dict[str, tuple[Any, ...]]


def _query_vector(
    query_terms: list[int],
    term_weights: dict[str, float],
    embed_dim: int,
) -> list[float]:
    """查询向量 = 命中词项的确定性伪向量均值（仅依赖词项与种子表）。"""
    import numpy as np

    vectors = []
    for term_id in query_terms:
        term_rng = np.random.default_rng(10_000 + term_id)
        vectors.append(term_rng.normal(0.0, 1.0, size=embed_dim))
    vec = np.mean(np.stack(vectors), axis=0)
    norm = float(np.linalg.norm(vec))
    if norm > 0:
        vec = vec / norm
    return [float(x) for x in vec]


def build_index(
    docs: list[dict],
    queries: list[dict],
    term_weights: dict[str, float],
    config: PipelineConfig,
    ops: Ops,
) -> SearchIndex:
    """根据输入与精度算子构建确定性索引。"""
    doc_ids = tuple(doc["doc_id"] for doc in docs)
    doc_categories = {doc["doc_id"]: int(doc["category"]) for doc in docs}

    inverted: dict[int, list[tuple[str, Any]]] = {}
    prefix: dict[int, list[tuple[str, Any]]] = {}

    for doc in docs:
        doc_id = doc["doc_id"]
        sqrt_len = ops.sqrt(ops.from_float(float(doc["doc_len"])))
        unique_terms = sorted(set(doc["terms"]))
        prefix_hits: dict[int, int] = {}
        for term_id in unique_terms:
            weight = ops.from_float(float(term_weights[str(term_id)]))
            # 倒排贡献 = 词项权重 / sqrt(doc_len)
            contribution = ops.div(weight, sqrt_len)
            inverted.setdefault(term_id, []).append((doc_id, contribution))
            bucket = term_prefix(
                term_id,
                config.route("prefix").prefix_len or 3,
            )
            prefix_hits[bucket] = prefix_hits.get(bucket, 0) + 1
        for bucket, hit_count in prefix_hits.items():
            prefix.setdefault(bucket, []).append(
                (doc_id, ops.from_float(float(hit_count)))
            )

    # 倒排链按 doc_id 升序，保证遍历顺序确定
    for postings in inverted.values():
        postings.sort(key=lambda item: item[0])
    for postings in prefix.values():
        postings.sort(key=lambda item: item[0])

    # 类别热度：类别内 sqrt(doc_len) 之和 / 文档数，再除以全量最大值做确定性归一
    category_lengths: dict[int, list[Any]] = {}
    for doc in docs:
        category = int(doc["category"])
        category_lengths.setdefault(category, []).append(
            ops.sqrt(ops.from_float(float(doc["doc_len"])))
        )
    raw_hotness: dict[int, Any] = {}
    for category, values in category_lengths.items():
        total = values[0]
        for value in values[1:]:
            total = ops.add(total, value)
        raw_hotness[category] = ops.div(
            total, ops.from_float(float(len(values)))
        )
    if raw_hotness:
        max_hotness = max(raw_hotness.values(), key=lambda value: ops.to_float(value))
        category_hotness = {
            category: (
                ops.div(value, max_hotness)
                if ops.to_float(max_hotness) > 0
                else ops.from_float(0.0)
            )
            for category, value in raw_hotness.items()
        }
    else:
        category_hotness = {}

    # 文档向量 L2 归一化，全部通过领域算子完成
    doc_vectors: dict[str, tuple[Any, ...]] = {}
    for doc in docs:
        raw = [ops.from_float(float(x)) for x in doc["embedding"]]
        square_sum = ops.from_float(0.0)
        for component in raw:
            square_sum = ops.add(square_sum, ops.mul(component, component))
        norm = ops.sqrt(square_sum)
        if ops.to_float(norm) > 0:
            normalized = tuple(ops.div(component, norm) for component in raw)
        else:
            normalized = tuple(raw)
        doc_vectors[doc["doc_id"]] = normalized

    query_vectors: dict[str, tuple[Any, ...]] = {}
    for query in queries:
        raw_float = _query_vector(
            query["terms"], term_weights, config.embed_dim
        )
        raw = [ops.from_float(value) for value in raw_float]
        square_sum = ops.from_float(0.0)
        for component in raw:
            square_sum = ops.add(square_sum, ops.mul(component, component))
        norm = ops.sqrt(square_sum)
        if ops.to_float(norm) > 0:
            normalized = tuple(ops.div(component, norm) for component in raw)
        else:
            normalized = tuple(raw)
        query_vectors[query["query_id"]] = normalized

    return SearchIndex(
        config=config,
        ops=ops,
        doc_ids=doc_ids,
        doc_lengths={doc["doc_id"]: int(doc["doc_len"]) for doc in docs},
        doc_categories=doc_categories,
        inverted=inverted,
        prefix=prefix,
        category_hotness=category_hotness,
        doc_vectors=doc_vectors,
        query_vectors=query_vectors,
    )


def estimate_index_bytes(index: SearchIndex) -> int:
    """估算索引驻留内存（字节），用于内存预算准入。

    采用确定性的结构计数估算而非读取 RSS，保证跨机器可复现：
    每个倒排/前缀条目、每条向量分量与每个 doc_id 都给出固定权重。
    """
    ops = index.ops
    per_number = 8 if ops.mode == "float64" else 28
    per_string = 48
    postings = sum(len(items) for items in index.inverted.values())
    prefix_items = sum(len(items) for items in index.prefix.values())
    vector_components = sum(len(vec) for vec in index.doc_vectors.values())
    vector_components += sum(len(vec) for vec in index.query_vectors.values())
    return (
        len(index.doc_ids) * per_string
        + postings * (per_string + per_number)
        + prefix_items * (per_string + per_number)
        + len(index.category_hotness) * per_number
        + vector_components * per_number
    )


def cosine_dot(
    ops: Ops, left: tuple[Any, ...], right: tuple[Any, ...]
) -> Any:
    """两个已 L2 归一化向量的点积，即余弦相似度。"""
    total = ops.from_float(0.0)
    for a, b in zip(left, right):
        total = ops.add(total, ops.mul(a, b))
    return total
