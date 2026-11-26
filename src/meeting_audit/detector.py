from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import numpy as np

from .audio import FrameFeatures
from .config import AuditConfig, DEFAULT_CONFIG


@dataclass
class TurnPoint:
    ordinal: int
    event_id: str
    kind: str
    frame_index: int
    decision_frame_index: int
    at_ms: int
    preceding_silence_ms: int | None
    acoustic_from: str | None
    acoustic_to: str | None
    energy_ratio_q: int | None
    detection_basis: dict[str, int | str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def zcr_band(zcr_count: int, config: AuditConfig = DEFAULT_CONFIG) -> str:
    if zcr_count <= config.zcr_low_max:
        return "low"
    if zcr_count <= config.zcr_mid_max:
        return "mid"
    if zcr_count <= config.zcr_mid_max + 6:
        return "high"
    return "transition"


def _median_int(values: list[int]) -> int:
    if not values:
        return 0
    return int(round(float(np.median(np.asarray(values, dtype=np.int64)))))


class TurnDetectionStream:
    """Causal detector. Feeding frame chunks with one persistent instance is
    equivalent to feeding the complete feature sequence."""

    def __init__(self, config: AuditConfig = DEFAULT_CONFIG):
        self.config = config
        self.state = "silence"
        self.current_vad: dict[str, Any] | None = None
        self.vad_segments: list[dict[str, Any]] = []
        self.raw_events: list[dict[str, Any]] = []
        self.previous_end_ms: int | None = None

        self.reference_band: str | None = None
        self.stable_seen = 0
        self.candidate_band: str | None = None
        self.candidate_start_frame: int | None = None
        self.candidate_count = 0
        self.recent_rms: list[int] = []
        self.last_shift_at_ms: int | None = None

    def _reset_acoustic_reference(self) -> None:
        self.reference_band = None
        self.stable_seen = 0
        self.candidate_band = None
        self.candidate_start_frame = None
        self.candidate_count = 0

    def _close_vad(self, feature: FrameFeatures) -> None:
        assert self.current_vad is not None
        end_ms = feature.start_sample // self.config.samples_per_ms
        segment = dict(self.current_vad)
        segment["end_frame"] = feature.frame_index
        segment["end_ms"] = end_ms
        segment["end_sample"] = feature.start_sample
        self.vad_segments.append(segment)
        self.previous_end_ms = end_ms
        self.current_vad = None
        self.state = "silence"
        self._reset_acoustic_reference()

    def _open_vad(self, feature: FrameFeatures) -> None:
        silence_ms = None
        if self.previous_end_ms is not None:
            silence_ms = max(0, feature.at_ms - self.previous_end_ms)
        self.current_vad = {
            "vad_id": f"V{len(self.vad_segments) + 1:04d}",
            "start_frame": feature.frame_index,
            "start_ms": feature.at_ms,
            "start_sample": feature.start_sample,
        }
        self.state = "speech"
        self.raw_events.append(
            {
                "kind": "speech_start",
                "frame_index": feature.frame_index,
                "decision_frame_index": feature.frame_index,
                "at_ms": feature.at_ms,
                "preceding_silence_ms": silence_ms,
                "acoustic_from": "silence",
                "acoustic_to": "speech",
                "energy_ratio_q": None,
                "detection_basis": {
                    "rms_q": feature.rms_q,
                    "threshold_q": self.config.vad_start_rms_q,
                    "zcr_count": feature.zcr_count,
                },
            }
        )
        self._reset_acoustic_reference()

    def _observe_acoustic_shift(self, feature: FrameFeatures) -> None:
        band = zcr_band(feature.zcr_count, self.config)
        self.recent_rms.append(feature.rms_q)
        if len(self.recent_rms) > 12:
            self.recent_rms = self.recent_rms[-12:]

        if band == "transition":
            self.candidate_band = None
            self.candidate_start_frame = None
            self.candidate_count = 0
            return

        if self.reference_band is None:
            if self.candidate_band == band:
                self.candidate_count += 1
            else:
                self.candidate_band = band
                self.candidate_start_frame = feature.frame_index
                self.candidate_count = 1
            if self.candidate_count >= 2:
                self.reference_band = band
                self.stable_seen = self.candidate_count
                self.candidate_band = None
                self.candidate_start_frame = None
                self.candidate_count = 0
            return

        if band == self.reference_band:
            self.candidate_band = None
            self.candidate_start_frame = None
            self.candidate_count = 0
            return

        if self.candidate_band == band:
            self.candidate_count += 1
        else:
            self.candidate_band = band
            self.candidate_start_frame = feature.frame_index
            self.candidate_count = 1

        if self.candidate_count < self.config.shift_confirm_frames:
            return

        candidate_start = self.candidate_start_frame or feature.frame_index
        pre_values = self.recent_rms[: -self.candidate_count]
        if len(pre_values) < 4:
            pre_values = self.recent_rms[: max(1, len(self.recent_rms) - self.candidate_count)]
        post_values = self.recent_rms[-self.candidate_count :]
        pre_median = _median_int(pre_values[-8:])
        post_median = _median_int(post_values)
        ratio_q = int(round(post_median * 100.0 / max(1, pre_median)))
        candidate_at_ms = self.config.frame_center_ms(candidate_start)
        cooldown_ok = (
            self.last_shift_at_ms is None
            or candidate_at_ms - self.last_shift_at_ms >= self.config.shift_cooldown_ms
        )
        if ratio_q >= self.config.shift_energy_ratio_q and cooldown_ok:
            from_band = self.reference_band
            self.raw_events.append(
                {
                    "kind": "acoustic_shift",
                    "frame_index": candidate_start,
                    "decision_frame_index": feature.frame_index,
                    "at_ms": self.config.frame_center_ms(candidate_start),
                    "preceding_silence_ms": 0,
                    "acoustic_from": from_band,
                    "acoustic_to": band,
                    "energy_ratio_q": ratio_q,
                    "detection_basis": {
                        "zcr_count": feature.zcr_count,
                        "confirm_frames": self.candidate_count,
                        "pre_rms_median_q": pre_median,
                        "post_rms_median_q": post_median,
                    },
                }
            )
            self.reference_band = band
            self.last_shift_at_ms = candidate_at_ms
        self.candidate_band = None
        self.candidate_start_frame = None
        self.candidate_count = 0

    def push(self, feature: FrameFeatures) -> list[dict[str, Any]]:
        before = len(self.raw_events)
        if self.state == "silence":
            if feature.rms_q >= self.config.vad_start_rms_q:
                self._open_vad(feature)
        else:
            if feature.rms_q < self.config.vad_end_rms_q:
                self._close_vad(feature)
            else:
                self._observe_acoustic_shift(feature)
        return [dict(event) for event in self.raw_events[before:]]

    def flush(self) -> None:
        # Do not fabricate an end boundary for an unfinished real-time stream.
        # Batch PCM is complete and callers use finalize(close_tail=True).
        return None

    def finalize_batch(self, features: list[FrameFeatures]) -> dict[str, Any]:
        for feature in features:
            self.push(feature)
        if self.current_vad is not None and features:
            last = features[-1]
            segment = dict(self.current_vad)
            segment["end_frame"] = last.frame_index + 1
            segment["end_ms"] = last.end_sample // self.config.samples_per_ms
            segment["end_sample"] = last.end_sample
            self.vad_segments.append(segment)
            self.current_vad = None
        return build_detection_result(self.raw_events, self.vad_segments, self.config)


def build_detection_result(
    raw_events: list[dict[str, Any]],
    vad_segments: list[dict[str, Any]],
    config: AuditConfig = DEFAULT_CONFIG,
) -> dict[str, Any]:
    ordered = sorted(
        raw_events,
        key=lambda item: (
            int(item["at_ms"]),
            int(item["frame_index"]),
            0 if item["kind"] == "acoustic_shift" else 1,
        ),
    )
    turn_points: list[dict[str, Any]] = []
    for ordinal, event in enumerate(ordered, start=1):
        point = TurnPoint(
            ordinal=ordinal,
            event_id=f"T{ordinal:04d}",
            kind=str(event["kind"]),
            frame_index=int(event["frame_index"]),
            decision_frame_index=int(event["decision_frame_index"]),
            at_ms=int(event["at_ms"]),
            preceding_silence_ms=event.get("preceding_silence_ms"),
            acoustic_from=event.get("acoustic_from"),
            acoustic_to=event.get("acoustic_to"),
            energy_ratio_q=event.get("energy_ratio_q"),
            detection_basis=dict(event["detection_basis"]),
        )
        turn_points.append(point.to_dict())

    return {
        "schema": "meeting-audit/detection/v1",
        "turn_points": turn_points,
        "vad_segments": vad_segments,
        "frame_grid": {
            "sample_rate": config.sample_rate,
            "frame_ms": config.frame_ms,
            "hop_ms": config.hop_ms,
            "frame_samples": config.frame_samples,
            "hop_samples": config.hop_samples,
        },
    }


def detect_turns(
    features: list[FrameFeatures], config: AuditConfig = DEFAULT_CONFIG
) -> dict[str, Any]:
    stream = TurnDetectionStream(config)
    return stream.finalize_batch(features)
