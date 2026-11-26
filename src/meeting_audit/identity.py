from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from .audio import FrameFeatures
from .config import AuditConfig, DEFAULT_CONFIG


def frame_pitch_feature(
    frame: np.ndarray, config: AuditConfig = DEFAULT_CONFIG
) -> dict[str, int | float | None]:
    samples = frame.astype(np.float64)
    centered = samples - float(np.mean(samples))
    if not np.any(centered):
        return {"pitch_hz": None, "pitch_lag": None, "pitch_confidence_q": 0}

    window = np.hanning(len(centered))
    weighted = centered * window
    autocorr = np.correlate(weighted, weighted, mode="full")[len(weighted) - 1 :]
    if autocorr[0] <= 0:
        return {"pitch_hz": None, "pitch_lag": None, "pitch_confidence_q": 0}

    min_lag = config.sample_rate // 320
    max_lag = min(config.sample_rate // 70, len(autocorr) - 1)
    search = autocorr[min_lag : max_lag + 1]
    lag = min_lag + int(np.argmax(search))
    confidence = float(autocorr[lag] / autocorr[0])
    if confidence < 0.18:
        return {
            "pitch_hz": None,
            "pitch_lag": lag,
            "pitch_confidence_q": int(round(confidence * 1000)),
        }
    return {
        "pitch_hz": config.sample_rate / lag,
        "pitch_lag": lag,
        "pitch_confidence_q": int(round(confidence * 1000)),
    }


def segment_speaker_feature(
    pcm: np.ndarray | None = None,
    frame_features: list[FrameFeatures] | None = None,
    start_frame: int | None = None,
    end_frame: int | None = None,
    config: AuditConfig = DEFAULT_CONFIG,
) -> dict[str, Any]:
    """Aggregate frame-level speaker features for one activity segment."""
    pitches: list[float] = []
    zcrs: list[int] = []
    rmss: list[int] = []
    confidences: list[int] = []

    if frame_features is not None:
        selected = frame_features
        if start_frame is not None and end_frame is not None:
            selected = [
                feature
                for feature in selected
                if start_frame <= feature.frame_index < end_frame
            ]
        for feature in selected:
            zcrs.append(feature.zcr_count)
            rmss.append(feature.rms_q)
            if pcm is not None:
                frame = pcm[feature.start_sample : feature.end_sample]
                pitch = frame_pitch_feature(frame, config)
                if pitch["pitch_hz"] is not None and int(pitch["pitch_confidence_q"]) >= 250:
                    pitches.append(float(pitch["pitch_hz"]))
                    confidences.append(int(pitch["pitch_confidence_q"]))

    pitch_hz = float(np.median(np.asarray(pitches))) if pitches else None
    return {
        "pitch_hz": round(pitch_hz, 3) if pitch_hz is not None else None,
        "log_pitch": round(math.log(max(1.0, pitch_hz or 180.0)), 6),
        "zcr_median": int(np.median(np.asarray(zcrs, dtype=np.int64))) if zcrs else 0,
        "rms_median_q": int(np.median(np.asarray(rmss, dtype=np.int64))) if rmss else 0,
        "pitch_confidence_q": int(np.median(np.asarray(confidences, dtype=np.int64)))
        if confidences
        else 0,
        "frame_count": len(zcrs),
        "pitch_frame_count": len(pitches),
    }


def feature_distance(left: dict[str, Any], right: dict[str, Any]) -> float:
    pitch_distance = abs(float(left["log_pitch"]) - float(right["log_pitch"]))
    zcr_distance = abs(int(left["zcr_median"]) - int(right["zcr_median"])) / 12.0
    return pitch_distance + 0.08 * zcr_distance


@dataclass
class IdentityAdjudicator:
    config: AuditConfig = DEFAULT_CONFIG

    def __post_init__(self) -> None:
        self.speakers: dict[str, dict[str, Any]] = {}
        self.truth_index: dict[str, str] = {}
        self.assignments: dict[str, dict[str, Any]] = {}
        self.lineage: dict[str, list[str]] = {}
        self._next_speaker = 1

    def _new_speaker(self, feature: dict[str, Any], truth_key: str | None = None) -> str:
        speaker_id = f"SPK-{self._next_speaker:04d}"
        self._next_speaker += 1
        self.speakers[speaker_id] = {
            "speaker_id": speaker_id,
            "centroid": dict(feature),
            "truth_keys": [truth_key] if truth_key else [],
            "assignment_count": 0,
        }
        if truth_key:
            self.truth_index[truth_key] = speaker_id
        return speaker_id

    def _nearest(self, feature: dict[str, Any]) -> tuple[str | None, float]:
        best_id: str | None = None
        best_distance = float("inf")
        for speaker_id, record in self.speakers.items():
            distance = feature_distance(feature, record["centroid"])
            if distance < best_distance:
                best_distance = distance
                best_id = speaker_id
        return best_id, best_distance

    def resolve_truth_speaker(self, truth_key: str, feature: dict[str, Any]) -> str:
        if truth_key in self.truth_index:
            speaker_id = self.truth_index[truth_key]
            if truth_key not in self.speakers[speaker_id]["truth_keys"]:
                self.speakers[speaker_id]["truth_keys"].append(truth_key)
            return speaker_id
        best_id, distance = self._nearest(feature)
        threshold = self.config.identity_distance_q / 100.0
        if best_id is not None and distance <= threshold:
            self.truth_index[truth_key] = best_id
            self.speakers[best_id]["truth_keys"].append(truth_key)
            return best_id
        return self._new_speaker(feature, truth_key)

    def assign(
        self,
        segment: dict[str, Any],
        truth_key: str | None = None,
        preserve_existing: bool = True,
    ) -> dict[str, Any]:
        segment_id = str(segment["segment_id"])
        feature = dict(segment["speaker_feature"])
        existing = self.assignments.get(segment_id) if preserve_existing else None

        distance: float | None = None
        if truth_key:
            speaker_id = self.resolve_truth_speaker(truth_key, feature)
            status = "truth_corrected"
            reason = "truth_override"
        elif existing is not None and existing.get("speaker_id"):
            speaker_id = str(existing["speaker_id"])
            status = str(existing.get("status", "stable"))
            reason = "stable_assignment_reused"
        else:
            nearest_id, nearest_distance = self._nearest(feature)
            threshold = self.config.identity_distance_q / 100.0
            if nearest_id is not None and nearest_distance <= threshold:
                speaker_id = nearest_id
                distance = nearest_distance
                status = "auto"
                reason = "feature_centroid_match"
            else:
                speaker_id = self._new_speaker(feature)
                distance = nearest_distance if nearest_id is not None else None
                status = "auto_new_speaker"
                reason = "distance_above_threshold"

        assignment = {
            "segment_id": segment_id,
            "speaker_id": speaker_id,
            "status": status,
            "reason": reason,
            "distance_q": None if distance is None else int(round(distance * 1000)),
            "truth_key": truth_key,
            "feature": feature,
            "lineage": segment.get("lineage", [segment_id]),
        }
        self.assignments[segment_id] = assignment
        record = self.speakers[speaker_id]
        record["assignment_count"] = int(record.get("assignment_count", 0)) + 1
        return assignment

    def correct_truth(
        self, segment_id: str, truth_key: str, feature: dict[str, Any]
    ) -> dict[str, Any]:
        return self.assign(
            {"segment_id": segment_id, "speaker_feature": feature},
            truth_key=truth_key,
            preserve_existing=False,
        )

    def inherit_lineage(
        self, child_segment: dict[str, Any], parent_ids: list[str]
    ) -> dict[str, Any] | None:
        labels = []
        for parent_id in parent_ids:
            parent = self.assignments.get(parent_id)
            if parent:
                labels.append(str(parent["speaker_id"]))
        if labels and len(set(labels)) == 1:
            segment_id = str(child_segment["segment_id"])
            assignment = {
                "segment_id": segment_id,
                "speaker_id": labels[0],
                "status": "stable_inherited",
                "reason": "uniform_parent_lineage",
                "distance_q": None,
                "truth_key": None,
                "feature": dict(child_segment["speaker_feature"]),
                "lineage": parent_ids + [segment_id],
            }
            self.assignments[segment_id] = assignment
            self.lineage[segment_id] = parent_ids
            return assignment
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "speakers": self.speakers,
            "truth_index": self.truth_index,
            "assignments": self.assignments,
            "lineage": self.lineage,
            "next_speaker": self._next_speaker,
        }


