"""Track measurement orchestration and bounded concurrent scheduling."""
from __future__ import annotations

import os
import threading
import traceback
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .canonical import file_sha256, fixed_to_float, quantize_fixed, sqlite_safe_integer
from .codes import BusinessFailure, Code, CooperativeInterrupted, EngineError
from .dsp import (
    band_phase_delay,
    common_analysis_length,
    estimate_integer_delay,
    resample_to_rate,
    rms_level,
    peak_level,
    search_window_in_rate,
)
from .ledger import LedgerStore
from .memory import MemoryAccountant, estimate_track_bytes
from .results import BandResult, TrackResult
from .task_model import TaskSpec, TrackSpec, ms_to_samples
from .wav_io import WavData, WavHeader, read_wav, read_wav_header


class StopController:
    def __init__(self):
        self._event = threading.Event()
        self.reason: str | None = None
        self.signum: int | None = None
        self.stage: str | None = None

    def request(self, reason: str = "requested", signum: int | None = None) -> None:
        self.reason = reason
        self.signum = signum
        self._event.set()

    @property
    def requested(self) -> bool:
        return self._event.is_set()

    def check(
        self,
        stage: str,
        track_id: str | None = None,
        completed_bands: list[str] | None = None,
        completed_tracks: list[str] | None = None,
    ) -> None:
        if self.requested:
            self.stage = stage
            raise CooperativeInterrupted(
                stage=stage,
                track_id=track_id,
                completed_bands=list(completed_bands or []),
                completed_tracks=list(completed_tracks or []),
            )


@dataclass
class PreparedTrack:
    track: TrackSpec
    wav: WavData
    sweep_start_sample: int
    sweep_end_sample: int
    noise_rms: float
    excitation_peak: float


@dataclass
class ReferenceContext:
    track_id: str
    sample_rate: int
    analysis_excitation: np.ndarray
    reserved_bytes: int


@dataclass
class RunOutcome:
    input_digest: str
    results: list[TrackResult] = field(default_factory=list)
    interrupted: CooperativeInterrupted | None = None
    first_error: dict[str, Any] | None = None
    completed_track_ids: list[str] = field(default_factory=list)
    skipped_resume_ids: list[str] = field(default_factory=list)


def compute_input_digest(task: TaskSpec) -> str:
    audio: list[dict[str, str]] = []
    for track in task.tracks:
        if track.path is not None and track.path.is_file():
            digest = file_sha256(track.path)
        else:
            digest = "missing:" + str(track.path)
        audio.append({"track_id": track.track_id, "sha256": digest})
    from .canonical import canonical_digest

    return canonical_digest({"task": task.raw, "audio": audio})


def _error_result(track: TrackSpec, index: int, codes: list[str], details: dict[str, Any] | None = None) -> TrackResult:
    return TrackResult(
        track_id=track.track_id,
        track_index=index,
        status="rejected" if any(code in {Code.E_FILE, Code.E_WAV, Code.E_RATE, Code.E_BITS, Code.E_CHANNELS, Code.E_START} for code in codes) else "error",
        is_reference=track.is_reference,
        business_codes=sorted(set(codes)),
        expected_start_ms=track.expected_start_ms,
        error_details=details or {},
    )


