"""受限词表上的哈希词袋嵌入。"""
from __future__ import annotations

from typing import Tuple

import numpy as np

from .hashing import int_seed


def tokenize(prompt: str) -> Tuple[str, ...]:
    return tuple(token for token in prompt.lower().replace("_", " ").split() if token)


class HashBagOfWords:
    """受限词表 + 哈希投影的确定性词袋嵌入。

    每个词元经稳定哈希映射到嵌入维度并施加确定性符号，词表外词元按 OOV
    投影处理但同样确定；嵌入按 L2 归一化，空文本退化为零向量。
    """

    def __init__(self, vocab: Tuple[str, ...], embed_dim: int) -> None:
        self.vocab = tuple(vocab)
        self.embed_dim = int(embed_dim)
        self._vocab_set = set(self.vocab)
        self._projection = {
            token: (int_seed("hbow", token) % self.embed_dim,
                    1.0 if int_seed("hbow-sign", token) % 2 == 0 else -1.0)
            for token in self.vocab
        }

    def embed(self, prompt: str) -> np.ndarray:
        vector = np.zeros(self.embed_dim, dtype=np.float32)
        for token in tokenize(prompt):
            if token in self._vocab_set:
                index, sign = self._projection[token]
                kind = "in"
            else:
                index = int_seed("hbow-oov", token) % self.embed_dim
                sign = 1.0 if int_seed("hbow-oov-sign", token) % 2 == 0 else -1.0
                kind = "oov"
            vector[index] += sign
            if kind == "oov":
                # OOV 词元以 0.5 权重计入，保持受限词表的主导地位。
                vector[index] -= 0.5 * sign
        norm = float(np.linalg.norm(vector))
        if norm > 0.0:
            vector /= norm
        return vector.astype(np.float32)
