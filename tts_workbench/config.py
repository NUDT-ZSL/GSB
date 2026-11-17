from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .errors import ConfigInvalid
from .util import sha256_json


ENGINE_VERSION = "1.0.0"
CONFIG_SCHEMA_VERSION = 1


def _number(payload: dict[str, Any], key: str, *, default: float | None = None) -> float:
    value = payload.get(key, default)
    if value is None:
        raise ConfigInvalid(f"missing numeric field {key!r}", details={"field": key})
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigInvalid(f"{key!r} must be numeric", details={"field": key})
    value = float(value)
    if not math.isfinite(value):
        raise ConfigInvalid(f"{key!r} must be finite", details={"field": key})
    return value


def _integer(payload: dict[str, Any], key: str, *, default: int | None = None) -> int:
    value = payload.get(key, default)
    if value is None:
        raise ConfigInvalid(f"missing integer field {key!r}", details={"field": key})
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigInvalid(f"{key!r} must be an integer", details={"field": key})
    return value


@dataclass(frozen=True)
class AcousticConfig:
    sample_rate: int
    frame_length_ms: float
    frame_shift_ms: float
    f0_min_hz: float
    f0_max_hz: float
    f0_quantization_hz: float
    spectral_bands: int
    voicing_threshold: float
    energy_target_dbfs: float
    max_batch_frames: int
    concurrency: int
    snapshot_interval_frames: int
    frame_length_samples: int
    frame_shift_samples: int
    config_hash: str

    @property
    def frame_overlap_samples(self) -> int:
        return self.frame_length_samples - self.frame_shift_samples

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def report_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result.pop("config_hash", None)
        result["acoustic_hash"] = self.acoustic_hash
        return result

    def acoustic_payload(self) -> dict[str, Any]:
        return {
            "sample_rate": self.sample_rate,
            "frame_length_ms": self.frame_length_ms,
            "frame_shift_ms": self.frame_shift_ms,
            "f0_min_hz": self.f0_min_hz,
            "f0_max_hz": self.f0_max_hz,
            "f0_quantization_hz": self.f0_quantization_hz,
            "spectral_bands": self.spectral_bands,
            "voicing_threshold": self.voicing_threshold,
            "energy_target_dbfs": self.energy_target_dbfs,
            "max_batch_frames": self.max_batch_frames,
            "snapshot_interval_frames": self.snapshot_interval_frames,
        }

    @property
    def acoustic_hash(self) -> str:
        """Hash only fields that can change acoustic samples.

        Thread count, batch size and checkpoint cadence are execution controls
        and are deliberately excluded from this fingerprint.
        """
        return sha256_json(
            {
                "schema_version": CONFIG_SCHEMA_VERSION,
                "sample_rate": self.sample_rate,
                "frame_length_ms": self.frame_length_ms,
                "frame_shift_ms": self.frame_shift_ms,
                "f0_min_hz": self.f0_min_hz,
                "f0_max_hz": self.f0_max_hz,
                "f0_quantization_hz": self.f0_quantization_hz,
                "spectral_bands": self.spectral_bands,
                "voicing_threshold": self.voicing_threshold,
                "energy_target_dbfs": self.energy_target_dbfs,
            }
        )


