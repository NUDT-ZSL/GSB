"""Speaker registration embeddings, frame similarity, and assignment."""

from __future__ import annotations

from typing import Any

import numpy as np

from .canonical import content_hash, quantize
from .config import PipelineConfig
from .errors import fail
from .features import FEATURE_NAMES, raw_feature_matrix
from .samples import RegistrationSample
from .synthesis import synthesize_registration

ASSIGNMENT_ALGORITHM = "registration-profile-distance-v2"


def registration_rows_for(
    registrations: list[RegistrationSample], speaker_ids: list[str]
) -> list[dict[str, Any]]:
    allowed = set(speaker_ids)
    rows = [
        registration.to_dict()
        for registration in registrations
        if registration.speaker_id in allowed
    ]
    return sorted(
        rows,
        key=lambda row: (
            row["speaker_id"],
            row["register_seed"],
            row["register_ms"],
        ),
    )


def registration_version(
    registrations: list[RegistrationSample], speaker_ids: list[str]
) -> str:
    return content_hash(
        {
            "algorithm": ASSIGNMENT_ALGORITHM,
            "rows": registration_rows_for(registrations, speaker_ids),
        }
    )


def assign_speakers(
    dialog_id: str,
    feature_data: dict[str, Any],
    speaker_ids: list[str],
    registrations: list[RegistrationSample],
    config: PipelineConfig,
) -> dict[str, Any]:
    speaker_ids = sorted(speaker_ids)
    if len(set(speaker_ids)) != len(speaker_ids):
        fail(
            "E_DIALOG_INVALID",
            "dialog speaker_ids must be unique",
            dialog_id=dialog_id,
        )
    allowed = set(speaker_ids)
    speaker_registrations = [
        row for row in registrations if row.speaker_id in allowed
    ]
    for speaker_id in speaker_ids:
        if not any(row.speaker_id == speaker_id for row in speaker_registrations):
            fail(
                "E_REGISTRATION_MISSING",
                "speaker has no registration samples",
                dialog_id=dialog_id,
                speaker_id=speaker_id,
            )

    profiles = _speaker_profiles(speaker_registrations, config)
    matrix = np.asarray(feature_data["features"], dtype=np.float64)
    frame_count = int(feature_data["frame_count"])
    if matrix.shape != (frame_count, len(FEATURE_NAMES)):
        fail(
            "E_FEATURE_INVALID",
            "feature matrix shape does not match frame_count",
            dialog_id=dialog_id,
            shape=list(matrix.shape),
            frame_count=frame_count,
        )
    embeddings = _frame_embeddings(matrix)
    speech_mask = matrix[:, 0] >= config.speech_energy_threshold
    candidate_frames = int(np.sum(speech_mask))
    candidate_comparisons = candidate_frames * len(speaker_ids)
    if (
        candidate_comparisons > config.limits.max_candidate_comparisons
        and candidate_comparisons > 0
    ):
        fail(
            "E_RESOURCE_LIMIT",
            "speaker candidate comparisons exceed configured limit",
            dialog_id=dialog_id,
            candidate_comparisons=candidate_comparisons,
            limit=config.limits.max_candidate_comparisons,
        )

    labels: list[str | None] = [None] * frame_count
    best_scores = np.zeros(frame_count, dtype=np.float64)
    for frame_index in np.flatnonzero(speech_mask):
        embedding = embeddings[frame_index]
        best_speaker: str | None = None
        best_score = float("-inf")
        for speaker_id in speaker_ids:
            profile = profiles[speaker_id]
            z = (embedding - profile["mean"]) / profile["scale"]
            score = -float(np.mean(z * z))
            score = quantize(score)
            if score > best_score:
                best_score = score
                best_speaker = speaker_id
        labels[frame_index] = best_speaker
        best_scores[frame_index] = best_score

    speech_indices = np.flatnonzero(speech_mask)
    if len(speech_indices) == 0:
        for frame_index in range(frame_count):
            labels[frame_index] = speaker_ids[0]
            best_scores[frame_index] = 0.0
    else:
        for frame_index in np.flatnonzero(~speech_mask):
            nearest = min(
                speech_indices,
                key=lambda index: (abs(int(index) - frame_index), int(index)),
            )
            labels[frame_index] = labels[int(nearest)]
            best_scores[frame_index] = best_scores[int(nearest)]

    final_labels = [str(label) for label in labels]
    validate_assignment_labels(dialog_id, final_labels, speaker_ids)
    profile_payload = {
        speaker_id: {
            "mean": [quantize(value) for value in profiles[speaker_id]["mean"]],
            "scale": [quantize(value) for value in profiles[speaker_id]["scale"]],
            "sample_count": profiles[speaker_id]["sample_count"],
        }
        for speaker_id in speaker_ids
    }
    raw_segments = _continuous_segments(final_labels)
    return {
        "dialog_id": dialog_id,
        "algorithm": ASSIGNMENT_ALGORITHM,
        "speaker_ids": speaker_ids,
        "registration_version": registration_version(registrations, speaker_ids),
        "profiles": profile_payload,
        "frame_count": frame_count,
        "labels": final_labels,
        "raw_segments": raw_segments,
        "scores": [quantize(value) for value in best_scores.tolist()],
        "speech_frame_count": candidate_frames,
        "candidate_comparisons": candidate_comparisons,
        "labels_hash": content_hash(final_labels),
        "raw_segments_hash": content_hash(raw_segments),
    }


