"""Deterministic template candidate detection, scoring and classification."""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Sequence

import numpy as np

from .catalog import ACTIVE_EVENT_TYPES, EVENT_TYPE_IDS, event_type_id
from .codes import BusinessCode
from .config import FEATURE_NAMES_FULL, FeatureConfig, validate_feature_config
from .errors import AedError
from .features import extract_features
from .paths import PathProfile
from .synthesis import (
    EVENT_NOMINAL_DURATION_S,
    make_event_waveform,
    make_speaker_calibration_waveform,
)
from .types import Candidate, DetectionResult, FeatureResult

_MATCH_TEMPERATURE = 0.8
_EVENT_TEMPLATE_VARIANTS = 8
_BACKGROUND_VARIANTS = 16


def _scaled_features(features: np.ndarray, names: Sequence[str]) -> np.ndarray:
    """Stable feature normalization for distance comparisons."""

    columns = {name: index for index, name in enumerate(FEATURE_NAMES_FULL)}
    missing = [name for name in names if name not in columns]
    if missing:
        raise KeyError(f"unknown feature names: {missing}")

    def column(name: str) -> np.ndarray:
        return features[:, columns[name]]

    builders = {
        "log_energy": lambda: (np.clip(column("log_energy"), -65.0, 5.0) + 45.0) / 25.0,
        "zcr": lambda: np.clip(column("zcr"), 0.0, 0.4) / 0.20,
        "spectral_centroid_hz": lambda: np.clip(
            column("spectral_centroid_hz"), 0.0, 7000.0
        )
        / 2500.0,
        "spectral_rolloff_hz": lambda: np.clip(
            column("spectral_rolloff_hz"), 0.0, 8000.0
        )
        / 3000.0,
        "spectral_bandwidth_hz": lambda: np.clip(
            column("spectral_bandwidth_hz"), 0.0, 3500.0
        )
        / 1200.0,
        "spectral_flatness": lambda: (
            np.log10(np.clip(column("spectral_flatness"), 1e-6, 1.0)) + 6.0
        )
        / 5.0,
        "spectral_flux": lambda: np.clip(column("spectral_flux"), 0.0, 2.0) / 0.7,
        "peak_abs": lambda: np.clip(column("peak_abs"), 0.0, 1.0) / 0.4,
        "low_band_ratio": lambda: np.clip(column("low_band_ratio"), 0.0, 1.0),
        "high_band_ratio": lambda: np.sqrt(
            np.clip(column("high_band_ratio"), 0.0, 1.0)
        ),
    }
    matrix = np.stack([builders[name]() for name in names], axis=1)
    return np.ascontiguousarray(matrix, dtype=np.float64)


def _calibration_samples(config: FeatureConfig) -> tuple[np.ndarray, list[str]]:
    full_config = config.with_mode("full")
    names = list(full_config.feature_names)
    event_samples: list[np.ndarray] = []
    background_samples: list[np.ndarray] = []

    for event_type in ACTIVE_EVENT_TYPES:
        class_frames: list[np.ndarray] = []
        for variant_index in range(_EVENT_TEMPLATE_VARIANTS):
            waveform = make_event_waveform(
                event_type,
                EVENT_NOMINAL_DURATION_S[event_type],
                sample_rate=config.sample_rate,
                seed=0x5EED + variant_index,
                variant=f"calibration-{variant_index}",
            )
            result = extract_features(
                waveform,
                full_config,
                dialog_id=f"template_{event_type}",
            )
            class_frames.append(result.features)
        class_matrix = np.concatenate(class_frames, axis=0)
        active = class_matrix[:, names.index("log_energy")] > -35.0
        class_matrix = class_matrix[active]
        threshold = np.quantile(class_matrix[:, names.index("log_energy")], 0.25)
        class_matrix = class_matrix[
            class_matrix[:, names.index("log_energy")] >= threshold
        ]
        event_samples.append(class_matrix)

    for variant_index in range(_BACKGROUND_VARIANTS):
        waveform = make_speaker_calibration_waveform(
            variant_index,
            duration_s=1.0,
            sample_rate=config.sample_rate,
        )
        result = extract_features(
            waveform,
            full_config,
            dialog_id=f"background_{variant_index}",
        )
        background_samples.append(result.features)
    silence = extract_features(
        np.zeros(config.sample_rate, dtype=np.float64),
        full_config,
        dialog_id="background_silence",
    )
    background_samples.append(silence.features)
    background_matrix = np.concatenate(background_samples, axis=0)
    background_matrix = background_matrix[
        background_matrix[:, names.index("log_energy")] > -45.0
    ]
    return background_matrix, event_samples