def build_speech_segments(
    detection: dict[str, Any],
    pcm: np.ndarray,
    features: list[FrameFeatures],
    config: AuditConfig = DEFAULT_CONFIG,
) -> list[dict[str, Any]]:
    """Split VAD ranges at acoustic shifts and attach speaker features."""
    shifts_by_vad: dict[int, list[int]] = {}
    for turn in detection["turn_points"]:
        if turn["kind"] == "acoustic_shift":
            for vad_index, vad in enumerate(detection["vad_segments"]):
                if int(vad["start_ms"]) <= int(turn["at_ms"]) < int(vad["end_ms"]):
                    shifts_by_vad.setdefault(vad_index, []).append(int(turn["at_ms"]))

    raw_ranges: list[tuple[int, int]] = []
    for vad_index, vad in enumerate(detection["vad_segments"]):
        start_ms = int(vad["start_ms"])
        end_ms = int(vad["end_ms"])
        cuts = sorted(set(shifts_by_vad.get(vad_index, [])))
        current = start_ms
        for cut in cuts:
            if current < cut:
                raw_ranges.append((current, cut))
            current = cut
        if current < end_ms:
            raw_ranges.append((current, end_ms))

    segments: list[dict[str, Any]] = []
    for index, (start_ms, end_ms) in enumerate(raw_ranges, start=1):
        start_sample = start_ms * config.samples_per_ms
        end_sample = end_ms * config.samples_per_ms
        start_frame = max(0, start_ms // config.hop_ms - 1)
        end_frame = max(start_frame, end_ms // config.hop_ms + 1)
        feature = segment_speaker_feature(
            pcm=pcm,
            frame_features=features,
            start_frame=start_frame,
            end_frame=end_frame,
            config=config,
        )
        segments.append(
            {
                "segment_id": f"SEG-{index:04d}",
                "segment_no": index,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "start_sample": start_sample,
                "end_sample": end_sample,
                "frame_range": [start_frame, end_frame],
                "speaker_feature": feature,
                "lineage": [f"SEG-{index:04d}"],
            }
        )
    return segments
