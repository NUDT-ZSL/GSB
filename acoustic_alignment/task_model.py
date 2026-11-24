"""Measurement task description parsing and validation."""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from .codes import Code, TaskSchemaError
from .wav_io import ALLOWED_BIT_DEPTHS, ALLOWED_SAMPLE_RATES


def pick(mapping: dict[str, Any], *names: str, default=None):
    for name in names:
        if name in mapping and mapping[name] is not None:
            return mapping[name]
    return default


def is_real_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def is_positive_number(value: Any) -> bool:
    return is_real_number(value) and float(value) > 0.0


def is_non_negative_number(value: Any) -> bool:
    return is_real_number(value) and float(value) >= 0.0


def ms_to_samples(ms: float | int, sample_rate: int) -> int:
    """Convert milliseconds to an exact non-negative source sample index."""
    if not is_non_negative_number(ms):
        raise ValueError("millisecond boundary must be a finite non-negative number")
    value = Decimal(str(ms)) * Decimal(int(sample_rate)) / Decimal(1000)
    return int(value.to_integral_value(rounding=ROUND_HALF_UP))


@dataclass
class BandSpec:
    band_id: str
    band_index: int
    min_hz: float
    max_hz: float


@dataclass
class TrackSpec:
    track_id: str
    path: Path | None
    raw: dict[str, Any]
    enabled: bool = True
    is_reference: bool = False
    expected_start_ms: int | None = None
    declared_sample_rate: int | None = None
    declared_bit_depth: int | None = None
    declared_channels: int | None = None
    sweep_start_ms: float | None = None
    sweep_end_ms: float | None = None
    silence_start_ms: float | None = None
    silence_end_ms: float | None = None
    delay_tolerance_samples: float | None = None
    phase_tolerance_samples: float | None = None
    validation_codes: list[str] = field(default_factory=list)


@dataclass
class TaskSpec:
    source_path: Path | None
    raw: dict[str, Any]
    task_id: str
    reference_track_id: str
    tracks: list[TrackSpec]
    bands: list[BandSpec]
    sweep_start_ms: float
    sweep_end_ms: float
    search_window_ms: float | None = None
    search_window_samples: float | None = None
    phase_unwrap_limit: float = math.pi
    fixed_point_scale: int = 1_000_000
    delay_tolerance_samples: float = 0.25
    phase_tolerance_samples: float = 0.75
    noise_gate_factor: float = 6.0
    correlation_threshold: float = 0.20
    phase_energy_floor: float = 0.02
    concurrency: int = 4
    memory_budget_bytes: int = 256 * 1024 * 1024
    allowed_sample_rates: tuple[int, ...] = ALLOWED_SAMPLE_RATES
    allowed_bit_depths: tuple[int, ...] = ALLOWED_BIT_DEPTHS

    @property
    def output_root_default(self) -> Path:
        return Path("output")

    def track_by_id(self, track_id: str) -> TrackSpec | None:
        for track in self.tracks:
            if track.track_id == track_id:
                return track
        return None


