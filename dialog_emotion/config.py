from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .errors import DomainError, E_CACHE_CORRUPT, E_CONFIG_INVALID, E_RESOURCE_LIMIT
from .util import atomic_write_json, quantize, read_json, sha256_value


@dataclass(frozen=True)
class PipelineConfig:
    sample_rate: int = 16000
    frame_ms: float = 20.0
    frame_shift_ms: float = 20.0
    fft_size: int = 512
    window: str = "hann"
    feature_dtype: str = "float64"
    segment_threshold_ms: float = 100.0
    embedding_version: str = "embed-v1"
    regression_ridge: float = 0.01
    confidence_level: float = 0.95
    max_feature_memory_mb: float = 64.0
    max_cache_entries: int = 10000
    max_parallel_nodes: int = 2
    requested_parallel_nodes: int = 1
    resource_policy: str = "error"
    cache_schema_version: str = "cache-v1"
    frame_schema_version: str = "frame-v1"
    speaker_schema_version: str = "speaker-v1"
    emotion_schema_version: str = "emotion-v1"
    aggregate_schema_version: str = "aggregate-v1"

    def validate(self) -> None:
        if self.sample_rate != 16000:
            raise DomainError(
                E_CONFIG_INVALID,
                "sample rate must be exactly 16000",
                field="sample_rate",
                value=self.sample_rate,
            )
        if not isinstance(self.frame_ms, (int, float)) or self.frame_ms <= 0:
            raise DomainError(
                E_CONFIG_INVALID,
                "frame_ms must be positive",
                field="frame_ms",
                value=self.frame_ms,
            )
        if not isinstance(self.frame_shift_ms, (int, float)) or self.frame_shift_ms <= 0:
            raise DomainError(
                E_CONFIG_INVALID,
                "frame_shift_ms must be positive",
                field="frame_shift_ms",
                value=self.frame_shift_ms,
            )
        if self.frame_shift_ms > self.frame_ms:
            raise DomainError(
                E_CONFIG_INVALID,
                "frame shift must not exceed frame length",
                field="frame_shift_ms",
                frame_ms=self.frame_ms,
                frame_shift_ms=self.frame_shift_ms,
            )
        if self.frame_ms > 1000:
            raise DomainError(
                E_CONFIG_INVALID,
                "frame_ms is outside the supported range",
                field="frame_ms",
                value=self.frame_ms,
            )
        if self.fft_size < 16 or self.fft_size & (self.fft_size - 1):
            raise DomainError(
                E_CONFIG_INVALID,
                "fft_size must be a power of two and at least 16",
                field="fft_size",
                value=self.fft_size,
            )
        frame_samples = self.frame_ms * self.sample_rate / 1000.0
        shift_samples = self.frame_shift_ms * self.sample_rate / 1000.0
        if abs(frame_samples - round(frame_samples)) > 1e-9:
            raise DomainError(
                E_CONFIG_INVALID,
                "frame_ms must map to an integer sample count",
                field="frame_ms",
                sample_count=frame_samples,
            )
        if abs(shift_samples - round(shift_samples)) > 1e-9:
            raise DomainError(
                E_CONFIG_INVALID,
                "frame_shift_ms must map to an integer sample count",
                field="frame_shift_ms",
                sample_count=shift_samples,
            )
        if self.window not in {"hann"}:
            raise DomainError(
                E_CONFIG_INVALID,
                "unsupported window",
                field="window",
                value=self.window,
            )
        if self.feature_dtype not in {"float32", "float64"}:
            raise DomainError(
                E_CONFIG_INVALID,
                "unsupported feature dtype",
                field="feature_dtype",
                value=self.feature_dtype,
            )
        if self.segment_threshold_ms < 0:
            raise DomainError(
                E_CONFIG_INVALID,
                "segment threshold must not be negative",
                field="segment_threshold_ms",
                value=self.segment_threshold_ms,
            )
        if self.regression_ridge < 0:
            raise DomainError(
                E_CONFIG_INVALID,
                "regression_ridge must not be negative",
                field="regression_ridge",
            )
        if not 0.0 < self.confidence_level < 1.0:
            raise DomainError(
                E_CONFIG_INVALID,
                "confidence level must be between zero and one",
                field="confidence_level",
                value=self.confidence_level,
            )
        if self.max_feature_memory_mb <= 0:
            raise DomainError(
                E_CONFIG_INVALID,
                "max_feature_memory_mb must be positive",
                field="max_feature_memory_mb",
            )
        if self.max_cache_entries < 0:
            raise DomainError(
                E_CONFIG_INVALID,
                "max_cache_entries must not be negative",
                field="max_cache_entries",
            )
        if self.max_parallel_nodes < 1 or self.requested_parallel_nodes < 1:
            raise DomainError(
                E_CONFIG_INVALID,
                "parallel node limits must be at least one",
                field="max_parallel_nodes",
            )
        if self.resource_policy not in {"error", "degrade"}:
            raise DomainError(
                E_CONFIG_INVALID,
                "resource_policy must be error or degrade",
                field="resource_policy",
                value=self.resource_policy,
            )

    @property
    def frame_samples(self) -> int:
        return int(round(self.frame_ms * self.sample_rate / 1000.0))

    @property
    def shift_samples(self) -> int:
        return int(round(self.frame_shift_ms * self.sample_rate / 1000.0))

    def effective_parallel_nodes(self) -> tuple[int, list[str]]:
        if self.requested_parallel_nodes > self.max_parallel_nodes:
            return self.max_parallel_nodes, ["parallelism_capped"]
        return self.requested_parallel_nodes, []

    def to_dict(self) -> dict[str, Any]:
        return {key: quantize(value) if isinstance(value, float) else value for key, value in asdict(self).items()}


