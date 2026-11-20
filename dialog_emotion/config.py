"""Validated pipeline configuration and snapshot helpers."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from fractions import Fraction
from typing import Any

from .canonical import content_hash
from .errors import fail


@dataclass(frozen=True)
class ResourceLimits:
    max_cache_entries: int = 4096
    max_candidate_comparisons: int = 5_000_000
    max_resident_feature_bytes: int = 128 * 1024 * 1024

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True)
class PipelineConfig:
    sample_rate: int = 16_000
    frame_ms: float = 20.0
    frame_shift_ms: float = 10.0
    segment_threshold_ms: float = 200.0
    rolloff_percent: float = 0.85
    speech_energy_threshold: float = 0.002
    ridge_lambda: float = 0.05
    confidence_level: float = 0.95
    weight_basis: str = "segment_duration_ms"
    audio_chunk_samples: int = 1600
    limits: ResourceLimits = ResourceLimits()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "dialog-emotion-config-v1",
            "sample_rate": self.sample_rate,
            "frame_ms": self.frame_ms,
            "frame_shift_ms": self.frame_shift_ms,
            "segment_threshold_ms": self.segment_threshold_ms,
            "rolloff_percent": self.rolloff_percent,
            "speech_energy_threshold": self.speech_energy_threshold,
            "ridge_lambda": self.ridge_lambda,
            "confidence_level": self.confidence_level,
            "weight_basis": self.weight_basis,
            "audio_chunk_samples": self.audio_chunk_samples,
            "limits": self.limits.to_dict(),
        }

    def content_hash(self) -> str:
        return content_hash(self.to_dict())

    def validate(self) -> None:
        if self.sample_rate != 16_000:
            fail(
                "E_SAMPLE_RATE",
                "sample_rate must be exactly 16000 Hz",
                sample_rate=self.sample_rate,
            )
        frame_samples = self.frame_samples()
        if frame_samples <= 0:
            fail(
                "E_FRAME_INVALID",
                "frame_ms must be positive and sample-aligned",
                frame_ms=self.frame_ms,
            )
        hop_samples = self.frame_shift_samples()
        if hop_samples <= 0:
            fail(
                "E_FRAME_SHIFT_INVALID",
                "frame_shift_ms must be positive and sample-aligned",
                frame_shift_ms=self.frame_shift_ms,
            )
        if hop_samples > frame_samples:
            fail(
                "E_FRAME_SHIFT_INVALID",
                "frame shift must not exceed frame length",
                frame_shift_ms=self.frame_shift_ms,
                frame_ms=self.frame_ms,
            )
        if self.segment_threshold_ms < 0:
            fail(
                "E_SEGMENT_CONFIG_INVALID",
                "segment_threshold_ms must be non-negative",
                segment_threshold_ms=self.segment_threshold_ms,
            )
        if not 0.0 < self.rolloff_percent <= 1.0:
            fail(
                "E_FEATURE_CONFIG_INVALID",
                "rolloff_percent must be in (0, 1]",
                rolloff_percent=self.rolloff_percent,
            )
        if not 0.0 < self.confidence_level < 1.0:
            fail(
                "E_FEATURE_CONFIG_INVALID",
                "confidence_level must be in (0, 1)",
                confidence_level=self.confidence_level,
            )
        if self.weight_basis != "segment_duration_ms":
            fail(
                "E_AGGREGATION_CONFIG_INVALID",
                "unsupported aggregation weight basis",
                weight_basis=self.weight_basis,
            )
        if self.audio_chunk_samples <= 0:
            fail(
                "E_CONFIG_INVALID",
                "audio_chunk_samples must be positive",
                audio_chunk_samples=self.audio_chunk_samples,
            )
        limits = self.limits
        for name, value in limits.to_dict().items():
            if value <= 0:
                fail(
                    "E_CONFIG_INVALID",
                    "resource limits must be positive",
                    limit=name,
                    value=value,
                )

    def frame_samples(self) -> int:
        return _aligned_samples(self.frame_ms, self.sample_rate)

    def frame_shift_samples(self) -> int:
        return _aligned_samples(self.frame_shift_ms, self.sample_rate)

    def frame_variant_key(self) -> str:
        return f"{self.frame_ms:g}/{self.frame_shift_ms:g}"


def _aligned_samples(milliseconds: float, sample_rate: int) -> int:
    try:
        value = Fraction(str(milliseconds)) * sample_rate / 1000
    except (ValueError, ZeroDivisionError):
        return 0
    if value.denominator != 1:
        return 0
    return int(value)


def validate_registration_rows(rows: list[dict[str, Any]]) -> None:
    for index, row in enumerate(rows):
        if "speaker_id" not in row or not row["speaker_id"]:
            fail(
                "E_SPEAKER_MISSING",
                "registration row is missing speaker_id",
                row_index=index,
                row=row,
            )
        if "register_seed" not in row:
            fail(
                "E_REGISTRATION_INVALID",
                "registration row is missing register_seed",
                row_index=index,
                speaker_id=row.get("speaker_id"),
            )
        if float(row.get("register_ms", 0)) <= 0:
            fail(
                "E_REGISTRATION_INVALID",
                "register_ms must be positive",
                row_index=index,
                speaker_id=row.get("speaker_id"),
            )


def validate_dialog_rows(rows: list[dict[str, Any]]) -> None:
    seen: set[str] = set()
    for index, row in enumerate(rows):
        dialog_id = row.get("dialog_id")
        if not dialog_id:
            fail("E_DIALOG_INVALID", "dialog row is missing dialog_id", row_index=index)
        if dialog_id in seen:
            fail(
                "E_DUPLICATE_DIALOG",
                "dialog_id must be unique",
                dialog_id=dialog_id,
                row_index=index,
            )
        seen.add(dialog_id)
        if int(row.get("sample_rate", 0)) != 16_000:
            fail(
                "E_SAMPLE_RATE",
                "dialog sample_rate must be exactly 16000 Hz",
                dialog_id=dialog_id,
                sample_rate=row.get("sample_rate"),
            )
        if int(row.get("duration_ms", 0)) <= 0:
            fail(
                "E_DIALOG_INVALID",
                "dialog duration_ms must be positive",
                dialog_id=dialog_id,
                duration_ms=row.get("duration_ms"),
            )
        if not row.get("speaker_ids"):
            fail(
                "E_DIALOG_INVALID",
                "dialog must contain at least one speaker",
                dialog_id=dialog_id,
            )
        if len(set(row["speaker_ids"])) != len(row["speaker_ids"]):
            fail(
                "E_DIALOG_INVALID",
                "dialog speaker_ids must be unique",
                dialog_id=dialog_id,
            )