def _load_raw(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    try:
        with path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError as exc:
        raise TaskSchemaError(f"task JSON not found: {path}", {"path": str(path)}, Code.E_FILE) from exc
    except (json.JSONDecodeError, OSError) as exc:
        raise TaskSchemaError(f"cannot read task JSON: {path}", {"path": str(path)}, Code.E_SCHEMA) from exc
    if not isinstance(raw, dict):
        raise TaskSchemaError("task JSON root must be an object", {"path": str(path)})
    return raw


def _parse_bands(raw: dict[str, Any]) -> list[BandSpec]:
    raw_bands = pick(raw, "bands", "frequency_bands", default=[])
    if not isinstance(raw_bands, list) or not raw_bands:
        raise TaskSchemaError("at least one frequency band is required", code=Code.E_BAND)
    bands: list[BandSpec] = []
    for index, item in enumerate(raw_bands):
        if not isinstance(item, dict):
            raise TaskSchemaError(f"band {index} must be an object", code=Code.E_BAND)
        band_id = str(pick(item, "band_id", "id", "name", default=f"band_{index}"))
        min_hz = pick(item, "min_hz", "low_hz", "f_min", "min_frequency_hz")
        max_hz = pick(item, "max_hz", "high_hz", "f_max", "max_frequency_hz")
        if not is_non_negative_number(min_hz) or not is_positive_number(max_hz) or float(min_hz) >= float(max_hz):
            raise TaskSchemaError(
                f"invalid frequency range for band {band_id}",
                {"band_id": band_id, "min_hz": min_hz, "max_hz": max_hz},
                Code.E_BAND,
            )
        bands.append(BandSpec(band_id, int(pick(item, "band_index", "index", default=index)), float(min_hz), float(max_hz)))
    bands.sort(key=lambda band: (band.band_index, band.band_id))
    for index, band in enumerate(bands):
        band.band_index = index
    return bands


def _resolve_path(value: Any, task_path: Path | None) -> Path | None:
    if value is None:
        return None
    path = Path(str(value))
    if not path.is_absolute() and task_path is not None:
        path = task_path.parent / path
    return path


def _parse_track(item: Any, index: int, task_path: Path | None, seen_ids: set[str]) -> TrackSpec:
    if not isinstance(item, dict):
        raise TaskSchemaError(f"track entry {index} must be an object", code=Code.E_SCHEMA)
    track_id = pick(item, "track_id", "id", "name")
    if track_id is None or not str(track_id):
        track_id = f"<track_{index}>"
        codes = [Code.E_SCHEMA]
    else:
        track_id = str(track_id)
        codes = []
    if track_id in seen_ids:
        codes.append(Code.E_DUPLICATE)
    seen_ids.add(track_id)

    path_value = pick(item, "path", "file", "wav", "wav_path", "filename")
    path = _resolve_path(path_value, task_path)
    if path is None:
        codes.append(Code.E_FILE)

    expected_start = pick(item, "expected_start_ms", "expected_start_milliseconds", "start_ms")
    expected_start_ms = None
    if isinstance(expected_start, int) and not isinstance(expected_start, bool) and expected_start >= 0:
        expected_start_ms = expected_start
    else:
        codes.append(Code.E_START)

    declared_sr = pick(item, "sample_rate", "expected_sample_rate", "sampling_rate")
    if declared_sr is not None and not (isinstance(declared_sr, int) and not isinstance(declared_sr, bool) and declared_sr > 0):
        codes.append(Code.E_RATE)
        declared_sr = None

    declared_bits = pick(item, "bit_depth", "bits_per_sample", "bits")
    if declared_bits is not None and not (isinstance(declared_bits, int) and not isinstance(declared_bits, bool) and declared_bits > 0):
        codes.append(Code.E_BITS)
        declared_bits = None

    declared_channels = pick(item, "channels", "channel_count", "num_channels")
    if declared_channels is not None and not (
        isinstance(declared_channels, int) and not isinstance(declared_channels, bool) and declared_channels > 0
    ):
        codes.append(Code.E_CHANNELS)
        declared_channels = None

    sweep_start = pick(item, "sweep_start_ms", "chirp_start_ms")
    sweep_end = pick(item, "sweep_end_ms", "chirp_end_ms")

    return TrackSpec(
        track_id=track_id,
        path=path,
        raw=item,
        enabled=bool(pick(item, "enabled", default=True)),
        is_reference=bool(pick(item, "is_reference", "reference", default=False))
        or str(pick(item, "role", default="")).lower() == "reference",
        expected_start_ms=expected_start_ms,
        declared_sample_rate=declared_sr,
        declared_bit_depth=declared_bits,
        declared_channels=declared_channels,
        sweep_start_ms=float(sweep_start) if is_real_number(sweep_start) else None,
        sweep_end_ms=float(sweep_end) if is_real_number(sweep_end) else None,
        silence_start_ms=pick(item, "silence_start_ms"),
        silence_end_ms=pick(item, "silence_end_ms"),
        delay_tolerance_samples=pick(item, "delay_tolerance_samples"),
        phase_tolerance_samples=pick(item, "phase_tolerance_samples"),
        validation_codes=codes,
    )


def parse_task(raw: dict[str, Any], source_path: str | Path | None = None) -> TaskSpec:
    task_path = Path(source_path).resolve() if source_path is not None else None
    raw_tracks = pick(raw, "tracks", "items", "recordings", default=[])
    if not isinstance(raw_tracks, list) or not raw_tracks:
        raise TaskSchemaError("task must contain a non-empty tracks array", code=Code.E_SCHEMA)

    seen_ids: set[str] = set()
    tracks = [_parse_track(item, index, task_path, seen_ids) for index, item in enumerate(raw_tracks)]

    reference_id = pick(raw, "reference_track", "reference_id", "reference")
    role_refs = [track.track_id for track in tracks if track.is_reference and Code.E_DUPLICATE not in track.validation_codes]
    if reference_id is not None:
        reference_id = str(reference_id)
        referenced = next((track for track in tracks if track.track_id == reference_id), None)
        if referenced is None:
            raise TaskSchemaError(f"reference track does not exist: {reference_id}", code=Code.E_REF)
        referenced.is_reference = True
        role_refs = list(dict.fromkeys([reference_id, *role_refs]))
    if len(role_refs) != 1:
        raise TaskSchemaError(
            "task must declare exactly one enabled reference track",
            {"reference_candidates": role_refs},
            Code.E_REF,
        )
    reference_id = role_refs[0]
    reference = next(track for track in tracks if track.track_id == reference_id)
    if not reference.enabled:
        raise TaskSchemaError(f"reference track is disabled: {reference_id}", code=Code.E_REF)

    sweep_start = pick(raw, "sweep_start_ms", "chirp_start_ms")
    sweep_end = pick(raw, "sweep_end_ms", "chirp_end_ms")
    if not is_non_negative_number(sweep_start) or not is_positive_number(sweep_end) or float(sweep_start) >= float(sweep_end):
        raise TaskSchemaError("sweep_start_ms and sweep_end_ms must define a positive interval", code=Code.E_PARAM)

    search_samples = pick(raw, "search_window_samples", "correlation_window_samples", "search_window")
    search_ms = pick(raw, "search_window_ms", "correlation_window_ms")
    correlation = pick(raw, "correlation", default={})
    if isinstance(correlation, dict):
        search_samples = pick(correlation, "search_window_samples", "window_samples", default=search_samples)
        search_ms = pick(correlation, "search_window_ms", "window_ms", default=search_ms)
    if search_samples is None and search_ms is None:
        raise TaskSchemaError("a correlation search window is required", code=Code.E_WINDOW)
    if search_samples is not None and not is_positive_number(search_samples):
        raise TaskSchemaError("search window must be a finite positive number", code=Code.E_WINDOW)
    if search_ms is not None and not is_positive_number(search_ms):
        raise TaskSchemaError("search window ms must be a finite positive number", code=Code.E_WINDOW)

    unwrap_limit = pick(raw, "phase_unwrap_limit", "phase_unwrap_max", "unwrap_limit")
    phase = pick(raw, "phase", default={})
    if isinstance(phase, dict):
        unwrap_limit = pick(phase, "unwrap_limit", "unwrap_max", default=unwrap_limit)
    if unwrap_limit is None:
        unwrap_limit = math.pi
    if not is_positive_number(unwrap_limit):
        raise TaskSchemaError("phase unwrap limit must be a finite positive number", code=Code.E_UNWRAP)

    scale = pick(raw, "fixed_point_scale", "fixed_scale")
    fixed_point = pick(raw, "fixed_point", default={})
    if isinstance(fixed_point, dict):
        scale = pick(fixed_point, "scale", "delay_scale", default=scale)
    if not isinstance(scale, int) or isinstance(scale, bool) or scale <= 0:
        raise TaskSchemaError("fixed point scale must be a positive integer", {"scale": scale}, Code.E_SCALE)

    delay_tol = pick(raw, "delay_tolerance_samples", "delay_tolerance")
    if delay_tol is None and isinstance(fixed_point, dict):
        delay_tol = fixed_point.get("delay_tolerance_samples")
    if delay_tol is None:
        delay_tol = 0.25
    if not is_non_negative_number(delay_tol):
        raise TaskSchemaError("delay tolerance must be finite and non-negative", code=Code.E_TOLERANCE)

    phase_tol = pick(raw, "phase_tolerance_samples", "phase_tolerance")
    if phase_tol is None and isinstance(fixed_point, dict):
        phase_tol = fixed_point.get("phase_tolerance_samples")
    if phase_tol is None:
        phase_tol = 0.75
    if not is_non_negative_number(phase_tol):
        raise TaskSchemaError("phase tolerance must be finite and non-negative", code=Code.E_TOLERANCE)

    concurrency = pick(raw, "concurrency", "max_workers", "worker_count", default=min(4, os.cpu_count() or 1))
    if not isinstance(concurrency, int) or isinstance(concurrency, bool) or concurrency <= 0:
        raise TaskSchemaError("concurrency must be a positive integer", code=Code.E_CONCURRENCY)

    memory_budget = pick(raw, "memory_budget_bytes", "memory_budget", "max_memory_bytes", default=256 * 1024 * 1024)
    if not isinstance(memory_budget, int) or isinstance(memory_budget, bool) or memory_budget <= 0:
        raise TaskSchemaError("memory budget must be a positive integer number of bytes", code=Code.E_MEMORY_BUDGET)

    noise_gate = pick(raw, "noise_gate_factor", default=6.0)
    if not is_positive_number(noise_gate):
        raise TaskSchemaError("noise gate factor must be a finite positive number", code=Code.E_PARAM)

    corr_threshold = pick(raw, "correlation_threshold", default=0.20)
    if not is_real_number(corr_threshold) or not 0.0 <= float(corr_threshold) <= 1.0:
        raise TaskSchemaError("correlation threshold must be in [0, 1]", code=Code.E_PARAM)

    energy_floor = pick(raw, "phase_energy_floor", default=0.02)
    if not is_real_number(energy_floor) or not 0.0 < float(energy_floor) <= 1.0:
        raise TaskSchemaError("phase energy floor must be in (0, 1]", code=Code.E_PARAM)

    allowed_rates = tuple(pick(raw, "allowed_sample_rates", default=ALLOWED_SAMPLE_RATES))
    allowed_bits = tuple(pick(raw, "allowed_bit_depths", default=ALLOWED_BIT_DEPTHS))

    return TaskSpec(
        source_path=task_path,
        raw=raw,
        task_id=str(pick(raw, "task_id", "id", default="alignment-task")),
        reference_track_id=reference_id,
        tracks=tracks,
        bands=_parse_bands(raw),
        sweep_start_ms=float(sweep_start),
        sweep_end_ms=float(sweep_end),
        search_window_ms=float(search_ms) if search_ms is not None else None,
        search_window_samples=float(search_samples) if search_samples is not None else None,
        phase_unwrap_limit=float(unwrap_limit),
        fixed_point_scale=int(scale),
        delay_tolerance_samples=float(delay_tol),
        phase_tolerance_samples=float(phase_tol),
        noise_gate_factor=float(noise_gate),
        correlation_threshold=float(corr_threshold),
        phase_energy_floor=float(energy_floor),
        concurrency=int(concurrency),
        memory_budget_bytes=int(memory_budget),
        allowed_sample_rates=tuple(int(value) for value in allowed_rates),
        allowed_bit_depths=tuple(int(value) for value in allowed_bits),
    )


def load_task(path: str | Path) -> TaskSpec:
    source = Path(path)
    raw = _load_raw(source)
    return parse_task(raw, source)