def validate_and_prepare(track: TrackSpec, index: int, task: TaskSpec) -> tuple[TrackResult | None, PreparedTrack | None]:
    codes = list(track.validation_codes)
    if not track.enabled:
        codes.append(Code.E_DISABLED)
        return _error_result(track, index, codes, {"enabled": False}), None
    if track.path is None:
        codes.append(Code.E_FILE)
        return _error_result(track, index, codes), None

    header: WavHeader | None = None
    try:
        header = read_wav_header(track.path)
        if header.sample_rate not in task.allowed_sample_rates:
            codes.append(Code.E_RATE)
        if track.declared_sample_rate is not None and header.sample_rate != track.declared_sample_rate:
            codes.append(Code.E_RATE)
        if header.bit_depth not in task.allowed_bit_depths:
            codes.append(Code.E_BITS)
        if track.declared_bit_depth is not None and header.bit_depth != track.declared_bit_depth:
            codes.append(Code.E_BITS)
        if track.declared_channels is not None and header.channels != track.declared_channels:
            codes.append(Code.E_CHANNELS)
    except EngineError as exc:
        codes.append(exc.code)
        return _error_result(track, index, codes, exc.details | {"message": exc.message}), None

    if codes:
        details = {
            "actual_sample_rate": header.sample_rate if header else None,
            "actual_bit_depth": header.bit_depth if header else None,
            "actual_channels": header.channels if header else None,
            "actual_frames": header.frames if header else None,
        }
        result = _error_result(track, index, codes, details)
        if header is not None:
            result.input_frames = header.frames
            result.sample_rate = header.sample_rate
            result.channels = header.channels
            result.bit_depth = header.bit_depth
        return result, None

    try:
        wav = read_wav(track.path)
        sweep_start_ms = track.sweep_start_ms if track.sweep_start_ms is not None else task.sweep_start_ms
        sweep_end_ms = track.sweep_end_ms if track.sweep_end_ms is not None else task.sweep_end_ms
        start_sample = ms_to_samples(sweep_start_ms, header.sample_rate)
        end_sample = ms_to_samples(sweep_end_ms, header.sample_rate)
        frame_count = header.frames
        if not (0 <= start_sample < end_sample <= frame_count):
            raise BusinessFailure(
                Code.E_SEGMENT,
                "sweep boundaries are outside the WAV frame range",
                {
                    "start_sample": start_sample,
                    "end_sample": end_sample,
                    "frame_count": frame_count,
                },
            )

        silence = wav.mono[0:start_sample]
        excitation = wav.mono[start_sample:end_sample]
        noise_rms = rms_level(silence)
        excitation_peak = peak_level(excitation)
        prepared = PreparedTrack(
            track=track,
            wav=wav,
            sweep_start_sample=start_sample,
            sweep_end_sample=end_sample,
            noise_rms=noise_rms,
            excitation_peak=excitation_peak,
        )
        return None, prepared
    except BusinessFailure as exc:
        codes.append(exc.code)
        return _error_result(track, index, codes, exc.details | {"message": exc.message}), None
    except EngineError as exc:
        codes.append(exc.code)
        return _error_result(track, index, codes, exc.details | {"message": exc.message}), None


def _base_result(prepared: PreparedTrack, index: int, task: TaskSpec) -> TrackResult:
    track = prepared.track
    header = prepared.wav.header
    return TrackResult(
        track_id=track.track_id,
        track_index=index,
        status="measured",
        is_reference=track.is_reference,
        input_frames=header.frames,
        sample_rate=header.sample_rate,
        channels=header.channels,
        bit_depth=header.bit_depth,
        expected_start_ms=track.expected_start_ms,
        sweep_start_ms=track.sweep_start_ms if track.sweep_start_ms is not None else task.sweep_start_ms,
        sweep_end_ms=track.sweep_end_ms if track.sweep_end_ms is not None else task.sweep_end_ms,
        silence_start_sample=0,
        silence_end_sample=prepared.sweep_start_sample,
        excitation_start_sample=prepared.sweep_start_sample,
        excitation_end_sample=prepared.sweep_end_sample,
        tail_end_sample=header.frames,
        noise_rms=prepared.noise_rms,
        excitation_peak=prepared.excitation_peak,
        noise_gate_threshold=task.noise_gate_factor * prepared.noise_rms,
    )


