"""Pipeline configuration and validation constants."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import fail
from .models import artifact_hash


STAGES: tuple[str, ...] = (
    "features",
    "timbre",
    "alignment",
    "params",
    "synthesis",
)

STAGE_INDEX = {name: index for index, name in enumerate(STAGES)}

PRECISION_PROFILES: dict[str, dict[str, Any]] = {
    "fp32": {"decimals": 12, "description": "float32-like determinism envelope"},
    "fp16_simulated": {"decimals": 5, "description": "deterministic fp16 simulation"},
    "int16_simulated": {"decimals": 7, "description": "deterministic integer-oriented profile"},
}

VALID_FRAME_MS = {10, 20, 25, 40}

DEFAULT_MAPPING_PARAMS: dict[str, float | int] = {
    "f0_strength": 0.65,
    "energy_strength": 0.70,
    "formant_strength": 0.25,
    "min_f0_hz": 60.0,
    "max_f0_hz": 400.0,
    "harmonics": 8,
}


@dataclass
class ResourceLimits:
    max_recompute_utterances: int = 64
    max_resident_embeddings: int = 32
    max_parallel_nodes: int = 4

    def validate(self) -> None:
        for name in (
            "max_recompute_utterances",
            "max_resident_embeddings",
            "max_parallel_nodes",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or value < 1:
                fail(
                    "E_RESOURCE_LIMIT",
                    "资源上限配置必须为正整数",
                    field=name,
                    details={"value": value},
                )

    def to_dict(self) -> dict[str, int]:
        return {
            "max_recompute_utterances": self.max_recompute_utterances,
            "max_resident_embeddings": self.max_resident_embeddings,
            "max_parallel_nodes": self.max_parallel_nodes,
        }


@dataclass
class PipelineConfig:
    sample_rate: int = 16000
    frame_ms: int = 20
    hop_ms: int = 20
    mapping_version: str = "map-v1"
    mapping_params: dict[str, float | int] = field(
        default_factory=lambda: dict(DEFAULT_MAPPING_PARAMS)
    )
    unit_count: int = 8
    extractor_version: str = "features-v1"
    embedding_version: str = "timbre-v1"
    alignment_version: str = "alignment-v1"
    synth_version: str = "synth-v1"
    parallel_nodes: int = 1
    resource_limits: ResourceLimits = field(default_factory=ResourceLimits)

    @property
    def frame_samples(self) -> int:
        return self.sample_rate * self.frame_ms // 1000

    def __post_init__(self) -> None:
        self.mapping_params = {
            **DEFAULT_MAPPING_PARAMS,
            **dict(self.mapping_params),
        }

    @property
    def hop_samples(self) -> int:
        return self.sample_rate * self.hop_ms // 1000

    @property
    def nyquist_hz(self) -> float:
        return self.sample_rate / 2.0

    def mapping_config_hash(self) -> str:
        return artifact_hash(
            {
                "mapping_version": self.mapping_version,
                "mapping_params": self.mapping_params,
            }
        )

    def config_hash(self) -> str:
        return artifact_hash(self.to_dict())

    def validate(self) -> None:
        if self.sample_rate != 16000:
            fail(
                "E_INVALID_SAMPLE_RATE",
                "采样率必须为 16000 Hz",
                field="sample_rate",
                details={"actual": self.sample_rate, "expected": 16000},
            )
        if self.frame_ms not in VALID_FRAME_MS:
            fail(
                "E_INVALID_FRAME_MS",
                "frame_ms 必须取 10、20、25 或 40",
                field="frame_ms",
                details={"actual": self.frame_ms, "allowed": sorted(VALID_FRAME_MS)},
            )
        if (
            not isinstance(self.hop_ms, int)
            or self.hop_ms <= 0
            or self.hop_ms > self.frame_ms
        ):
            fail(
                "E_INVALID_FRAME_HOP",
                "帧移必须为正整数且不得超过帧长",
                field="hop_ms",
                details={"hop_ms": self.hop_ms, "frame_ms": self.frame_ms},
            )
        if self.unit_count < 1:
            fail(
                "E_INVALID_CONFIG",
                "unit_count 必须为正整数",
                field="unit_count",
                details={"actual": self.unit_count},
            )
        if not isinstance(self.parallel_nodes, int) or self.parallel_nodes < 1:
            fail(
                "E_RESOURCE_LIMIT",
                "parallel_nodes 必须为正整数",
                field="parallel_nodes",
                details={"actual": self.parallel_nodes},
            )
        harmonics = int(self.mapping_params.get("harmonics", 0))
        if harmonics < 1:
            fail(
                "E_INVALID_CONFIG",
                "mapping_params.harmonics 必须为正整数",
                field="mapping_params.harmonics",
                details={"actual": harmonics},
            )
        self.resource_limits.validate()

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_rate": self.sample_rate,
            "frame_ms": self.frame_ms,
            "hop_ms": self.hop_ms,
            "mapping_version": self.mapping_version,
            "mapping_params": dict(sorted(self.mapping_params.items())),
            "unit_count": self.unit_count,
            "extractor_version": self.extractor_version,
            "embedding_version": self.embedding_version,
            "alignment_version": self.alignment_version,
            "synth_version": self.synth_version,
            "parallel_nodes": self.parallel_nodes,
            "resource_limits": self.resource_limits.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PipelineConfig":
        limits = ResourceLimits(**value.get("resource_limits", {}))
        return cls(
            sample_rate=int(value.get("sample_rate", 16000)),
            frame_ms=int(value.get("frame_ms", 20)),
            hop_ms=int(value.get("hop_ms", 20)),
            mapping_version=str(value.get("mapping_version", "map-v1")),
            mapping_params=dict(
                {
                    **DEFAULT_MAPPING_PARAMS,
                    **value.get("mapping_params", {}),
                }
            ),
            unit_count=int(value.get("unit_count", 8)),
            extractor_version=str(value.get("extractor_version", "features-v1")),
            embedding_version=str(value.get("embedding_version", "timbre-v1")),
            alignment_version=str(value.get("alignment_version", "alignment-v1")),
            synth_version=str(value.get("synth_version", "synth-v1")),
            parallel_nodes=int(value.get("parallel_nodes", 1)),
            resource_limits=limits,
        )


def decimals_for(profile: str) -> int:
    try:
        return int(PRECISION_PROFILES[profile]["decimals"])
    except KeyError:
        fail(
            "E_UNKNOWN_PRECISION_PROFILE",
            "precision_profile 未知",
            field="precision_profile",
            details={"actual": profile, "allowed": sorted(PRECISION_PROFILES)},
        )
    raise AssertionError("unreachable")

