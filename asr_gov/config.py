"""识别配置与词表模型：单调递增版本号、规范化 JSON 与内容校验和。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict


def canonical_json(payload) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class RecognitionConfig:
    version: int = 1
    frame_length_ms: int = 25
    frame_shift_ms: int = 10
    energy_threshold_high_mult: float = 6.0
    energy_threshold_low_mult: float = 2.0
    min_slice_ms: int = 200
    max_slices: int = 32
    num_workers: int = 4
    quantization: str = "int16"
    max_slices_per_clip: int = 128
    max_resident_feature_bytes: int = 8 * 1024 * 1024
    max_recompute_chunks: int = 8
    exact_max_frames: int = 20000
    exact_max_slices: int = 500
    exact_max_pairwise: int = 2_000_000
    min_memory_headroom_bytes: int = 1024 * 1024
    approx_block_size: int = 64
    approx_shift_multiplier: int = 4

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, payload):
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in payload.items() if k in known})

    def canonical(self) -> str:
        return canonical_json(self.to_dict())

    def checksum(self) -> str:
        return sha256_hex(self.canonical().encode("utf-8"))

    def framing_params(self):
        return {
            "frame_length_ms": self.frame_length_ms,
            "frame_shift_ms": self.frame_shift_ms,
            "quantization": self.quantization,
        }

    def boundary_params(self):
        return {
            "energy_threshold_high_mult": self.energy_threshold_high_mult,
            "energy_threshold_low_mult": self.energy_threshold_low_mult,
            "min_slice_ms": self.min_slice_ms,
            "max_slices": self.max_slices,
        }


@dataclass
class Vocab:
    """词表：token 到确定性整数 id（按排序后的 token 序分配）。"""

    version: str
    token_ids: dict = field(default_factory=dict)

    @classmethod
    def from_tokens(cls, version, tokens):
        ordered = sorted(set(tokens))
        return cls(version=version, token_ids={t: i for i, t in enumerate(ordered)})

    def fingerprint_for(self, tokens) -> str:
        pairs = [[t, self.token_ids.get(t)] for t in sorted(set(tokens))]
        return sha256_hex(canonical_json(pairs).encode("utf-8"))

    def to_dict(self):
        return {"version": self.version, "token_ids": dict(sorted(self.token_ids.items()))}

    @classmethod
    def from_dict(cls, payload):
        return cls(version=payload["version"], token_ids=dict(payload["token_ids"]))
