from __future__ import annotations

import math
import threading

import numpy as np

from .audio import read_wav_interval, wav_mean
from .errors import LedgerError, RepairError
from .kernels import (
    DenoiseModel,
    apply_normalization,
    declick_interval,
    normalization_gain,
    remove_dc,
)
from .ledger import Ledger
from .task import RecordingSpec


class RecordingRuntime:
    def __init__(
        self,
        spec: RecordingSpec,
        ledger: Ledger,
        run_id: str,
    ) -> None:
        self.spec = spec
        self.ledger = ledger
        self.run_id = run_id
        self._lock = threading.RLock()
        self._ranges = {stage: spec.block_ranges(stage) for stage in range(4)}
        self._completed = {
            stage: ledger.block_indices(spec.recording_id, stage) for stage in range(4)
        }
        self._validate_existing()
        self.prior_counts = {
            stage: len(self._completed[stage]) for stage in range(4)
        }
        self._global_mean: float | None = None
        self._denoise_model = DenoiseModel.build(spec.sample_count, spec.denoise)
        self._noise_profile: np.ndarray | None = None
        self._gain: float | None = None
        self._denoise_peak: float | None = None

    @property
    def recording_id(self) -> str:
        return self.spec.recording_id

    @property
    def sample_count(self) -> int:
        return self.spec.sample_count

    def _validate_existing(self) -> None:
        for stage, ranges in self._ranges.items():
            expected_count = len(ranges)
            indices = self._completed[stage]
            if any(index < 0 or index >= expected_count for index in indices):
                raise LedgerError(
                    "E_LEDGER_CORRUPT",
                    f"out-of-range block index for {self.recording_id}/stage {stage}",
                )
            for index in sorted(indices):
                record = self.ledger.block_record(self.recording_id, stage, index)
                expected_range = ranges[index]
                if (
                    record.start_sample != expected_range[0]
                    or record.end_sample != expected_range[1]
                    or record.sample_count != expected_range[1] - expected_range[0]
                ):
                    raise LedgerError(
                        "E_LEDGER_CORRUPT",
                        f"range mismatch for {self.recording_id}/{stage}/{index}",
                    )

    def stage_complete(self, stage: int) -> bool:
        return len(self._completed[stage]) == len(self._ranges[stage])

    def all_complete(self) -> bool:
        return all(self.stage_complete(stage) for stage in range(4))

    def completed_count(self, stage: int) -> int:
        return len(self._completed[stage])

    def expected_count(self, stage: int) -> int:
        return len(self._ranges[stage])

    def block_ranges(self, stage: int) -> list[tuple[int, int]]:
        return self._ranges[stage]

    def is_completed(self, stage: int, block_index: int) -> bool:
        return block_index in self._completed[stage]

    def mark_completed(self, stage: int, block_index: int) -> None:
        self._completed[stage].add(block_index)

    def block_range(self, stage: int, block_index: int) -> tuple[int, int]:
        return self._ranges[stage][block_index]

    def read_stage_interval(self, stage: int, start: int, end: int) -> np.ndarray:
        if start < 0 or end > self.sample_count or end <= start:
            raise RepairError(
                "E_SAMPLE_RANGE",
                f"invalid interval [{start}, {end}) for {self.recording_id}",
            )
        ranges = self._ranges[stage]
        first = max(0, start // self.spec.stage_block_frames(stage))
        output = np.empty(end - start, dtype=np.float64)
        cursor = start
        index = first
        while cursor < end:
            if index >= len(ranges):
                raise LedgerError(
                    "E_CONTEXT_SHORT_READ",
                    f"stage {stage} does not cover [{start}, {end})",
                )
            block_start, block_end = ranges[index]
            if block_end <= cursor:
                index += 1
                continue
            if block_start > cursor:
                raise LedgerError(
                    "E_CONTEXT_SHORT_READ",
                    f"gap before block {index} in stage {stage}",
                )
            values = self.ledger.fetch_block(self.recording_id, stage, index)
            overlap_end = min(end, block_end)
            source_start = cursor - block_start
            source_end = overlap_end - block_start
            target_start = cursor - start
            target_end = overlap_end - start
            output[target_start:target_end] = values[source_start:source_end]
            cursor = overlap_end
            index += 1
        return output

    def _dc_mean(self) -> float:
        with self._lock:
            if self._global_mean is None:
                cached = self.ledger.get_stage_metric(self.recording_id, 0, "dc_mean")
                if cached is None:
                    self._global_mean = wav_mean(self.spec.wav_path)
                    self.ledger.put_stage_metric(
                        recording_id=self.recording_id,
                        stage=0,
                        metric_key="dc_mean",
                        value=self._global_mean,
                        run_id=self.run_id,
                    )
                else:
                    self._global_mean = float(cached)
            return self._global_mean

    def _noise(self) -> np.ndarray:
        with self._lock:
            if self._noise_profile is None:
                self._noise_profile = self._denoise_model.noise_profile(
                    lambda start, end: self.read_stage_interval(1, start, end)
                )
            return self._noise_profile

    def ensure_denoise_metrics(self) -> tuple[float, float]:
        with self._lock:
            if self._denoise_peak is not None and self._gain is not None:
                return self._denoise_peak, self._gain
            cached_peak = self.ledger.get_stage_metric(
                self.recording_id, 2, "denoise_peak"
            )
            cached_gain = self.ledger.get_stage_metric(
                self.recording_id, 3, "normalization_gain"
            )
            if cached_peak is not None and cached_gain is not None:
                self._denoise_peak = float(cached_peak)
                self._gain = float(cached_gain)
                return self._denoise_peak, self._gain

            peak = 0.0
            for block_index, _ in enumerate(self._ranges[2]):
                values = self.ledger.fetch_block(self.recording_id, 2, block_index)
                peak = max(peak, float(np.max(np.abs(values))))
            gain = (
                1.0
                if peak <= 1e-15
                else normalization_gain(
                    np.asarray([peak], dtype=np.float64), self.spec.normalize
                )
            )
            if not math.isfinite(gain):
                raise RepairError("E_NORMALIZE_NONFINITE", "normalization gain is not finite")
            self.ledger.put_stage_metric(
                recording_id=self.recording_id,
                stage=2,
                metric_key="denoise_peak",
                value=peak,
                run_id=self.run_id,
            )
            self.ledger.put_stage_metric(
                recording_id=self.recording_id,
                stage=3,
                metric_key="normalization_gain",
                value=gain,
                run_id=self.run_id,
            )
            self._denoise_peak = peak
            self._gain = gain
            return peak, gain

    def compute_block(self, stage: int, block_index: int) -> np.ndarray:
        start, end = self._ranges[stage][block_index]
        if stage == 0:
            raw = read_wav_interval(self.spec.wav_path, start, end)
            return remove_dc(raw, self.spec.dc, self._dc_mean())
        if stage == 1:
            return declick_interval(
                lambda fetch_start, fetch_end: self.read_stage_interval(
                    0, fetch_start, fetch_end
                ),
                self.sample_count,
                start,
                end,
                self.spec.declick,
            )
        if stage == 2:
            return self._denoise_model.denoise_interval(
                lambda fetch_start, fetch_end: self.read_stage_interval(
                    1, fetch_start, fetch_end
                ),
                self._noise(),
                start,
                end,
            )
        if stage == 3:
            _, gain = self.ensure_denoise_metrics()
            values = self.read_stage_interval(2, start, end)
            return apply_normalization(values, gain)
        raise RepairError("E_STAGE_RANGE", f"invalid stage number {stage}")

    def compute_and_commit(
        self,
        stage: int,
        block_index: int,
        logical_cost_ns: int,
    ) -> bool:
        values = self.compute_block(stage, block_index)
        start, end = self._ranges[stage][block_index]
        inserted = self.ledger.put_block(
            recording_id=self.recording_id,
            stage=stage,
            block_index=block_index,
            start_sample=start,
            end_sample=end,
            values=values,
            logical_cost_ns=logical_cost_ns,
            run_id=self.run_id,
        )
        self.mark_completed(stage, block_index)
        return inserted

    def stage_records(self, stage: int):
        for block_index, _ in enumerate(self._ranges[stage]):
            yield self.ledger.block_record(self.recording_id, stage, block_index)

    def iter_stage_audio(self, stage: int):
        for block_index, _ in enumerate(self._ranges[stage]):
            yield self.ledger.fetch_block(self.recording_id, stage, block_index)

    def spectral_overlap_max_abs_error(self) -> float:
        cached = self.ledger.get_stage_metric(
            self.recording_id, 2, "spectral_overlap_max_abs_error"
        )
        if cached is not None:
            return float(cached)
        if not self.stage_complete(2):
            return 0.0
        frame_size = self.spec.denoise.frame_size
        noise = self._noise()
        maximum = 0.0
        boundaries = [end for _, end in self._ranges[2][:-1]]
        for boundary in boundaries:
            probe_start = max(0, boundary - frame_size)
            probe_end = min(self.sample_count, boundary + frame_size)
            actual = self.read_stage_interval(2, probe_start, probe_end)
            expected = self._denoise_model.denoise_interval(
                lambda fetch_start, fetch_end: self.read_stage_interval(
                    1, fetch_start, fetch_end
                ),
                noise,
                probe_start,
                probe_end,
            )
            maximum = max(maximum, float(np.max(np.abs(actual - expected))))
        self.ledger.put_stage_metric(
            recording_id=self.recording_id,
            stage=2,
            metric_key="spectral_overlap_max_abs_error",
            value=maximum,
            run_id=self.run_id,
        )
        return maximum

    def normalization_gain(self) -> float:
        return self.ensure_denoise_metrics()[1]
