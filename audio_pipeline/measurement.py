"""Two-stage gated loudness and short-term dynamic-range measurements."""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from typing import Any

import numpy as np

from .errors import InterruptedRun, PipelineError
from .models import InputSpec, Measurement, MeasurementDefaults, UnifiedView
from .snapshots import resolve_numeric, resolve_window
from .state import StateStore
from .util import rounded
from .wavio import WavInfo, decode_wav


LUFS_OFFSET = -0.691
ENERGY_FLOOR = 1e-30


def energy_to_lufs(energy: float) -> float | None:
    if not math.isfinite(energy) or energy <= 0.0:
        return None
    return LUFS_OFFSET + 10.0 * math.log10(max(energy, ENERGY_FLOOR))


def lufs_to_energy(lufs: float) -> float:
    return 10.0 ** ((lufs - LUFS_OFFSET) / 10.0)


@dataclass
class StopController:
    """Cancellation and signal state checked only at safe boundaries."""

    event: threading.Event
    lock: threading.Lock
    signum: int | None = None
    fatal: PipelineError | None = None
    interrupt_after_chunks: int | None = None
    committed_chunks: int = 0
    last_input_id: str | None = None
    last_boundary: str | None = None
    last_details: dict[str, Any] | None = None

    @classmethod
    def create(cls) -> "StopController":
        return cls(threading.Event(), threading.Lock())

    def request_signal(self, signum: int) -> None:
        with self.lock:
            if self.signum is None:
                self.signum = signum
        self.event.set()

    def cancel(self, error: PipelineError) -> None:
        with self.lock:
            if self.fatal is None:
                self.fatal = error
        self.event.set()

    def signal(self) -> int | None:
        with self.lock:
            return self.signum

    def boundary(self, *, input_id: str, boundary: str,
                 details: dict[str, Any] | None = None) -> None:
        with self.lock:
            signum = self.signum
            fatal = self.fatal
            self.last_input_id = input_id
            self.last_boundary = boundary
            self.last_details = dict(details or {})
            if signum is not None:
                raise InterruptedRun(signum, input_id=input_id, boundary=boundary,
                                     details=details)
            if fatal is not None:
                raise PipelineError(
                    "E_INPUT",
                    f"cancelled after another input failed: {fatal.message}",
                    input_id=input_id,
                    details={"first_error": fatal.as_dict()},
                    recoverable=False,
                )

    def chunk_committed(self) -> None:
        with self.lock:
            self.committed_chunks += 1
            should_interrupt = (
                self.interrupt_after_chunks is not None
                and self.committed_chunks >= self.interrupt_after_chunks
            )
            if should_interrupt and self.signum is None:
                self.signum = 15
        if should_interrupt:
            self.event.set()


def _window_bounds(frames: int, window_frames: int, hop_frames: int,
                   policy: str) -> list[tuple[int, int, int, bool]]:
    if frames <= 0:
        return []
    result: list[tuple[int, int, int, bool]] = []
    start = 0
    while start < frames:
        available = min(window_frames, frames - start)
        partial = available < window_frames
        if partial and policy == "drop":
            break
        result.append((start, start + available, available, partial))
        if start + window_frames >= frames:
            break
        start += hop_frames
        if hop_frames <= 0:
            break
    return result


def compute_window_records(samples: np.ndarray, weights: tuple[float, ...],
                           window_frames: int, hop_frames: int, policy: str,
                           start_index: int = 0) -> list[dict[str, Any]]:
    if samples.ndim != 2:
        raise PipelineError("E_WAV", "decoded samples must be two-dimensional")
    frames = samples.shape[0]
    bounds = _window_bounds(frames, window_frames, hop_frames, policy)
    if start_index >= len(bounds):
        return []
    weight_array = np.asarray(weights, dtype=np.float64)
    weight_sum = float(weight_array.sum())
    records: list[dict[str, Any]] = []
    for index in range(start_index, len(bounds)):
        start, end, valid_frames, partial = bounds[index]
        block = samples[start:end]
        if partial and policy == "zero_pad":
            channel_mse = np.mean(block.astype(np.float64) ** 2, axis=0,
                                  dtype=np.float64)
            # Zero padding contributes samples but no energy.
            channel_mse *= valid_frames / float(window_frames)
            divisor = window_frames
        else:
            channel_mse = np.mean(block.astype(np.float64) ** 2, axis=0,
                                  dtype=np.float64)
            divisor = valid_frames
        weighted = float(np.dot(weight_array, channel_mse) / weight_sum)
        lufs = energy_to_lufs(weighted)
        records.append({
            "index": index,
            "start_frame": start,
            "end_frame": end,
            "valid_frames": valid_frames,
            "divisor_frames": divisor,
            "partial": partial,
            "energy": round(weighted, 15),
            "lufs": rounded(lufs, 12),
        })
    return records


