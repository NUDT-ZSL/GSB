from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import BusinessError
from .utils import content_hash, read_json


REQUIRED_WEIGHTS = (
    "shared_entity",
    "shared_time",
    "trigger_form",
    "source_trust",
)


@dataclass
class AppConfig:
    vocab_version: str
    weights_version: str
    weights: dict[str, float]
    trust_version: str
    trust_sources: dict[str, dict[str, Any]]
    similarity_floor: float = 0.56
    degrade_floor: float = 0.90
    parser_version: str = "parser-2026.11.23.1"
    max_doc_events: int = 12
    max_candidate_comparisons: int = 160
    max_chain_memory_bytes: int = 1_048_576
    degrade_on_limit: bool = True
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_files(
        cls,
        root: str | Path,
        *,
        similarity_floor: float = 0.56,
        degrade_floor: float = 0.90,
        max_doc_events: int = 12,
        max_candidate_comparisons: int = 160,
        max_chain_memory_bytes: int = 1_048_576,
        degrade_on_limit: bool = True,
    ) -> "AppConfig":
        base = Path(root)
        vocab = read_json(base / "data" / "vocab.json")
        weights_doc = read_json(base / "config" / "weights.json")
        trust_doc = read_json(base / "data" / "trust_table.json")
        config = cls(
            vocab_version=str(vocab["version"]),
            weights_version=str(weights_doc["version"]),
            weights={key: float(value) for key, value in weights_doc["weights"].items()},
            trust_version=str(trust_doc["version"]),
            trust_sources=dict(trust_doc["sources"]),
            similarity_floor=float(similarity_floor),
            degrade_floor=float(degrade_floor),
            max_doc_events=int(max_doc_events),
            max_candidate_comparisons=int(max_candidate_comparisons),
            max_chain_memory_bytes=int(max_chain_memory_bytes),
            degrade_on_limit=bool(degrade_on_limit),
        )
        config.validate()
        return config

    def validate(self) -> None:
        self._validate_floor("similarity_floor", self.similarity_floor)
        self._validate_floor("degrade_floor", self.degrade_floor)
        if self.degrade_floor < self.similarity_floor:
            self._invalid(
                "degrade_floor",
                "降级相似度下限不得低于常规相似度下限",
            )
        for name in REQUIRED_WEIGHTS:
            if name not in self.weights:
                self._invalid(name, "缺少相似度权重")
            value = self.weights[name]
            if not isinstance(value, (int, float)):
                self._invalid(name, "相似度权重必须为数值")
            if float(value) < 0:
                self._invalid(name, "相似度权重不得为负值")
        if sum(float(value) for value in self.weights.values()) <= 0:
            self._invalid("weights", "相似度权重总和必须大于 0")
        for name, value in (
            ("max_doc_events", self.max_doc_events),
            ("max_candidate_comparisons", self.max_candidate_comparisons),
            ("max_chain_memory_bytes", self.max_chain_memory_bytes),
        ):
            if not isinstance(value, int) or value <= 0:
                self._invalid(name, "资源上限必须为正整数")
        for source, payload in self.trust_sources.items():
            trust = payload.get("trust")
            if not isinstance(trust, (int, float)) or not 0 <= float(trust) <= 1:
                self._invalid(
                    f"trust_sources.{source}.trust",
                    "来源可信度必须位于 [0, 1]",
                )

    def _validate_floor(self, field: str, value: float) -> None:
        if not isinstance(value, (int, float)) or not 0 <= float(value) <= 1:
            self._invalid(field, "相似度下限必须位于 [0, 1]")

    @staticmethod
    def _invalid(field: str, message: str) -> None:
        raise BusinessError(
            "E_CONFIG_INVALID",
            f"配置字段 {field} 非法：{message}",
            field=field,
        )

    @property
    def trust(self) -> dict[str, float]:
        return {
            source: float(payload["trust"])
            for source, payload in self.trust_sources.items()
        }

    @property
    def trust_content_hash(self) -> str:
        return content_hash(
            {
                "version": self.trust_version,
                "sources": self.trust_sources,
            }
        )

    @property
    def effective_weights_version(self) -> str:
        return content_hash(
            {
                "declared": self.weights_version,
                "weights": self.weights,
            }
        )[:20]

    @property
    def fingerprint(self) -> str:
        return content_hash(
            {
                "vocab_version": self.vocab_version,
                "weights_version": self.weights_version,
                "weights": self.weights,
                "trust_version": self.trust_version,
                "trust": self.trust_sources,
                "parser_version": self.parser_version,
                "similarity_floor": self.similarity_floor,
                "degrade_floor": self.degrade_floor,
            }
        )[:16]

    def as_public_dict(self) -> dict[str, Any]:
        return {
            "vocab_version": self.vocab_version,
            "weights_version": self.weights_version,
            "weights": self.weights,
            "trust_version": self.trust_version,
            "trust_sources": self.trust_sources,
            "similarity_floor": self.similarity_floor,
            "degrade_floor": self.degrade_floor,
            "parser_version": self.parser_version,
            "max_doc_events": self.max_doc_events,
            "max_candidate_comparisons": self.max_candidate_comparisons,
            "max_chain_memory_bytes": self.max_chain_memory_bytes,
            "degrade_on_limit": self.degrade_on_limit,
            "fingerprint": self.fingerprint,
            "trust_content_hash": self.trust_content_hash,
        }

    def clone_with(self, **updates: Any) -> "AppConfig":
        data = {
            "vocab_version": self.vocab_version,
            "weights_version": self.weights_version,
            "weights": dict(self.weights),
            "trust_version": self.trust_version,
            "trust_sources": {
                key: dict(value) for key, value in self.trust_sources.items()
            },
            "similarity_floor": self.similarity_floor,
            "degrade_floor": self.degrade_floor,
            "parser_version": self.parser_version,
            "max_doc_events": self.max_doc_events,
            "max_candidate_comparisons": self.max_candidate_comparisons,
            "max_chain_memory_bytes": self.max_chain_memory_bytes,
            "degrade_on_limit": self.degrade_on_limit,
            "extra": dict(self.extra),
        }
        data.update(updates)
        clone = self.__class__(**data)
        clone.validate()
        return clone