@dataclass
class ConfigSnapshotStore:
    root: Path

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def current_path(self) -> Path:
        return self.root / "current.json"

    def load_current(self) -> dict[str, Any] | None:
        if not self.current_path.exists():
            return None
        try:
            document = read_json(self.current_path)
        except Exception as exc:
            raise DomainError(
                E_CACHE_CORRUPT,
                "configuration snapshot is not valid JSON",
                entry="config_snapshot",
                version=None,
            ) from exc
        checksum = document.get("checksum")
        body = {key: value for key, value in document.items() if key != "checksum"}
        if checksum != sha256_value(body):
            raise DomainError(
                E_CACHE_CORRUPT,
                "configuration snapshot checksum mismatch",
                entry="config_snapshot",
                version=document.get("version"),
            )
        return document

    def save(self, config: PipelineConfig) -> dict[str, Any]:
        payload = config.to_dict()
        current = self.load_current()
        if current is not None and current.get("payload") == payload:
            return current
        version = int(current.get("version", 0)) + 1 if current else 1
        body = {
            "schema_version": "config-v1",
            "version": version,
            "payload": payload,
        }
        document = dict(body)
        document["checksum"] = sha256_value(body)
        atomic_write_json(self.root / f"v{version:08d}.json", document)
        atomic_write_json(self.current_path, document)
        return document

    def replace(self, config: PipelineConfig) -> dict[str, Any]:
        current = self.load_current()
        version = int(current.get("version", 0)) + 1 if current else 1
        payload = config.to_dict()
        body = {
            "schema_version": "config-v1",
            "version": version,
            "payload": payload,
        }
        document = dict(body)
        document["checksum"] = sha256_value(body)
        atomic_write_json(self.root / f"v{version:08d}.json", document)
        atomic_write_json(self.current_path, document)
        return document


def estimate_feature_memory_bytes(frame_count: int, frame_samples: int, fft_size: int) -> int:
    frame_work = frame_samples * 8
    spectrum_work = (fft_size // 2 + 1) * 16
    output = frame_count * 4 * 8
    return int(frame_count * (frame_work + spectrum_work) + output)


def enforce_feature_memory(config: PipelineConfig, estimated_bytes: int) -> tuple[str, list[str], bool]:
    limit = int(config.max_feature_memory_mb * 1024 * 1024)
    if estimated_bytes <= limit:
        return config.feature_dtype, [], False
    if config.resource_policy == "degrade":
        return "float32", ["feature_precision_float32"], True
    raise DomainError(
        E_RESOURCE_LIMIT,
        "estimated resident feature memory exceeds the configured limit",
        category="feature_memory",
        estimated_bytes=estimated_bytes,
        limit_bytes=limit,
        field="max_feature_memory_mb",
    )