@lru_cache(maxsize=16)
def _prototype_centers_cached(
    sample_rate: int,
    frame_ms: float,
    hop_ms: float,
    n_fft: int,
    rolloff_ratio: float,
    feature_mode: str,
    eps: float,
) -> dict[str, Any]:
    config = FeatureConfig(
        sample_rate=sample_rate,
        frame_ms=frame_ms,
        hop_ms=hop_ms,
        n_fft=n_fft,
        rolloff_ratio=rolloff_ratio,
        feature_mode=feature_mode,
        eps=eps,
    )
    background, events = _calibration_samples(config)
    names = list(config.feature_names)
    background_scaled = _scaled_features(background, names)
    centers = [np.median(background_scaled, axis=0)]
    class_names = ["background"]
    for event_type, samples in zip(ACTIVE_EVENT_TYPES, events):
        event_scaled = _scaled_features(samples, names)
        centers.append(np.median(event_scaled, axis=0))
        class_names.append(event_type)
    center_matrix = np.ascontiguousarray(np.stack(centers, axis=0), dtype=np.float64)
    # Cache canonical bytes logically by keeping only numeric immutable-ish data.
    return {
        "classes": tuple(class_names),
        "centers": center_matrix,
        "names": tuple(names),
        "calibration_counts": {
            "background_frames": int(background.shape[0]),
            **{
                event_type: int(samples.shape[0])
                for event_type, samples in zip(ACTIVE_EVENT_TYPES, events)
            },
        },
    }


def build_prototype_model(config: FeatureConfig) -> dict[str, Any]:
    validate_feature_config(config)
    model = _prototype_centers_cached(
        config.sample_rate,
        float(config.frame_ms),
        float(config.hop_ms),
        config.n_fft,
        float(config.rolloff_ratio),
        config.feature_mode,
        float(config.eps),
    )
    return {
        "classes": list(model["classes"]),
        "centers": model["centers"].copy(),
        "names": list(model["names"]),
        "calibration_counts": dict(model["calibration_counts"]),
    }


def _feature_matrix_for_profile(
    feature_result: FeatureResult,
    profile: PathProfile,
) -> tuple[np.ndarray, list[str]]:
    available = list(feature_result.config.feature_names)
    missing = [name for name in profile.feature_names if name not in available]
    if missing:
        raise AedError(
            BusinessCode.E_CONFIG_INVALID,
            f"feature result lacks columns required by path {profile.name}: {missing}",
            dialog_id=feature_result.dialog_id,
            field="feature_mode",
            details={"missing_features": missing, "path": profile.name},
        )
    indices = [available.index(name) for name in profile.feature_names]
    return np.ascontiguousarray(feature_result.features[:, indices], dtype=np.float64), list(
        profile.feature_names
    )


def _classify_matrix(
    raw_features: np.ndarray,
    names: Sequence[str],
    model: dict[str, Any],
    threshold: float,
) -> tuple[list[str], np.ndarray]:
    scaled = _scaled_features(raw_features, names)
    centers = model["centers"]
    distances = np.sqrt(
        np.sum((scaled[:, None, :] - centers[None, :, :]) ** 2, axis=2) / scaled.shape[1]
    )
    event_distances = distances[:, 1:]
    background_distances = distances[:, 0]
    best_event = np.argmin(event_distances, axis=1)
    best_event_distance = event_distances[np.arange(scaled.shape[0]), best_event]
    event_scores = np.exp(-best_event_distance / _MATCH_TEMPERATURE)
    active = (best_event_distance <= background_distances) & (event_scores >= threshold)

    labels: list[str] = []
    scores = np.zeros(scaled.shape[0], dtype=np.float64)
    for frame_index in range(scaled.shape[0]):
        if active[frame_index]:
            labels.append(ACTIVE_EVENT_TYPES[int(best_event[frame_index])])
            scores[frame_index] = float(event_scores[frame_index])
        else:
            labels.append("background")
    return labels, scores


def _refine_labels(
    labels: Sequence[str],
    scores: Sequence[float],
    passes: int,
) -> tuple[list[str], list[float]]:
    current = list(labels)
    current_scores = [float(score) for score in scores]
    for _ in range(max(0, passes - 1)):
        updated = list(current)
        updated_scores = list(current_scores)
        for index, label in enumerate(current):
            if label != "background":
                continue
            left = current[index - 1] if index > 0 else "background"
            right = current[index + 1] if index + 1 < len(current) else "background"
            if left == right and left != "background":
                updated[index] = left
                neighboring_scores = []
                if index > 0:
                    neighboring_scores.append(current_scores[index - 1])
                if index + 1 < len(current):
                    neighboring_scores.append(current_scores[index + 1])
                updated_scores[index] = min(neighboring_scores) if neighboring_scores else 0.0
            elif left != "background" and right != "background" and left != right:
                left_key = (current_scores[index - 1], -event_type_id(left))
                right_key = (current_scores[index + 1], -event_type_id(right))
                selected = left if left_key >= right_key else right
                updated[index] = selected
                updated_scores[index] = (
                    current_scores[index - 1]
                    if selected == left
                    else current_scores[index + 1]
                )
        current = updated
        current_scores = updated_scores
    return current, current_scores


