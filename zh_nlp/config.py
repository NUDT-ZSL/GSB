"""流水线配置：路径、可调参数与确定性配置哈希。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, asdict
from pathlib import Path


@dataclass(frozen=True)
class PipelineConfig:
    lexicon_path: str
    hmm_path: str
    pos_path: str
    max_word_length: int = 8
    hmm_emission_smoothing: float = 1e-6
    pos_emission_smoothing: float = 1e-6
    pos_transition_smoothing: float = 1e-8
    unknown_confidence: float = 0.01
    low_confidence_threshold: float = -10.0
    max_workers: int = 4

    @staticmethod
    def from_file(path: str | Path) -> "PipelineConfig":
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        return PipelineConfig(**raw)

    def to_dict(self) -> dict:
        return asdict(self)

    def config_hash(self) -> str:
        """基于全部配置字段及所引用数据文件字节计算 SHA256。

        数据文件内容参与哈希，使概率表或词典变更必然导致哈希变化；
        路径本身不参与，保证同内容不同路径下结果一致。
        """
        digest = hashlib.sha256()
        # max_workers 仅影响并发度，不改变计算结果，不参与配置哈希。
        payload = {
            key: value
            for key, value in self.to_dict().items()
            if not key.endswith("_path") and key != "max_workers"
        }
        digest.update(json.dumps(payload, sort_keys=True,
                                ensure_ascii=False).encode("utf-8"))
        for key in ("lexicon_path", "hmm_path", "pos_path"):
            data = Path(getattr(self, key)).read_bytes()
            digest.update(key.encode("utf-8"))
            digest.update(hashlib.sha256(data).digest())
        return digest.hexdigest()
