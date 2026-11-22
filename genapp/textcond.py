"""文本条件: 受控词表 + 词频加权池化.

确定性约束:
- 词表为固定常量元组;
- 每个 token 的嵌入向量由 sha256(token) 派生种子, 与运行环境无关;
- 池化按 token 字典序累加, 不依赖字典遍历顺序或系统时间.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from typing import Sequence

import numpy as np

VOCAB = tuple(
    sorted(
        [
            "amber", "arch", "ashen", "aurora", "basalt", "bay", "birch",
            "bloom", "breeze", "brine", "canyon", "cedar", "cinder", "cirrus",
            "cliff", "cobalt", "coral", "crag", "creek", "crest", "dawn",
            "delta", "dune", "dusk", "ember", "fern", "fjord", "flint",
            "fog", "frost", "gale", "glade", "glen", "granite", "grove",
            "harbor", "haze", "heath", "horizon", "inlet", "iris", "isle",
            "jade", "kelp", "lagoon", "larch", "lichen", "lumen", "marsh",
            "mesa", "mist", "moss", "nimbus", "oasis", "onyx", "opal",
            "pine", "quartz", "reef", "ridge", "sable", "sage", "shale",
            "sierra", "solstice", "spruce", "storm", "summit", "tide",
        ]
    )
)

_VOCAB_SET = frozenset(VOCAB)


def token_embedding(token: str, dim: int) -> np.ndarray:
    """由 token 文本派生确定性嵌入向量 (float64)."""
    digest = hashlib.sha256(f"genapp-token:{token}".encode("utf-8")).digest()
    seed = int.from_bytes(digest[:8], "little", signed=False)
    rng = np.random.default_rng(seed)
    return rng.standard_normal(dim)


def condition_vector(
    tokens: Sequence[str], dim: int, dtype: np.dtype
) -> np.ndarray:
    """词频加权池化得到条件向量.

    累加顺序为 token 字典序, 与输入顺序无关, 保证逐字节可复现.
    """
    counts = Counter(tokens)
    unknown = sorted(t for t in counts if t not in _VOCAB_SET)
    if unknown:
        raise ValueError(f"tokens outside controlled vocab: {unknown}")
    total = sum(counts.values())
    if total == 0:
        raise ValueError("empty token sequence")
    vec = np.zeros(dim, dtype=np.float64)
    for token in sorted(counts):
        weight = counts[token] / total
        vec += weight * token_embedding(token, dim)
    return vec.astype(dtype)