def measure_prepared(
    prepared: PreparedTrack,
    index: int,
    task: TaskSpec,
    reference: ReferenceContext | None,
    stop: StopController | None = None,
    band_callback: BandCallback | None = None,
) -> TrackResult:
    stop = stop or StopController()
    track = prepared.track
    result = _base_result(prepared, index, task)
    codes: list[str] = []

    try:
        stop.check("track", track.track_id)
        header = prepared.wav.header
        target_rate = reference.sample_rate if reference is not None else header.sample_rate
        if reference is None and not track.is_reference:
            raise BusinessFailure(Code.E_REF, "reference analysis context is unavailable")

        candidate_excitation = resample_to_rate(
            prepared.wav.mono[prepared.sweep_start_sample : prepared.sweep_end_sample],
            header.sample_rate,
            target_rate,
        )
        reference_excitation = (
            reference.analysis_excitation
            if reference is not None
            else resample_to_rate(
                prepared.wav.mono[prepared.sweep_start_sample : prepared.sweep_end_sample],
                header.sample_rate,
                target_rate,
            )
        )
        length = common_analysis_length(candidate_excitation, reference_excitation)
        candidate_equal = candidate_excitation[:length]
        reference_equal = reference_excitation[:length]
        result.analysis_sample_rate = target_rate
        result.analysis_samples = length

        window = search_window_in_rate(task.search_window_samples, task.search_window_ms, target_rate)
        result.search_window_samples = window

        gate_threshold = task.noise_gate_factor * prepared.noise_rms
        result.noise_gate_threshold = float(gate_threshold)
        if prepared.excitation_peak < gate_threshold:
            raise BusinessFailure(
                Code.E_PEAK,
                "excitation peak is below the background-noise gate",
                {
                    "noise_rms": prepared.noise_rms,
                    "excitation_peak": prepared.excitation_peak,
                    "noise_gate_threshold": float(gate_threshold),
                },
            )

        delay = estimate_integer_delay(candidate_equal, reference_equal, window, task.correlation_threshold)
        result.correlation_peak = delay.correlation_peak
        result.delay_samples = delay.delay_samples
        result.delay_float_samples = float(delay.delay_float_samples)
        result.delay_float_delta_samples = float(delay.delta_samples)
        result.delay_abs_error_samples = abs(float(delay.delay_float_samples) - int(delay.delay_samples))

        integer_q = sqlite_safe_integer(int(delay.delay_samples) * int(task.fixed_point_scale))
        float_q = sqlite_safe_integer(quantize_fixed(delay.delay_float_samples, task.fixed_point_scale))
        tolerance_q = sqlite_safe_integer(quantize_fixed(task.delay_tolerance_samples, task.fixed_point_scale))
        result.integer_delay_q = integer_q
        result.float_delay_q = float_q
        result.delay_delta_q = abs(float_q - integer_q)
        result.delay_tolerance_q = tolerance_q
        result.delay_fixed_samples = fixed_to_float(float_q, task.fixed_point_scale)
        result.delay_quantization_error = float(
            result.delay_float_samples - result.delay_fixed_samples
        )
        result.delay_pass = result.delay_delta_q <= tolerance_q
        if not result.delay_pass:
            codes.append(Code.E_DELAY)

        phase_tolerance_q = sqlite_safe_integer(quantize_fixed(task.phase_tolerance_samples, task.fixed_point_scale))
        completed_bands: list[str] = []
        for band_spec in task.bands:
            stop.check("band", track.track_id, completed_bands)
            band_result = BandResult(
                band_id=band_spec.band_id,
                band_index=band_spec.band_index,
                min_hz=band_spec.min_hz,
                max_hz=band_spec.max_hz,
                integer_delay_q=integer_q,
                phase_tolerance_q=phase_tolerance_q,
            )
            try:
                slope, phase_delay, point_count = band_phase_delay(
                    reference_equal,
                    candidate_equal,
                    target_rate,
                    band_spec.min_hz,
                    band_spec.max_hz,
                    task.phase_energy_floor,
                    task.phase_unwrap_limit,
                )
                phase_q = sqlite_safe_integer(quantize_fixed(phase_delay, task.fixed_point_scale))
                error_q = abs(phase_q - integer_q)
                band_result.point_count = point_count
                band_result.slope_rad_per_hz = float(slope)
                band_result.slope_q = sqlite_safe_integer(
                    quantize_fixed(slope, task.fixed_point_scale)
                )
                band_result.slope_quantization_error = float(
                    slope - fixed_to_float(band_result.slope_q, task.fixed_point_scale)
                )
                band_result.phase_delay_samples = float(phase_delay)
                band_result.phase_delay_q = phase_q
                band_result.phase_delay_fixed_samples = fixed_to_float(
                    phase_q, task.fixed_point_scale
                )
                band_result.phase_delay_quantization_error = float(
                    phase_delay - band_result.phase_delay_fixed_samples
                )
                band_result.phase_error_q = error_q
                band_result.passed = error_q <= phase_tolerance_q
                if not band_result.passed:
                    band_result.code = Code.E_PHASE
                    codes.append(Code.E_PHASE)
            except BusinessFailure as exc:
                band_result.code = exc.code
                band_result.point_count = int(exc.details.get("point_count", 0) or 0)
                band_result.passed = False
                codes.append(exc.code)
                result.error_details.setdefault("band_failures", []).append(
                    {"band_id": band_spec.band_id, "code": exc.code, **exc.details}
                )
            result.bands.append(band_result)
            completed_bands.append(band_spec.band_id)
            if band_callback is not None:
                band_callback(track.track_id, band_spec.band_id, band_spec.band_index)

        result.business_codes = sorted(set(codes))
        result.alignment_pass = result.delay_pass and all(band.passed for band in result.bands) and not result.business_codes
        if Code.E_PHASE in result.business_codes:
            result.alignment_pass = False
        return result
    except CooperativeInterrupted:
        raise
    except BusinessFailure as exc:
        result.business_codes = sorted(set(codes + [exc.code]))
        result.status = "error"
        result.alignment_pass = False
        details = dict(exc.details)
        details.setdefault("message", exc.message)
        result.error_details.update(details)
        if exc.code == Code.E_PEAK:
            result.noise_rms = prepared.noise_rms
            result.excitation_peak = prepared.excitation_peak
            result.noise_gate_threshold = task.noise_gate_factor * prepared.noise_rms
        return result


