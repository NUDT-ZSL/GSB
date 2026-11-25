"""固定种子确定性样本生成。

所有随机量都来自显式种子（NumPy ``default_rng`` 与 Python ``random.Random``），
同一版本生成的样本逐字节稳定，供回归与验收复现。

产物：

- ``docs.jsonl``: 文档（doc_id / terms / doc_len / category）
- ``queries.jsonl``: 查询（query_id / terms / top_k）
- ``term_weights.json``: 词项权重表
- ``recall_config.json``: 四路召回、融合、精度与调度配置
- ``manifest.json``: 生成参数与输入文件校验和
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SEED = 20261126
VOCAB_SIZE = 60
NUM_DOCS = 100
NUM_QUERIES = 12
NUM_CATEGORIES = 3
EMBED_DIM = 16
PREFIX_LEN = 3


@dataclass(frozen=True)
class SampleParams:
    seed: int = SEED
    vocab_size: int = VOCAB_SIZE
    num_docs: int = NUM_DOCS
    num_queries: int = NUM_QUERIES
    num_categories: int = NUM_CATEGORIES
    embed_dim: int = EMBED_DIM
    prefix_len: int = PREFIX_LEN


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def generate_samples(out_dir: str | Path, params: SampleParams | None = None) -> dict:
    """生成全套确定性样本并写入 ``out_dir``，返回产物清单。"""
    params = params or SampleParams()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    py_rng = random.Random(params.seed)
    np_rng = np.random.default_rng(params.seed)

    # 词项权重表：0.5 ~ 2.0 之间，两位小数，保证全部为正
    term_weights = {
        str(term_id): round(float(py_rng.uniform(0.5, 2.0)), 2)
        for term_id in range(params.vocab_size)
    }

    docs: list[dict] = []
    embeddings = np_rng.normal(
        0.0, 1.0, size=(params.num_docs, params.embed_dim)
    ).astype(np.float64)
    for doc_index in range(params.num_docs):
        term_count = py_rng.randint(5, 15)
        terms = sorted(py_rng.sample(range(params.vocab_size), term_count))
        doc = {
            "doc_id": f"doc_{doc_index:04d}",
            "terms": terms,
            # doc_len 与词项数相关但不完全相等，模拟真实长度字段
            "doc_len": term_count + py_rng.randint(0, 6),
            "category": py_rng.randrange(params.num_categories),
            "embedding": [round(float(x), 6) for x in embeddings[doc_index]],
        }
        docs.append(doc)

    queries: list[dict] = []
    for query_index in range(params.num_queries):
        term_count = py_rng.randint(2, 5)
        terms = sorted(py_rng.sample(range(params.vocab_size), term_count))
        queries.append(
            {
                "query_id": f"q_{query_index:03d}",
                "terms": terms,
                "top_k": py_rng.randint(3, 8),
            }
        )

    recall_config = {
        "routes": [
            {
                "name": "inverted",
                "weight": 0.40,
                "enabled": True,
                "candidate_cap": 30,
            },
            {
                "name": "prefix",
                "weight": 0.20,
                "enabled": True,
                "candidate_cap": 30,
                "prefix_len": params.prefix_len,
            },
            {
                "name": "category_hot",
                "weight": 0.15,
                "enabled": True,
                "candidate_cap": 30,
            },
            {
                "name": "vector_ann",
                "weight": 0.25,
                "enabled": True,
                "candidate_cap": 30,
            },
        ],
        "fusion": {
            # highest_norm: 保留最高归一化分；first_hit: 保留首路命中
            "dedup_policy": "highest_norm",
            "route_order": ["inverted", "prefix", "category_hot", "vector_ann"],
            "min_max_epsilon": 1e-12,
            # 与定点缩放 10^-6 的量化步长匹配，门限偏差在保护带内即视为稳定
            "threshold_guard": 1e-6,
        },
        "precision": {
            "mode": "float64",
            "scale": 1_000_000,
            "rounding": "half_even",
        },
        "scheduler": {
            "workers": 2,
            "batch_size": 4,
            "candidate_budget": 480,
            "memory_budget_bytes": 64 * 1024 * 1024,
        },
        "vocab_size": params.vocab_size,
        "num_categories": params.num_categories,
        "embed_dim": params.embed_dim,
    }

    docs_path = out / "docs.jsonl"
    queries_path = out / "queries.jsonl"
    weights_path = out / "term_weights.json"
    config_path = out / "recall_config.json"

    with docs_path.open("w", encoding="utf-8") as handle:
        for doc in docs:
            handle.write(json.dumps(doc, ensure_ascii=False, sort_keys=True) + "\n")
    with queries_path.open("w", encoding="utf-8") as handle:
        for query in queries:
            handle.write(json.dumps(query, ensure_ascii=False, sort_keys=True) + "\n")
    weights_path.write_text(
        json.dumps(term_weights, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    config_path.write_text(
        json.dumps(recall_config, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )

    manifest = {
        "generator": "sr_pipeline.samplegen",
        "generator_version": "1.0.0",
        "params": params.__dict__,
        "files": {
            "docs": {
                "path": docs_path.name,
                "sha256": _file_sha256(docs_path),
                "records": len(docs),
            },
            "queries": {
                "path": queries_path.name,
                "sha256": _file_sha256(queries_path),
                "records": len(queries),
            },
            "term_weights": {
                "path": weights_path.name,
                "sha256": _file_sha256(weights_path),
                "records": len(term_weights),
            },
            "recall_config": {
                "path": config_path.name,
                "sha256": _file_sha256(config_path),
            },
        },
    }
    manifest_path = out / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    return manifest


def term_prefix(term_id: int, prefix_len: int = PREFIX_LEN) -> int:
    """词项到前缀桶的确定性映射：取词项编号对 10^prefix_len 取模。"""
    return term_id % int(math.pow(10, prefix_len))
