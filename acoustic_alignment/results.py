"""Typed measurement results and JSON-safe serialization."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class BandResult:
    band_id: str
    band_index: int
    min_hz: float
    max_hz: float
    point_count: int = 0
    slope_rad_per_hz: float | None = None
    slope_q: int | None = None
    slope_quantization_error: float | None = None
    phase_delay_samples: float | None = None
    phase_delay_q: int | None = None
    phase_delay_fixed_samples: float | None = None
    phase_delay_quantization_error: float | None = None
    integer_delay_q: int | None = None
    phase_error_q: int | None = None
    phase_tolerance_q: int | None = None
    passed: bool = False
    code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "BandResult":
        known = {name for name in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{key: item for key, item in value.items() if key in known})


@dataclass
class TrackResult:
    track_id: str
    track_index: int = 0
    status: str = "measured"
    is_reference: bool = False
    business_codes: list[str] = field(default_factory=list)
    input_frames: int | None = None
    sample_rate: int | None = None
    analysis_sample_rate: int | None = None
    channels: int | None = None
    bit_depth: int | None = None
    expected_start_ms: int | None = None
    sweep_start_ms: float | None = None
    sweep_end_ms: float | None = None
    silence_start_sample: int | None = None
    silence_end_sample: int | None = None
    excitation_start_sample: int | None = None
    excitation_end_sample: int | None = None
    tail_end_sample: int | None = None
    search_window_samples: int | None = None
    analysis_samples: int | None = None
    noise_rms: float | None = None
    excitation_peak: float | None = None
    correlation_peak: float | None = None
    noise_gate_threshold: float | None = None
    delay_samples: int | None = None
    delay_float_samples: float | None = None
    delay_float_delta_samples: float | None = None
    delay_abs_error_samples: float | None = None
    delay_fixed_samples: float | None = None
    delay_quantization_error: float | None = None
    integer_delay_q: int | None = None
    float_delay_q: int | None = None
    delay_delta_q: int | None = None
    delay_tolerance_q: int | None = None
    delay_pass: bool = False
    alignment_pass: bool = False
    error_details: dict[str, Any] = field(default_factory=dict)
    bands: list[BandResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["bands"] = [band.to_dict() if isinstance(band, BandResult) else band for band in self.bands]
        return data

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TrackResult":
        known = {name for name in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        bands = [BandResult.from_dict(item) for item in value.get("bands", []) if isinstance(item, dict)]
        kwargs = {key: item for key, item in value.items() if key in known and key != "bands"}
        result = cls(**kwargs)
        result.bands = bands
        return result

    @property
    def complete(self) -> bool:
        return self.status == "measured" and self.delay_samples is not None and bool(self.bands)

    def sorted_codes(self) -> list[str]:
        return sorted(set(self.business_codes))
