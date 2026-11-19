"""Shared data models for configs, migrations and sampling."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SamplingConfig:
    version: str
    temperature: float
    top_k: float
    temperature_literal: str
    top_p: float | None = None
    repetition_penalty: float | None = None
    vocab_version: str | None = None
    units: dict[str, str] = field(default_factory=dict)
    literal_values: dict[str, str] = field(default_factory=dict)
    source_format: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "temperature": self.temperature,
            "temperature_literal": self.temperature_literal,
            "top_k": self.top_k,
            "top_p": self.top_p,
            "repetition_penalty": self.repetition_penalty,
            "vocab_version": self.vocab_version,
            "units": dict(self.units),
            "literal_values": dict(self.literal_values),
            "source_format": self.source_format,
        }


@dataclass
class MigrationResult:
    source_version: str
    target_version: str
    config: SamplingConfig
    output_text: str
    added_fields: list[dict[str, Any]] = field(default_factory=list)
    dropped_fields: list[dict[str, Any]] = field(default_factory=list)
    quantized_fields: list[dict[str, Any]] = field(default_factory=list)
    losses: list[dict[str, Any]] = field(default_factory=list)

    @property
    def lossy(self) -> bool:
        return bool(self.losses)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_version": self.source_version,
            "target_version": self.target_version,
            "output_config": self.config.to_dict(),
            "output_text": self.output_text,
            "added_fields": self.added_fields,
            "dropped_fields": self.dropped_fields,
            "quantized_fields": self.quantized_fields,
            "losses": self.losses,
            "lossy": self.lossy,
        }


@dataclass(frozen=True)
class NormalizedParams:
    temperature: float
    top_k: int
    top_p: float
    repetition_penalty: float
    vocabulary_size: int
    temperature_literal: str
    defaulted_fields: tuple[dict[str, Any], ...]
    clamped_fields: tuple[dict[str, Any], ...]
    range_policy: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "temperature": self.temperature,
            "top_k": self.top_k,
            "top_p": self.top_p,
            "repetition_penalty": self.repetition_penalty,
            "vocabulary_size": self.vocabulary_size,
            "temperature_literal": self.temperature_literal,
            "defaulted_fields": [dict(item) for item in self.defaulted_fields],
            "clamped_fields": [dict(item) for item in self.clamped_fields],
            "range_policy": dict(self.range_policy),
        }

    def sampling_fingerprint(self) -> dict[str, Any]:
        """Fields that must match for equivalent sampling decisions."""

        return {
            "temperature": self.temperature,
            "top_k": self.top_k,
            "top_p": self.top_p,
            "repetition_penalty": self.repetition_penalty,
        }


@dataclass(frozen=True)
class VocabEntry:
    version: str
    size: int
    tokens: tuple[str, ...]


@dataclass(frozen=True)
class VocabTable:
    snapshot_version: int
    checksum: str
    entries: dict[str, VocabEntry]


@dataclass
class InferenceRequest:
    request_id: str
    prompt_tokens: tuple[int, ...]
    tokenizer_version: str
    sampling_profiles: dict[str, str]
    sampling_seed: int
    seed_source: str
    logits: Any | None
    source_line: int

    def profile_versions(self) -> tuple[str, ...]:
        return tuple(sorted(self.sampling_profiles, key=_version_sort_key))


@dataclass
class SamplingResult:
    token_ids: list[int]
    token_sha256: str
    step_traces: list[dict[str, Any]]
    normalized: NormalizedParams
    seed: int
    config_version: str


def _version_sort_key(version: str) -> tuple[int, str]:
    if version.startswith("v") and version[1:].isdigit():
        return (int(version[1:]), version)
    return (999, version)
