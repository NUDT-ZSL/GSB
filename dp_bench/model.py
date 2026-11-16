"""转移依存解析模型：参数加载、特征哈希、点积打分。

模型参数 JSON 结构::

    {
      "feature_dim": 128,
      "hash_buckets": 128,
      "word_hash": {"经济": 3, ...},
      "pos_hash": {"NN": 1, ...},
      "weights": {"SHIFT": [...], "LEFT_REDUCE": [...],
                  "RIGHT_REDUCE": [...], "FINISH": [...]}
    }

特征向量为确定性多热向量：栈顶/次栈顶与缓冲队首/次队首的词、词性各占
哈希槽位（词性槽位整体偏移半个桶区以避免与词槽冲突），槽位写 1.0。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import numpy as np

from .errors import ModelShapeMismatchError, UnknownPosError

ACTIONS = ("SHIFT", "LEFT_REDUCE", "RIGHT_REDUCE", "FINISH")

_NULL = "<NULL>"


def canonical_json_bytes(obj) -> bytes:
    """跨平台、跨进程稳定的 JSON 规范化字节（用于哈希）。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def sha256_hex(obj) -> str:
    return hashlib.sha256(canonical_json_bytes(obj)).hexdigest()


@dataclass(frozen=True)
class Token:
    index: int
    word: str
    pos: str


class TransitionModel:
    """加载并校验小型转移解析模型，负责特征构造与动作打分。"""

    def __init__(self, params: dict):
        self._raw = params
        required = ("feature_dim", "hash_buckets", "word_hash",
                    "pos_hash", "weights")
        missing = [k for k in required if k not in params]
        if missing:
            raise ModelShapeMismatchError(
                f"模型参数缺少字段: {','.join(missing)}", phase="load")

        self.feature_dim = int(params["feature_dim"])
        self.hash_buckets = int(params["hash_buckets"])
        if self.feature_dim <= 0 or self.hash_buckets <= 0:
            raise ModelShapeMismatchError(
                "feature_dim 与 hash_buckets 必须为正整数", phase="load")
        if self.feature_dim != self.hash_buckets:
            raise ModelShapeMismatchError(
                f"模型特征维度 {self.feature_dim} 与哈希桶数 "
                f"{self.hash_buckets} 不符", phase="load")

        self.word_hash = dict(params["word_hash"])
        self.pos_hash = dict(params["pos_hash"])
        if not isinstance(self.word_hash, dict) or not isinstance(
                self.pos_hash, dict):
            raise ModelShapeMismatchError(
                "word_hash/pos_hash 必须为对象", phase="load")

        weights = params["weights"]
        if not isinstance(weights, dict):
            raise ModelShapeMismatchError("weights 必须为对象", phase="load")
        missing_actions = [a for a in ACTIONS if a not in weights]
        if missing_actions:
            raise ModelShapeMismatchError(
                f"动作权重缺少: {','.join(missing_actions)}", phase="load")
        self.weights = {}
        for action in ACTIONS:
            vec = np.asarray(weights[action], dtype=np.float64)
            if vec.shape != (self.feature_dim,):
                raise ModelShapeMismatchError(
                    f"动作 {action} 权重向量形状 {vec.shape} 与特征维度 "
                    f"{(self.feature_dim,)} 不符", phase="load")
            self.weights[action] = vec
        self._weight_matrix = np.vstack(
            [self.weights[a] for a in ACTIONS])

        self._pos_offset = self.hash_buckets // 2
        self._cache: dict[str, int] = {}

    @property
    def raw(self) -> dict:
        return self._raw

    def content_hash(self) -> str:
        return sha256_hex(self._raw)

    # ---- 确定性哈希 -----------------------------------------------------
    def _bucket(self, key: str, table: dict) -> int:
        cache_key = key
        if cache_key in self._cache:
            return self._cache[cache_key]
        if key in table:
            bucket = int(table[key]) % self.hash_buckets
        else:
            digest = hashlib.blake2b(
                key.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest, "big") % self.hash_buckets
        self._cache[cache_key] = bucket
        return bucket

    def _word_bucket(self, word: str) -> int:
        return self._bucket(word, self.word_hash)

    def _pos_bucket(self, pos: str) -> int:
        if pos != _NULL and pos not in self.pos_hash:
            raise UnknownPosError(
                f"词性 '{pos}' 不在模型词性表中", phase="feature",
                exception_type="UnknownPosError")
        return (self._bucket(pos, self.pos_hash) + self._pos_offset) % \
            self.hash_buckets

    # ---- 特征向量 -------------------------------------------------------
    def feature_vector(self, stack: list[Token],
                       buffer_: list[Token]) -> np.ndarray:
        """根据当前栈与缓冲构造多热特征向量。

        取栈顶/次栈顶、缓冲队首/次队首共四个位置，每个位置贡献词、词性
        两个槽位；空位置以 <NULL> 占位。
        """
        positions = []
        positions.append(stack[-1] if len(stack) >= 1 else None)
        positions.append(stack[-2] if len(stack) >= 2 else None)
        positions.append(buffer_[0] if len(buffer_) >= 1 else None)
        positions.append(buffer_[1] if len(buffer_) >= 2 else None)

        vec = np.zeros(self.feature_dim, dtype=np.float64)
        for tok in positions:
            if tok is None:
                vec[self._word_bucket(_NULL)] = 1.0
                vec[self._pos_bucket(_NULL)] = 1.0
            else:
                vec[self._word_bucket(tok.word)] = 1.0
                vec[self._pos_bucket(tok.pos)] = 1.0
        return vec

    # ---- 打分 -----------------------------------------------------------
    def score(self, features: np.ndarray) -> dict:
        """返回四类动作的点积得分（保持 ACTIONS 固定顺序）。"""
        raw = self._weight_matrix @ features
        return {action: float(raw[i]) for i, action in enumerate(ACTIONS)}