def _group_candidates(
    labels: Sequence[str],
    scores: Sequence[float],
    dialog_id: str,
) -> list[Candidate]:
    candidates: list[Candidate] = []
    start: int | None = None
    current_type: str | None = None
    span_scores: list[float] = []
    for index, label in enumerate(labels):
        if label == current_type and label != "background":
            span_scores.append(float(scores[index]))
            continue
        if current_type is not None and current_type != "background" and start is not None:
            candidates.append(
                Candidate(
                    start_frame=start,
                    end_frame=index - 1,
                    event_type=current_type,
                    confidence=float(np.mean(span_scores)),
                )
            )
        if label == "background":
            start = None
            current_type = None
            span_scores = []
        else:
            start = index
            current_type = label
            span_scores = [float(scores[index])]
    if current_type is not None and current_type != "background" and start is not None:
        candidates.append(
            Candidate(
                start_frame=start,
                end_frame=len(labels) - 1,
                event_type=current_type,
                confidence=float(np.mean(span_scores)),
            )
        )

    validate_candidates(candidates, len(labels), dialog_id=dialog_id)
    candidates.sort(
        key=lambda candidate: (
            -round(candidate.confidence, 12),
            event_type_id(candidate.event_type),
            candidate.start_frame,
            candidate.end_frame,
        )
    )
    return candidates


def validate_candidates(
    candidates: Sequence[Candidate],
    frame_count: int,
    *,
    dialog_id: str,
) -> None:
    for event_index, candidate in enumerate(candidates):
        if candidate.event_type not in EVENT_TYPE_IDS or candidate.event_type == "background":
            raise AedError(
                BusinessCode.E_EVENT_TYPE,
                f"candidate {event_index} has invalid event type {candidate.event_type!r}",
                dialog_id=dialog_id,
                field="event_type",
                details={
                    "event_index": event_index,
                    "start_frame": candidate.start_frame,
                    "end_frame": candidate.end_frame,
                    "event_type": candidate.event_type,
                },
            )
        if (
            isinstance(candidate.start_frame, bool)
            or isinstance(candidate.end_frame, bool)
            or not isinstance(candidate.start_frame, int)
            or not isinstance(candidate.end_frame, int)
            or candidate.start_frame < 0
            or candidate.end_frame < candidate.start_frame
            or candidate.end_frame >= frame_count
        ):
            raise AedError(
                BusinessCode.E_EVENT_RANGE,
                f"candidate {event_index} range is outside [0, {frame_count})",
                dialog_id=dialog_id,
                field="event_range",
                details={
                    "event_index": event_index,
                    "start_frame": candidate.start_frame,
                    "end_frame": candidate.end_frame,
                    "frame_count": frame_count,
                    "event_type": candidate.event_type,
                },
            )


def detect_candidates(
    feature_result: FeatureResult,
    profile: PathProfile,
    *,
    detection_config: Any,
) -> DetectionResult:
    """Detect and classify candidates on a canonical frame lattice.

    The approximate path evaluates every ``analysis_stride_frames`` anchor and
    holds each anchor's decision over the intervening canonical frames. This
    keeps 100% frame coverage while reducing classifier work.
    """

    raw_matrix, names = _feature_matrix_for_profile(feature_result, profile)
    model = build_prototype_model(
        FeatureConfig(
            sample_rate=feature_result.config.sample_rate,
            frame_ms=feature_result.config.frame_ms,
            hop_ms=feature_result.config.hop_ms,
            n_fft=feature_result.config.n_fft,
            rolloff_ratio=feature_result.config.rolloff_ratio,
            feature_mode=profile.feature_mode,
            eps=feature_result.config.eps,
        )
    )

    frame_count = raw_matrix.shape[0]
    labels = ["background"] * frame_count
    scores = np.zeros(frame_count, dtype=np.float64)
    stride = profile.analysis_stride_frames
    anchors = list(range(0, frame_count, stride))
    anchor_matrix = raw_matrix[anchors]
    anchor_labels, anchor_scores = _classify_matrix(
        anchor_matrix,
        names,
        model,
        profile.detection_threshold,
    )
    for anchor_position, anchor_index in enumerate(anchors):
        next_index = anchors[anchor_position + 1] if anchor_position + 1 < len(anchors) else frame_count
        for frame_index in range(anchor_index, next_index):
            labels[frame_index] = anchor_labels[anchor_position]
            scores[frame_index] = anchor_scores[anchor_position]

    labels, refined_scores = _refine_labels(
        labels,
        scores.tolist(),
        profile.classifier_passes,
    )
    candidates = _group_candidates(labels, refined_scores, feature_result.dialog_id)
    return DetectionResult(
        dialog_id=feature_result.dialog_id,
        frame_labels=labels,
        frame_scores=[float(score) for score in refined_scores],
        candidates=candidates,
        passes=profile.classifier_passes,
        analysis_stride_frames=stride,
        feature_dimension=len(names),
    )