def estimate_for_track(track: TrackSpec) -> int:
    if track.path is not None:
        try:
            header = read_wav_header(track.path)
            return estimate_track_bytes(header.frames, header.channels)
        except Exception:
            pass
    return estimate_track_bytes(48_000 * 2, 2)


class MeasurementEngine:
    def __init__(
        self,
        task: TaskSpec,
        ledger: LedgerStore,
        stop: StopController | None = None,
        band_callback: BandCallback | None = None,
        fault_exit_after_commit: int | None = None,
    ):
        self.task = task
        self.ledger = ledger
        self.stop = stop or StopController()
        self.band_callback = band_callback
        self.fault_exit_after_commit = fault_exit_after_commit
        self._commit_count = 0
        self.accountant = MemoryAccountant(task.memory_budget_bytes)

    def _commit(self, input_digest: str, result: TrackResult) -> None:
        self.ledger.commit_track(input_digest, result)
        self._commit_count += 1
        if self.fault_exit_after_commit is not None and self._commit_count >= self.fault_exit_after_commit:
            os._exit(9)

    def _prepare_reference(self, input_digest: str, outcome: RunOutcome) -> ReferenceContext | None:
        reference_track = self.task.track_by_id(self.task.reference_track_id)
        if reference_track is None:
            raise EngineError(Code.E_REF, "reference track is missing from task")
        index = next(index for index, track in enumerate(self.task.tracks) if track.track_id == reference_track.track_id)
        self.stop.check("track", reference_track.track_id, completed_tracks=outcome.completed_track_ids)

        existing = self.ledger.get_track(input_digest, reference_track.track_id)
        error_result, prepared = validate_and_prepare(reference_track, index, self.task)
        if error_result is not None or prepared is None:
            if error_result is not None:
                self._commit(input_digest, error_result)
                outcome.results.append(error_result)
            outcome.first_error = {
                "code": Code.E_REF,
                "message": "reference track is not measurable",
                "track_id": reference_track.track_id,
            }
            return None

        assert prepared is not None
        reference_excitation = resample_to_rate(
            prepared.wav.mono[prepared.sweep_start_sample : prepared.sweep_end_sample],
            prepared.wav.header.sample_rate,
            prepared.wav.header.sample_rate,
        )
        reserved = reference_excitation.nbytes
        self.accountant.reserve(reserved)

        if existing is not None and existing.complete:
            result = existing
        else:
            result = measure_prepared(prepared, index, self.task, None, self.stop, self.band_callback)
            self._commit(input_digest, result)
        outcome.results.append(result)
        if result.complete:
            outcome.completed_track_ids.append(result.track_id)
        return ReferenceContext(
            track_id=reference_track.track_id,
            sample_rate=prepared.wav.header.sample_rate,
            analysis_excitation=reference_excitation,
            reserved_bytes=reserved,
        )

    def run(self, input_digest: str, resume: bool = True) -> RunOutcome:
        outcome = RunOutcome(input_digest=input_digest)
        try:
            reference = self._prepare_reference(input_digest, outcome)
        except CooperativeInterrupted as interrupt:
            outcome.interrupted = interrupt
            return outcome
        if reference is None:
            return outcome

        candidate_entries = [
            (index, track)
            for index, track in enumerate(self.task.tracks)
            if track.track_id != self.task.reference_track_id
        ]
        pending: list[tuple[int, TrackSpec]] = []
        for index, track in candidate_entries:
            if resume and self.ledger.has_complete_track(input_digest, track.track_id):
                stored = self.ledger.get_track(input_digest, track.track_id)
                if stored is not None:
                    outcome.results.append(stored)
                    outcome.completed_track_ids.append(track.track_id)
                    outcome.skipped_resume_ids.append(track.track_id)
                    continue
            pending.append((index, track))
        pending = sorted(pending, key=lambda item: item[1].track_id)
        max_workers = max(1, min(self.task.concurrency, len(pending) or 1))
        executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="alignment-track")
        futures: dict[Any, tuple[str, int]] = {}
        estimates: dict[str, int] = {
            track.track_id: estimate_for_track(track) for _, track in candidate_entries
        }

        def run_one(index: int, track: TrackSpec) -> TrackResult:
            error_result, prepared = validate_and_prepare(track, index, self.task)
            if error_result is not None or prepared is None:
                return error_result  # type: ignore[return-value]
            return measure_prepared(prepared, index, self.task, reference, self.stop, self.band_callback)

        try:
            while pending or futures:
                self.stop.check("track_boundary")
                while pending and len(futures) < max_workers:
                    index, track = pending[0]
                    estimate = estimates[track.track_id]
                    if estimate > self.accountant.budget_bytes:
                        memory_error = _error_result(
                            track,
                            index,
                            [Code.E_MEMORY],
                            {
                                "required_bytes": estimate,
                                "budget_bytes": self.accountant.budget_bytes,
                                "order": "track_id",
                            },
                        )
                        self._commit(input_digest, memory_error)
                        outcome.results.append(memory_error)
                        pending.pop(0)
                        continue
                    if not self.accountant.can_fit(estimate):
                        break
                    self.accountant.reserve(estimate)
                    future = executor.submit(run_one, index, track)
                    futures[future] = (track.track_id, estimate)
                    pending.pop(0)

                if not futures:
                    if pending:
                        # Defensive path: the head item cannot fit but is not over budget.
                        _, track = pending.pop(0)
                        outcome.first_error = {
                            "code": Code.E_MEMORY,
                            "message": "memory scheduler made no progress",
                            "track_id": track.track_id,
                        }
                    break

                done, _ = wait(futures, timeout=0.05, return_when=FIRST_COMPLETED)
                if not done:
                    continue

                for future in done:
                    track_id, estimate = futures.pop(future)
                    try:
                        result = future.result()
                        self._commit(input_digest, result)
                        outcome.results.append(result)
                        if result.complete:
                            outcome.completed_track_ids.append(track_id)
                    except CooperativeInterrupted as interrupt:
                        outcome.interrupted = interrupt
                        self.stop.request("interrupted")
                    except Exception as exc:  # unexpected: cancel all remaining work
                        if outcome.first_error is None:
                            error = exc if isinstance(exc, EngineError) else EngineError(Code.E_INTERNAL, str(exc))
                            details = dict(error.details)
                            details["traceback"] = traceback.format_exc()
                            outcome.first_error = {
                                "code": error.code,
                                "message": error.message,
                                "track_id": track_id,
                                "details": details,
                            }
                        self.stop.request("cancel")
                    finally:
                        self.accountant.release(estimate)

                if outcome.first_error is not None or outcome.interrupted is not None:
                    pending.clear()
                    for future in futures:
                        future.cancel()
                    break
        except CooperativeInterrupted as interrupt:
            outcome.interrupted = interrupt
        finally:
            for future in list(futures):
                future.cancel()
            executor.shutdown(wait=True, cancel_futures=True)
            self.accountant.release(reference.reserved_bytes)

        return outcome
