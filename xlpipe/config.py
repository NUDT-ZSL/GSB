"""Pipeline configuration: controlled languages, fallback policy, limits."""

from dataclasses import dataclass, field

from .util import hash_obj, read_json

FALLBACK_KEEP_SOURCE = "keep_source"
FALLBACK_DROP = "drop"
FALLBACK_STRATEGIES = (FALLBACK_KEEP_SOURCE, FALLBACK_DROP)


@dataclass(frozen=True)
class Limits:
    max_pairs_per_stage: int = 100_000
    max_alignment_candidates: int = 4
    max_working_bytes: int = 64 * 1024 * 1024


@dataclass(frozen=True)
class PipelineConfig:
    languages: tuple = ("en", "es", "de", "fr", "zh")
    fallback_strategy: str = FALLBACK_KEEP_SOURCE
    limits: Limits = field(default_factory=Limits)

    @classmethod
    def load(cls, path):
        raw = read_json(path)
        limits = Limits(**raw.get("limits", {}))
        cfg = cls(
            languages=tuple(raw["languages"]),
            fallback_strategy=raw.get("fallback_strategy", FALLBACK_KEEP_SOURCE),
            limits=limits,
        )
        if cfg.fallback_strategy not in FALLBACK_STRATEGIES:
            raise ValueError(f"unknown fallback_strategy: {cfg.fallback_strategy}")
        return cfg

    def to_dict(self):
        return {
            "languages": list(self.languages),
            "fallback_strategy": self.fallback_strategy,
            "limits": {
                "max_pairs_per_stage": self.limits.max_pairs_per_stage,
                "max_alignment_candidates": self.limits.max_alignment_candidates,
                "max_working_bytes": self.limits.max_working_bytes,
            },
        }

    def config_hash(self):
        return hash_obj(self.to_dict())