def config_from_payload(payload: dict[str, Any]) -> AcousticConfig:
    if not isinstance(payload, dict):
        raise ConfigInvalid("acoustic configuration must be a JSON object")

    sample_rate = _integer(payload, "sample_rate")
    frame_length_ms = _number(payload, "frame_length_ms")
    frame_shift_ms = _number(payload, "frame_shift_ms")
    f0_min_hz = _number(payload, "f0_min_hz")
    f0_max_hz = _number(payload, "f0_max_hz")
    f0_quantization_hz = _number(
        payload,
        "f0_quantization_hz",
        default=payload.get("f0_step_hz"),
    )
    spectral_bands = _integer(payload, "spectral_bands")
    voicing_threshold = _number(payload, "voicing_threshold")
    energy_target_dbfs = _number(payload, "energy_target_dbfs")
    max_batch_frames = _integer(payload, "max_batch_frames")
    concurrency = _integer(payload, "concurrency")
    snapshot_interval_frames = _integer(payload, "snapshot_interval_frames")

    if sample_rate <= 0:
        raise ConfigInvalid("sample_rate must be positive")
    if frame_length_ms <= 0 or frame_shift_ms <= 0:
        raise ConfigInvalid("frame_length_ms and frame_shift_ms must be positive")
    if f0_min_hz <= 0 or f0_max_hz <= f0_min_hz:
        raise ConfigInvalid("f0 range must satisfy 0 < f0_min_hz < f0_max_hz")
    if f0_quantization_hz <= 0:
        raise ConfigInvalid("f0_quantization_hz must be positive")
    if spectral_bands <= 0:
        raise ConfigInvalid("spectral_bands must be positive")
    if not 0.0 <= voicing_threshold <= 1.0:
        raise ConfigInvalid("voicing_threshold must be in [0, 1]")
    if not -120.0 <= energy_target_dbfs <= 0.0:
        raise ConfigInvalid("energy_target_dbfs must be in [-120, 0]")
    if max_batch_frames <= 0:
        raise ConfigInvalid("max_batch_frames must be positive")
    # A non-positive concurrency is a hard configuration error, never a
    # silently coerced single-thread fallback.
    if concurrency <= 0:
        raise ConfigInvalid("concurrency must be positive")
    if snapshot_interval_frames <= 0:
        raise ConfigInvalid("snapshot_interval_frames must be positive")

    length_exact = sample_rate * frame_length_ms / 1000.0
    shift_exact = sample_rate * frame_shift_ms / 1000.0
    frame_length_samples = int(round(length_exact))
    frame_shift_samples = int(round(shift_exact))
    if abs(length_exact - frame_length_samples) > 1e-7:
        raise ConfigInvalid("frame_length_ms does not map to an integer sample count")
    if abs(shift_exact - frame_shift_samples) > 1e-7:
        raise ConfigInvalid("frame_shift_ms does not map to an integer sample count")
    if frame_length_samples <= 0 or frame_shift_samples <= 0:
        raise ConfigInvalid("derived frame size must be positive")
    if frame_length_samples % frame_shift_samples != 0:
        raise ConfigInvalid("frame length must be exactly divisible by frame shift")

    payload_for_hash = {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "sample_rate": sample_rate,
        "frame_length_ms": frame_length_ms,
        "frame_shift_ms": frame_shift_ms,
        "f0_min_hz": f0_min_hz,
        "f0_max_hz": f0_max_hz,
        "f0_quantization_hz": f0_quantization_hz,
        "spectral_bands": spectral_bands,
        "voicing_threshold": voicing_threshold,
        "energy_target_dbfs": energy_target_dbfs,
        "max_batch_frames": max_batch_frames,
        "concurrency": concurrency,
        "snapshot_interval_frames": snapshot_interval_frames,
    }

    return AcousticConfig(
        sample_rate=sample_rate,
        frame_length_ms=frame_length_ms,
        frame_shift_ms=frame_shift_ms,
        f0_min_hz=f0_min_hz,
        f0_max_hz=f0_max_hz,
        f0_quantization_hz=f0_quantization_hz,
        spectral_bands=spectral_bands,
        voicing_threshold=voicing_threshold,
        energy_target_dbfs=energy_target_dbfs,
        max_batch_frames=max_batch_frames,
        concurrency=concurrency,
        snapshot_interval_frames=snapshot_interval_frames,
        frame_length_samples=frame_length_samples,
        frame_shift_samples=frame_shift_samples,
        config_hash=sha256_json(payload_for_hash),
    )


def load_config(path: Path) -> AcousticConfig:
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigInvalid(f"cannot read acoustic configuration: {exc}") from exc
    return config_from_payload(payload)
