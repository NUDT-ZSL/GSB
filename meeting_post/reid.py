"""Deterministic registration-based speaker re-identification."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from .config import MeetingConfig
from .errors import MeetingError, unknown_speaker
from .features import FeatureResult
from .timebase import SegmentSpan, verify_span_timebase


@dataclass
class IdentityAssignment:
    segment_id: str
    speaker_id: int
    score: float
    confidence_low: float
    confidence_high: float
    accepted: bool
    candidate_scores: dict[int, float]
    truth_speaker_ids: list[int]
    matches_truth: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "speaker_id": self.speaker_id,
            "score": self.score,
            "confidence_interval": [self.confidence_low, self.confidence_high],
            "accepted": self.accepted,
            "candidate_scores": {
                str(key): value for key, value in sorted(self.candidate_scores.items())
            },
            "truth_speaker_ids": self.truth_speaker_ids,
            "matches_truth": self.matches_truth,
        }


def _truth_speakers_by_frame(
    session_truth: list[dict[str, Any]],
    declared_speakers: set[int],
) -> list[list[int]]:
    by_frame: dict[int, list[int]] = {}
    for row in session_truth:
        speaker_id = int(row["speaker_id"])
        if speaker_id not in declared_speakers:
            raise unknown_speaker(speaker_id, sorted(declared_speakers))
        if row["is_speech"]:
            by_frame.setdefault(int(row["frame_index"]), []).append(speaker_id)
    return [sorted(by_frame.get(index, [])) for index in range(max(by_frame, default=-1) + 1)]


def assign_identities(
    config: MeetingConfig,
    features: FeatureResult,
    profiles: dict[int, dict[str, Any]],
    segments: list[SegmentSpan],
    *,
    session_truth: list[dict[str, Any]],
    session_speaker_ids: set[int],
) -> list[IdentityAssignment]:
    for speaker_id in profiles:
        if speaker_id not in session_speaker_ids:
            raise unknown_speaker(speaker_id, sorted(session_speaker_ids))
    frame_truth = _truth_speakers_by_frame(session_truth, session_speaker_ids)
    profile_ids = sorted(profiles)
    means = np.vstack([profiles[speaker_id]["mean"] for speaker_id in profile_ids])
    assignments: list[IdentityAssignment] = []
    for segment in segments:
        verify_span_timebase(
            segment,
            sample_rate=config.sample_rate,
            frame_samples=config.frame_samples,
            hop_samples=config.hop_samples,
        )
        frame_sims = np.vstack(
            [means @ features.embeddings[frame_index] for frame_index in segment.frame_indices]
        )
        mean_scores = np.mean(frame_sims, axis=0)
        order = sorted(
            range(len(profile_ids)),
            key=lambda index: (-float(mean_scores[index]), profile_ids[index]),
        )
        winner_index = order[0]
        top_id = profile_ids[winner_index]
        top_score = float(mean_scores[winner_index])
        if top_id not in session_speaker_ids:
            raise unknown_speaker(top_id, sorted(session_speaker_ids))

        truth_ids: list[int] = []
        frame_truth_sets = [
            frame_truth[index] if index < len(frame_truth) else []
            for index in segment.frame_indices
        ]
        for active in frame_truth_sets:
            truth_ids.extend(active)
        unique_truth = sorted(set(truth_ids))
        matches_truth = bool(unique_truth) and all(
            top_id in active for active in frame_truth_sets
        )

        winner_series = frame_sims[:, winner_index]
        mean = float(np.mean(winner_series))
        deviation = float(np.std(winner_series, ddof=0))
        radius = 1.96 * deviation / math.sqrt(max(1, winner_series.size))
        low = max(0.0, min(1.0, mean - radius))
        high = max(0.0, min(1.0, mean + radius))
        assignments.append(
            IdentityAssignment(
                segment_id=segment.segment_id,
                speaker_id=top_id,
                score=top_score,
                confidence_low=low,
                confidence_high=high,
                accepted=top_score >= config.reid_threshold,
                candidate_scores={
                    speaker_id: float(mean_scores[index])
                    for index, speaker_id in enumerate(profile_ids)
                },
                truth_speaker_ids=unique_truth,
                matches_truth=matches_truth,
            )
        )
    assignments.sort(key=lambda item: (item.speaker_id, item.segment_id))
    return assignments


def validate_speaker_override(speaker_id: int, declared: set[int]) -> None:
    if speaker_id not in declared:
        raise unknown_speaker(speaker_id, sorted(declared))
