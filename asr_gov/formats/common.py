"""跨版本共用的规范化切片模型（微秒为内部基准时间单位）。"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..config import canonical_json, sha256_hex


def us_to_ms(us: int) -> int:
    """微秒到毫秒的声明式取整规则：四舍五入（half-up）。"""
    return (int(us) + 500) // 1000


@dataclass
class TokenTiming:
    token: str
    start_us: int
    end_us: int
    conf_lo: float
    conf_hi: float

    def to_dict(self):
        return {
            "token": self.token,
            "start_us": self.start_us,
            "end_us": self.end_us,
            "conf_lo": self.conf_lo,
            "conf_hi": self.conf_hi,
        }


@dataclass
class CanonicalSlice:
    utterance_id: str
    start_us: int
    end_us: int
    text: str
    tokens: list = field(default_factory=list)
    flat_tokens: list = None
    speaker: str = None
    conf_lo: float = None
    conf_hi: float = None
    segment_label: str = None
    revision: int = 0
    provenance: dict = field(default_factory=dict)

    @property
    def start_ms(self) -> int:
        return us_to_ms(self.start_us)

    @property
    def end_ms(self) -> int:
        return us_to_ms(self.end_us)

    def token_strings(self):
        if self.flat_tokens:
            return list(self.flat_tokens)
        if self.tokens:
            return [t.token for t in self.tokens]
        return [w for w in self.text.split() if w]

    def to_dict(self):
        return {
            "utterance_id": self.utterance_id,
            "start_us": self.start_us,
            "end_us": self.end_us,
            "text": self.text,
            "tokens": [t.to_dict() for t in self.tokens],
            "flat_tokens": list(self.flat_tokens) if self.flat_tokens else None,
            "speaker": self.speaker,
            "conf_lo": self.conf_lo,
            "conf_hi": self.conf_hi,
            "segment_label": self.segment_label,
            "revision": self.revision,
        }

    def fingerprint(self) -> str:
        return sha256_hex(canonical_json(self.to_dict()).encode("utf-8"))


@dataclass
class CanonicalSliceSet:
    clip_id: str
    source_format: int
    slices: list = field(default_factory=list)
    vocab_version: str = None

    def slice_ids(self):
        return [s.utterance_id for s in self.slices]

    def get(self, utterance_id):
        for s in self.slices:
            if s.utterance_id == utterance_id:
                return s
        return None

    def to_dict(self):
        return {
            "clip_id": self.clip_id,
            "source_format": self.source_format,
            "vocab_version": self.vocab_version,
            "slices": [s.to_dict() for s in self.slices],
        }

    def digest(self) -> str:
        return sha256_hex(canonical_json(self.to_dict()).encode("utf-8"))