def _aggregate(measurement: Measurement, windows: list[dict[str, Any]],
               absolute_gate_lufs: float, relative_gate_lu: float) -> None:
    measurement.windows = windows
    measurement.total_windows = len(windows)
    if not windows:
        measurement.silent = False
        measurement.gated_window_count = 0
        measurement.abs_gated_window_count = 0
        measurement.integrated_lufs = None
        measurement.short_term_peak_lufs = None
        measurement.short_term_floor_lufs = None
        measurement.dynamic_range_db = None
        measurement.code = "E_WINDOW"
        return
    energies = [float(window["energy"]) for window in windows]
    lufs_values = [
        float(window["lufs"]) for window in windows if window["lufs"] is not None
    ]
    measurement.silent = not any(energy > 0.0 for energy in energies)
    abs_passed = [
        value for value in lufs_values
        if value >= absolute_gate_lufs
    ]
    measurement.abs_gated_window_count = len(abs_passed)
    if not abs_passed:
        measurement.gated_window_count = 0
        measurement.integrated_lufs = None
        measurement.short_term_peak_lufs = None
        measurement.short_term_floor_lufs = None
        measurement.dynamic_range_db = None
        measurement.code = "E_SILENT" if measurement.silent else "E_BELOW_GATE"
        return
    abs_energy = float(np.mean([lufs_to_energy(value) for value in abs_passed]))
    abs_loudness = energy_to_lufs(abs_energy)
    assert abs_loudness is not None
    relative_threshold = abs_loudness + relative_gate_lu
    gated_lufs = [value for value in abs_passed if value >= relative_threshold]
    if not gated_lufs:
        measurement.gated_window_count = 0
        measurement.integrated_lufs = None
        measurement.short_term_peak_lufs = rounded(max(abs_passed), 6)
        measurement.short_term_floor_lufs = rounded(min(abs_passed), 6)
        measurement.dynamic_range_db = rounded(max(abs_passed) - min(abs_passed), 6)
        measurement.code = "E_BELOW_GATE"
        return
    gated_energy = float(np.mean([lufs_to_energy(value) for value in gated_lufs]))
    integrated = energy_to_lufs(gated_energy)
    peak = max(abs_passed)
    floor = min(abs_passed)
    measurement.gated_window_count = len(gated_lufs)
    measurement.integrated_lufs = rounded(integrated, 6)
    measurement.short_term_peak_lufs = rounded(peak, 6)
    measurement.short_term_floor_lufs = rounded(floor, 6)
    measurement.dynamic_range_db = rounded(peak - floor, 6)
    measurement.code = None