def validate_assignment_labels(
    dialog_id: str,
    labels: list[str],
    speaker_ids: list[str],
) -> None:
    allowed = set(speaker_ids)
    for frame_index, label in enumerate(labels):
        if label not in allowed:
            fail(
                "E_SPEAKER_UNKNOWN",
                "assigned speaker is not in the dialog speaker_ids",
                dialog_id=dialog_id,
                frame_index=frame_index,
                speaker_id=label,
                allowed_speaker_ids=sorted(allowed),
            )


def _speaker_profiles(
    registrations: list[RegistrationSample],
    config: PipelineConfig,
) -> dict[str, dict[str, Any]]:
    embeddings_by_speaker: dict[str, list[np.ndarray]] = {}
    for registration in registrations:
        pcm = synthesize_registration(
            speaker_id=registration.speaker_id,
            register_seed=registration.register_seed,
            register_ms=registration.register_ms,
            sample_rate=config.sample_rate,
        )
        matrix = raw_feature_matrix(pcm, config)
        if len(matrix) == 0:
            continue
        speech = matrix[:, 0] >= config.speech_energy_threshold
        if np.any(speech):
            matrix = matrix[speech]
        summary = np.mean(_frame_embeddings(matrix), axis=0)
        embeddings_by_speaker.setdefault(registration.speaker_id, []).append(summary)

    profiles: dict[str, dict[str, Any]] = {}
    for speaker_id, embeddings in embeddings_by_speaker.items():
        stacked = np.vstack(embeddings)
        mean = np.mean(stacked, axis=0)
        if len(stacked) > 1:
            spread = np.std(stacked, axis=0, ddof=0)
        else:
            spread = np.zeros(stacked.shape[1], dtype=np.float64)
        scale = np.maximum(spread, 0.05)
        profiles[speaker_id] = {
            "mean": mean,
            "scale": scale,
            "sample_count": len(embeddings),
        }
    return profiles


def _frame_embeddings(matrix: np.ndarray) -> np.ndarray:
    if len(matrix) == 0:
        return np.empty((0, 3), dtype=np.float64)
    return np.column_stack(
        [
            matrix[:, 1],
            matrix[:, 2] / 4000.0,
            matrix[:, 3] / 8000.0,
        ]
    )


def _continuous_segments(labels: list[str]) -> list[dict[str, Any]]:
    if not labels:
        return []
    segments: list[dict[str, Any]] = []
    start = 0
    current = labels[0]
    for index in range(1, len(labels)):
        if labels[index] == current:
            continue
        segments.append(
            {
                "start_frame": start,
                "end_frame": index,
                "speaker_id": current,
            }
        )
        start = index
        current = labels[index]
    segments.append(
        {
            "start_frame": start,
            "end_frame": len(labels),
            "speaker_id": current,
        }
    )
    return segments
