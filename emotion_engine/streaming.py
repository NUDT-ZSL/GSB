"""Chunk-arrival engine with exact prefix checkpoints and real latency observation."""

from __future__ import annotations

import copy
import time
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from .aggregation import (
    assert_stream_batch_match,
    build_view,
    validate_view_structure,
)
from .batch import validate_manifest_dialog
from .config import EngineConfig
from .errors import (
    E_CHUNK_GAP,
    E_CHUNK_ORDER,
    E_CHUNK_RANGE,
    E_SPEAKER_LABEL_INVALID,
    E_WINDOW_RECOUNT,
    EngineError,
)
from .features import (
    PCM_DTYPE,
    analyze_frame,
    complete_frame_count,
    validate_speakers,
)
from .segments import recount_segment
from .snapshot import SnapshotStore, commit_view, verify_snapshot_store
from .windows import recount_window


class StreamingEngine:
    def __init__(
        self,
        manifest_entry: Mapping[str, Any],
        config: EngineConfig,
        clock_ns: Callable[[], int] = time.perf_counter_ns,
    ) -> None:
        validate_manifest_dialog(manifest_entry, config)
        self.entry = dict(manifest_entry)
        self.config = config
        self.clock_ns = clock_ns
        self.sample_rate = int(manifest_entry["sample_rate"])
        self.duration_ms = int(manifest_entry["duration_ms"])
        self.chunk_ms = int(manifest_entry["chunk_ms"])
        self.total_samples = round(self.duration_ms * self.sample_rate / 1000)
        self.chunk_samples = round(self.chunk_ms * self.sample_rate / 1000)
        self.frame_samples = config.frame_samples(self.sample_rate)
        self.hop_samples = config.hop_samples(self.sample_rate)
        self.total_frames = complete_frame_count(
            self.total_samples,
            self.frame_samples,
            self.hop_samples,
        )
        self.allowed_speakers = [str(item) for item in manifest_entry["speakers"]]
        self.dialog_id = str(manifest_entry["dialog_id"])
        self.snapshots = SnapshotStore()
        self.latency_observations: list[dict[str, Any]] = []
        self.deviations: list[dict[str, Any]] = []
        self._parts: list[np.ndarray] = []
        self._pcm = np.empty(0, dtype=PCM_DTYPE)
        self._labels: dict[int, str] = {}
        self._features: list[dict[str, Any]] = []
        self._next_frame_index = 0
        self._accepted_samples = 0
        self._next_sequence = 0
        self._snapshot_version = 0
        self._last_committed_frame_count = -1
        self._view: dict[str, Any] | None = None
        self._prefix_summaries: list[dict[str, Any]] = []

    @property
    def accepted_samples(self) -> int:
        return self._accepted_samples

    @property
    def frame_count(self) -> int:
        return len(self._features)

    @property
    def next_sequence(self) -> int:
        return self._next_sequence

    def _check_order(self, sequence: int, start_ms: int) -> None:
        if sequence != self._next_sequence:
            raise EngineError(
                E_CHUNK_GAP,
                "chunk sequence is missing, duplicated, or out of order",
                expected_sequence=self._next_sequence,
                actual_sequence=sequence,
            )
        if start_ms < 0 or start_ms * self.sample_rate != self._accepted_samples * 1000:
            raise EngineError(
                E_CHUNK_ORDER,
                "chunk timestamp is inconsistent with the accepted stream position",
                expected_start_ms=self._accepted_samples * 1000 / self.sample_rate,
                actual_start_ms=start_ms,
                accepted_samples=self._accepted_samples,
            )

    def submit_chunk(
        self,
        sequence: int,
        start_ms: int,
        pcm: np.ndarray,
        speaker_ids: Sequence[str],
        checkpoint: bool = True,
    ) -> dict[str, Any]:
        start_clock = self.clock_ns()
        sequence = int(sequence)
        start_ms = int(start_ms)
        self._check_order(sequence, start_ms)

        chunk = np.asarray(pcm, dtype=PCM_DTYPE).reshape(-1)
        if chunk.size == 0:
            raise EngineError(E_CHUNK_RANGE, "chunk must contain at least one sample", sequence=sequence)
        start_sample = self._accepted_samples
        end_sample = start_sample + int(chunk.size)
        if end_sample > self.total_samples:
            raise EngineError(
                E_CHUNK_RANGE,
                "chunk extends beyond the dialog duration",
                sequence=sequence,
                start_sample=start_sample,
                end_sample=end_sample,
                total_samples=self.total_samples,
            )
        if start_sample % self.hop_samples != 0 or int(chunk.size) % self.hop_samples != 0:
            raise EngineError(
                E_CHUNK_RANGE,
                "chunk boundaries must align to the configured hop grid",
                sequence=sequence,
                start_sample=start_sample,
                sample_count=int(chunk.size),
                hop_samples=self.hop_samples,
            )
        if int(chunk.size) > self.chunk_samples:
            raise EngineError(
                E_CHUNK_RANGE,
                "chunk exceeds the declared chunk_ms",
                sequence=sequence,
                sample_count=int(chunk.size),
                chunk_samples=self.chunk_samples,
            )
        frame_starts = start_sample // self.hop_samples
        candidate_frame_count = int(chunk.size) // self.hop_samples
        required_frame_count = max(
            0,
            min(frame_starts + candidate_frame_count, self.total_frames) - frame_starts,
        )
        expected_labels = required_frame_count
        if len(speaker_ids) != expected_labels:
            raise EngineError(
                E_SPEAKER_LABEL_INVALID,
                "chunk speaker label count does not match the chunk frame starts",
                frame_index=frame_starts,
                expected_labels=expected_labels,
                actual_labels=len(speaker_ids),
            )
        validate_speakers(
            speaker_ids,
            self.allowed_speakers,
            len(speaker_ids),
        )

        chunk_frame_start = frame_starts
        chunk_frame_end = frame_starts + expected_labels - 1
        for offset, speaker in enumerate(speaker_ids):
            self._labels[frame_starts + offset] = str(speaker)

        self._parts.append(chunk.copy())
        self._pcm = np.concatenate(self._parts) if len(self._parts) > 1 else self._parts[0]
        self._accepted_samples = end_sample
        self._next_sequence += 1
        self._process_complete_frames()
        self._view = None

        result: dict[str, Any] = {
            "sequence": sequence,
            "start_ms": start_ms,
            "end_ms": end_sample * 1000 // self.sample_rate,
            "chunk_frame_start": chunk_frame_start,
            "chunk_frame_end": chunk_frame_end,
            "business_code": "OK",
        }
        if checkpoint:
            view = self._checkpoint_internal()
            result["checkpoint_version"] = view.get("snapshot_version")
            result["frame_count"] = view["frame_count"]
            latency = self._observe_latency(
                start_clock=start_clock,
                sequence=sequence,
                start_ms=start_ms,
                end_ms=end_sample * 1000 // self.sample_rate,
                chunk_frame_start=chunk_frame_start,
                chunk_frame_end=chunk_frame_end,
                label_count=expected_labels,
            )
            self.latency_observations.append(latency)
            result["latency"] = latency
        return result

    def _process_complete_frames(self) -> None:
        maximum = complete_frame_count(
            int(self._pcm.shape[0]),
            self.frame_samples,
            self.hop_samples,
        )
        while self._next_frame_index < maximum:
            if self._next_frame_index not in self._labels:
                raise EngineError(
                    E_SPEAKER_LABEL_INVALID,
                    "a complete frame arrived without a speaker label",
                    frame_index=self._next_frame_index,
                )
            self._features.append(
                analyze_frame(
                    self._pcm,
                    self._next_frame_index,
                    self.sample_rate,
                    self.config,
                )
            )
            self._next_frame_index += 1

    def _speaker_sequence(self) -> list[str]:
        return [self._labels[index] for index in range(len(self._features))]

    def _checkpoint_internal(self) -> dict[str, Any]:
        if self._view is not None and self.frame_count == self._last_committed_frame_count:
            return self._view
        speakers = self._speaker_sequence()
        stream_view = build_view(
            self._features,
            speakers,
            self.config,
            identity={"dialog_id": self.dialog_id, "execution_path": "streaming"},
            allowed_speakers=self.allowed_speakers,
        )
        batch_view = build_view(
            copy.deepcopy(self._features),
            list(speakers),
            self.config,
            identity={"dialog_id": self.dialog_id, "execution_path": "offline_prefix"},
            allowed_speakers=self.allowed_speakers,
        )
        try:
            assert_stream_batch_match(stream_view, batch_view, self.config)
        except EngineError as exc:
            self.deviations.append(exc.details["first_deviation"])
            raise

        if self.frame_count != self._last_committed_frame_count:
            self._snapshot_version += 1
            replacements = commit_view(self.snapshots, stream_view, self._snapshot_version)
            self._last_committed_frame_count = self.frame_count
        else:
            replacements = []

        self_check = self._self_check(stream_view, batch_view)
        stream_view["snapshot_version"] = self._snapshot_version
        stream_view["snapshots"] = self.snapshots.metadata()
        stream_view["snapshot_replacements"] = replacements
        stream_view["self_check"] = self_check
        stream_view["stream_batch"] = {
            "matched": True,
            "declared_precision": self.config.declared_precision,
            "first_deviation": None,
            "offline_prefix_summary": batch_view["summary"],
        }
        stream_view["business_code"] = "OK"
        stream_view["degraded"] = False
        self._view = stream_view
        self._prefix_summaries.append(
            {
                "version": self._snapshot_version,
                "frame_count": stream_view["frame_count"],
                "summary": copy.deepcopy(stream_view["summary"]),
            }
        )
        return stream_view

    def checkpoint(self) -> dict[str, Any]:
        return self._checkpoint_internal()

    def _self_check(
        self,
        stream_view: Mapping[str, Any],
        batch_view: Mapping[str, Any],
    ) -> dict[str, Any]:
        stream_frames = list(stream_view["frames"])
        batch_frames = list(batch_view["frames"])
        stream_indices = [int(frame["frame_index"]) for frame in stream_frames]
        batch_indices = [int(frame["frame_index"]) for frame in batch_frames]
        stream_boundaries = [
            [int(segment["start_frame"]), int(segment["end_frame"])]
            for segment in stream_view["segments"]
        ]
        batch_boundaries = [
            [int(segment["start_frame"]), int(segment["end_frame"])]
            for segment in batch_view["segments"]
        ]
        stream_switches = [
            [int(frame["frame_index"]), str(frame["tier"])]
            for index, frame in enumerate(stream_frames)
            if index == 0 or str(frame["tier"]) != str(stream_frames[index - 1]["tier"])
        ]
        batch_switches = [
            [int(frame["frame_index"]), str(frame["tier"])]
            for index, frame in enumerate(batch_frames)
            if index == 0 or str(frame["tier"]) != str(batch_frames[index - 1]["tier"])
        ]
        checks = [
            {
                "name": "stream_batch_frame_set",
                "ok": stream_indices == batch_indices,
                "stream_count": len(stream_indices),
                "batch_count": len(batch_indices),
            },
            {
                "name": "stream_batch_segment_boundaries",
                "ok": stream_boundaries == batch_boundaries,
                "stream": stream_boundaries,
                "batch": batch_boundaries,
            },
            {
                "name": "stream_batch_tier_switch_sequence",
                "ok": stream_switches == batch_switches,
                "stream": stream_switches,
                "batch": batch_switches,
            },
            {
                "name": "view_structure",
                "ok": all(
                    bool(check["ok"])
                    for check in validate_view_structure(stream_view, self.config)
                ),
            },
        ]
        checks.extend(verify_snapshot_store(self.snapshots))
        return {
            "ok": all(bool(check["ok"]) for check in checks),
            "checks": checks,
        }

    def _observe_latency(
        self,
        start_clock: int,
        sequence: int,
        start_ms: int,
        end_ms: int,
        chunk_frame_start: int,
        chunk_frame_end: int,
        label_count: int,
    ) -> dict[str, Any]:
        elapsed_ns = max(0, int(self.clock_ns()) - int(start_clock))
        observed_ms = round(elapsed_ns / 1_000_000.0, 6)
        estimate_config = self.config.raw["latency_estimate"]
        frame_ops = label_count
        window_ops = label_count * self.config.window_length
        speaker_ops = label_count * len(self.allowed_speakers)
        estimated_ms = round(
            frame_ops * float(estimate_config["frame_factor_ms"])
            + window_ops * float(estimate_config["window_factor_ms"])
            + speaker_ops * float(estimate_config["speaker_factor_ms"]),
            6,
        )
        return {
            "sequence": sequence,
            "chunk_start_ms": start_ms,
            "chunk_end_ms": end_ms,
            "chunk_frame_start": chunk_frame_start,
            "chunk_frame_end": chunk_frame_end,
            "frame_count": frame_ops,
            "window_length_frames": self.config.window_length,
            "speaker_count": len(self.allowed_speakers),
            "observed_processing_ms": observed_ms,
            "observed_processing_ns": elapsed_ns,
            "processing_clock": "time.perf_counter_ns",
            "estimate": {
                "frame_ops": frame_ops,
                "window_ops": window_ops,
                "speaker_ops": speaker_ops,
                "estimated_ms": estimated_ms,
                "estimate_only": True,
                "used_for_timeout": False,
                "timeout_decision": "not_applicable",
            },
        }

    def recount_frame(self, frame_index: int) -> dict[str, Any]:
        if self._view is None:
            raise EngineError(
                E_WINDOW_RECOUNT,
                "no checkpoint view is available",
                frame_index=frame_index,
            )
        return recount_window(frame_index, self._view["frames"], self.config)

    def recount_segment(self, segment_index: int) -> dict[str, Any]:
        if self._view is None:
            raise EngineError(
                E_WINDOW_RECOUNT,
                "no checkpoint view is available",
                segment_index=segment_index,
            )
        return recount_segment(
            self._view["segments"][segment_index],
            self._view["frames"],
            self.config,
        )

    def finish(self) -> dict[str, Any]:
        expected_frames = complete_frame_count(
            self.total_samples,
            self.frame_samples,
            self.hop_samples,
        )
        if self._accepted_samples != self.total_samples:
            raise EngineError(
                E_CHUNK_RANGE,
                "stream ended before the declared dialog duration",
                accepted_samples=self._accepted_samples,
                total_samples=self.total_samples,
            )
        if len(self._features) != expected_frames:
            raise EngineError(
                E_CHUNK_RANGE,
                "stream did not materialize all complete frames",
                frame_count=len(self._features),
                expected_frames=expected_frames,
            )
        return self._checkpoint_internal()