def measure_input(
    *,
    spec: InputSpec,
    info: WavInfo,
    defaults: MeasurementDefaults,
    view: UnifiedView,
    state: StateStore,
    stop: StopController,
    interrupt_after_chunks: int | None = None,
) -> tuple[Measurement, np.ndarray | None, list[dict[str, Any]], list[dict[str, Any]]]:
    window_ms, hop_ms, policy_override = resolve_window(
        view, spec.canonical_id, defaults.window_ms, defaults.hop_ms
    )
    policy = policy_override or defaults.partial_window_policy
    window_frames = max(1, int(round(window_ms * info.sample_rate / 1000.0)))
    hop_frames = max(1, int(round(hop_ms * info.sample_rate / 1000.0)))
    relative_gate = resolve_numeric(
        view, spec.canonical_id, "relative_gate",
        ("threshold", "threshold_db", "threshold_lufs", "relative_gate_lu",
         "relative_gate_db"),
        defaults.relative_gate_lu,
    )
    absolute_gate = resolve_numeric(
        view, spec.canonical_id, "absolute_gate",
        ("threshold_lufs", "absolute_gate_lufs", "threshold_db"),
        defaults.absolute_gate_lufs,
    )
    bounds = _window_bounds(info.frames, window_frames, hop_frames, policy)
    total_windows = len(bounds)
    key = state.chunk_key(
        spec.input_id, info.sha256, view.measurement_revision,
        defaults.checkpoint_windows,
    )
    cached, state_issues, next_window, complete = state.load_chunks(
        key=key,
        input_id=spec.input_id,
        input_sha256=info.sha256,
        measurement_revision=view.measurement_revision,
        total_windows=total_windows,
        frame_count=info.frames,
        window_frames=window_frames,
        hop_frames=hop_frames,
    )
    measurement = Measurement(
        input_id=spec.input_id,
        canonical_id=spec.canonical_id,
        source_sample_rate=info.sample_rate,
        channels=info.channels,
        source_bit_depth=info.bits_per_sample,
        frames=info.frames,
        window_frames=window_frames,
        hop_frames=hop_frames,
        window_ms=window_ms,
        hop_ms=hop_ms,
        partial_window_policy=policy,
        relative_gate_lu=relative_gate,
        absolute_gate_lufs=absolute_gate,
        total_windows=total_windows,
        windows=[],
        integrated_lufs=None,
        short_term_peak_lufs=None,
        short_term_floor_lufs=None,
        dynamic_range_db=None,
        gated_window_count=0,
        abs_gated_window_count=0,
        silent=False,
        code=None,
        measurement_reused=False,
    )
    chunk_records: list[dict[str, Any]] = []
    samples: np.ndarray | None = None
    if complete:
        _aggregate(measurement, cached, absolute_gate, relative_gate)
        measurement.measurement_reused = True
        return measurement, None, state_issues, chunk_records

    # Prefix windows are retained; only decode and recompute if the cache cannot
    # provide a complete measurement. This keeps recovery deterministic.
    stop.boundary(input_id=spec.input_id, boundary="before-decode",
                  details={"resume_window": next_window})
    samples = decode_wav(info, block_frames=defaults.decode_block_frames)
    records = list(cached)
    current_index = next_window
    while current_index < total_windows:
        end_index = min(total_windows, current_index + defaults.checkpoint_windows)
        new_records = compute_window_records(
            samples,
            spec.weights,
            window_frames,
            hop_frames,
            policy,
            start_index=current_index,
        )
        new_records = [
            record for record in new_records if current_index <= record["index"] < end_index
        ]
        if not new_records:
            break
        start = new_records[0]["index"]
        end = new_records[-1]["index"] + 1
        chunk_index = start // defaults.checkpoint_windows
        chunk = state.save_chunk(
            key=key,
            index=chunk_index,
            input_id=spec.input_id,
            input_sha256=info.sha256,
            measurement_revision=view.measurement_revision,
            total_windows=total_windows,
            total_frames=info.frames,
            window_frames=window_frames,
            hop_frames=hop_frames,
            start_window=start,
            end_window=end,
            windows=new_records,
        )
        chunk_records.append(chunk)
        records.extend(new_records)
        current_index = end
        stop.chunk_committed()
        stop.boundary(
            input_id=spec.input_id,
            boundary="window-chunk",
            details={"next_window": current_index, "total_windows": total_windows},
        )
    _aggregate(measurement, records, absolute_gate, relative_gate)
    return measurement, samples, state_issues, chunk_records


def interpolate_measurement_from_windows(
    spec: InputSpec,
    info: WavInfo,
    defaults: MeasurementDefaults,
    view: UnifiedView,
    windows: list[dict[str, Any]],
    *,
    reused: bool,
) -> Measurement:
    window_ms, hop_ms, policy_override = resolve_window(
        view, spec.canonical_id, defaults.window_ms, defaults.hop_ms
    )
    policy = policy_override or defaults.partial_window_policy
    window_frames = max(1, int(round(window_ms * info.sample_rate / 1000.0)))
    hop_frames = max(1, int(round(hop_ms * info.sample_rate / 1000.0)))
    relative_gate = resolve_numeric(
        view, spec.canonical_id, "relative_gate",
        ("threshold", "threshold_db", "threshold_lufs", "relative_gate_lu",
         "relative_gate_db"),
        defaults.relative_gate_lu,
    )
    absolute_gate = resolve_numeric(
        view, spec.canonical_id, "absolute_gate",
        ("threshold_lufs", "absolute_gate_lufs", "threshold_db"),
        defaults.absolute_gate_lufs,
    )
    measurement = Measurement(
        input_id=spec.input_id,
        canonical_id=spec.canonical_id,
        source_sample_rate=info.sample_rate,
        channels=info.channels,
        source_bit_depth=info.bits_per_sample,
        frames=info.frames,
        window_frames=window_frames,
        hop_frames=hop_frames,
        window_ms=window_ms,
        hop_ms=hop_ms,
        partial_window_policy=policy,
        relative_gate_lu=relative_gate,
        absolute_gate_lufs=absolute_gate,
        total_windows=len(windows),
        windows=[],
        integrated_lufs=None,
        short_term_peak_lufs=None,
        short_term_floor_lufs=None,
        dynamic_range_db=None,
        gated_window_count=0,
        abs_gated_window_count=0,
        silent=False,
        code=None,
        measurement_reused=reused,
    )
    _aggregate(measurement, windows, absolute_gate, relative_gate)
    measurement.measurement_reused = reused
    return measurement
