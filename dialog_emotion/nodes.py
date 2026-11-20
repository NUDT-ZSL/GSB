from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np

from .config import PipelineConfig, enforce_feature_memory, estimate_feature_memory_bytes
from .dataset import RegistrySample, load_wav_pcm
from .errors import (
    DomainError,
    E_CONFIG_INVALID,
    E_FRAME_COUNT_MISMATCH,
    E_INCONSISTENT,
    E_INTERVAL_INVALID,
    E_REGISTRY_INVALID,
    E_SEGMENT_GAP,
    E_SPEAKER_SET_INVALID,
)
from .util import quantize, sha256_value


FEATURE_NAMES = [
    "short_time_energy",
    "zero_crossing_rate",
    "spectral_centroid_hz",
    "spectral_rolloff_hz",
]


def _frame_count(sample_count: int, frame_samples: int, shift_samples: int) -> int:
    if sample_count < frame_samples:
        return 0
    return (sample_count - frame_samples) // shift_samples + 1


def compute_feature_matrix(
    pcm: np.ndarray,
    sample_rate: int,
    config: PipelineConfig,
    dtype: str,
) -> np.ndarray:
    frame_samples = config.frame_samples
    shift_samples = config.shift_samples
    frame_count = _frame_count(len(pcm), frame_samples, shift_samples)
    if frame_count == 0:
        return np.empty((0, len(FEATURE_NAMES)), dtype=dtype)
    working = pcm.astype(dtype, copy=False)
    window = np.hanning(frame_samples).astype(dtype)
    frequencies = np.fft.rfftfreq(config.fft_size, d=1.0 / sample_rate).astype(dtype)
    matrix = np.empty((frame_count, len(FEATURE_NAMES)), dtype=dtype)
    for frame_index in range(frame_count):
        start = frame_index * shift_samples
        frame = working[start : start + frame_samples]
        windowed = frame * window
        energy = float(np.mean(windowed * windowed, dtype=np.float64))
        if frame_samples > 1:
            signs = frame[:-1] * frame[1:]
            zcr = float(np.count_nonzero(signs < 0)) / float(frame_samples - 1)
        else:
            zcr = 0.0
        spectrum = np.abs(np.fft.rfft(windowed, n=config.fft_size)) ** 2
        total_power = float(np.sum(spectrum, dtype=np.float64))
        if total_power > 0:
            centroid = float(np.sum(frequencies * spectrum, dtype=np.float64) / total_power)
            cumulative = np.cumsum(spectrum, dtype=np.float64)
            threshold = 0.85 * total_power
            rolloff_index = int(np.searchsorted(cumulative, threshold, side="left"))
            rolloff_index = min(rolloff_index, len(frequencies) - 1)
            rolloff = float(frequencies[rolloff_index])
        else:
            centroid = 0.0
            rolloff = 0.0
        matrix[frame_index, 0] = energy
        matrix[frame_index, 1] = zcr
        matrix[frame_index, 2] = centroid
        matrix[frame_index, 3] = rolloff
    return matrix


def build_frame_features(
    dialog_id: str,
    pcm: np.ndarray,
    sample_rate: int,
    expected_frame_count: int,
    config: PipelineConfig,
    audio_content_hash: str,
) -> tuple[dict[str, Any], list[str]]:
    frame_count = _frame_count(len(pcm), config.frame_samples, config.shift_samples)
    if frame_count != expected_frame_count:
        raise DomainError(
            E_FRAME_COUNT_MISMATCH,
            "feature frame count does not match the truth frame count",
            dialog_id=dialog_id,
            expected_frame_count=expected_frame_count,
            actual_frame_count=frame_count,
            field="frame_index",
        )
    estimated = estimate_feature_memory_bytes(frame_count, config.frame_samples, config.fft_size)
    dtype, degraded, _ = enforce_feature_memory(config, estimated)
    matrix = compute_feature_matrix(pcm, sample_rate, config, dtype)
    rows = [
        [quantize(float(value), 8) for value in row]
        for row in matrix
    ]
    payload = {
        "schema_version": config.frame_schema_version,
        "dialog_id": dialog_id,
        "sample_rate": sample_rate,
        "frame_ms": quantize(config.frame_ms),
        "frame_shift_ms": quantize(config.frame_shift_ms),
        "frame_length_samples": config.frame_samples,
        "frame_shift_samples": config.shift_samples,
        "frame_count": frame_count,
        "expected_frame_count": expected_frame_count,
        "feature_names": FEATURE_NAMES,
        "feature_dtype": dtype,
        "features": rows,
        "audio_content_hash": audio_content_hash,
        "degraded": degraded,
    }
    return payload, degraded


def _zscore_stats(matrix: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    if len(matrix) == 0:
        return np.zeros(matrix.shape[1]), np.ones(matrix.shape[1])
    mean = np.mean(matrix, axis=0, dtype=np.float64)
    std = np.std(matrix, axis=0, dtype=np.float64)
    std = np.where(std < 1e-8, 1.0, std)
    return mean, std


def _reference_matrix(sample: RegistrySample, config: PipelineConfig) -> np.ndarray:
    if sample.ref_path is not None and sample.ref_path.exists():
        pcm, sample_rate, _ = load_wav_pcm(sample.ref_path)
        if sample_rate != 16000:
            raise DomainError(
                E_REGISTRY_INVALID,
                "registration sample sample rate must be 16000",
                field="sample_rate",
                speaker_id=sample.speaker_id,
                sample_rate=sample_rate,
            )
    else:
        from .synth import synthesize_reference

        pcm = synthesize_reference(sample.speaker_id, sample.ref_seed, sample.ref_duration_ms)
    matrix = compute_feature_matrix(pcm, 16000, config, "float64")
    if len(matrix) == 0:
        raise DomainError(
            E_REGISTRY_INVALID,
            "registration sample is shorter than one analysis frame",
            field="ref_duration_ms",
            speaker_id=sample.speaker_id,
            sample_id=sample.raw.get("sample_id"),
        )
    return matrix


def _runs(labels: list[str]) -> list[tuple[int, int, str]]:
    if not labels:
        return []
    runs: list[tuple[int, int, str]] = []
    start = 0
    current = labels[0]
    for index in range(1, len(labels)):
        if labels[index] != current:
            runs.append((start, index, current))
            start = index
            current = labels[index]
    runs.append((start, len(labels), current))
    return runs


def _smooth_labels(labels: list[str], threshold_frames: int) -> list[str]:
    if threshold_frames <= 1 or not labels:
        return labels
    result = list(labels)
    for _ in range(max(1, len(result))):
        changed = False
        runs = _runs(result)
        for run_index, (start, end, _speaker) in enumerate(runs):
            if end - start >= threshold_frames:
                continue
            replacement: str | None = None
            if run_index > 0:
                replacement = runs[run_index - 1][2]
            elif run_index + 1 < len(runs):
                replacement = runs[run_index + 1][2]
            if replacement is not None and replacement != result[start]:
                for position in range(start, end):
                    result[position] = replacement
                changed = True
        if not changed:
            break
    return result


def validate_speaker_output(
    labels: list[str],
    segments: list[dict[str, Any]],
    allowed_speakers: set[str],
    dialog_id: str,
) -> None:
    for frame_index, speaker_id in enumerate(labels):
        if speaker_id not in allowed_speakers:
            raise DomainError(
                E_SPEAKER_SET_INVALID,
                "assigned speaker is outside the dialog speaker set",
                dialog_id=dialog_id,
                frame_position=frame_index,
                speaker_id=speaker_id,
                allowed_speakers=sorted(allowed_speakers),
            )
    expected_start = 0
    for segment in segments:
        start = int(segment["start_frame"])
        end = int(segment["end_frame"])
        if start != expected_start:
            raise DomainError(
                E_SEGMENT_GAP,
                "speaker segments are not contiguous",
                dialog_id=dialog_id,
                frame_position=expected_start,
                segment_id=segment.get("segment_id"),
                segment_start=start,
                expected_start=expected_start,
            )
        if end <= start:
            raise DomainError(
                E_SEGMENT_GAP,
                "speaker segment is empty or reversed",
                dialog_id=dialog_id,
                frame_position=start,
                segment_id=segment.get("segment_id"),
            )
        expected_start = end
    if expected_start != len(labels):
        raise DomainError(
            E_SEGMENT_GAP,
            "speaker segments do not cover all frames",
            dialog_id=dialog_id,
            frame_position=expected_start,
            covered_frame_count=expected_start,
            frame_count=len(labels),
        )


def build_speaker_assignment(
    dialog_id: str,
    frame_payload: dict[str, Any],
    registry: list[RegistrySample],
    allowed_speakers: tuple[str, ...],
    config: PipelineConfig,
    feature_hash: str,
) -> dict[str, Any]:
    matrix = np.asarray(frame_payload["features"], dtype=np.float64)
    frame_count = int(frame_payload["frame_count"])
    if matrix.shape != (frame_count, len(FEATURE_NAMES)):
        raise DomainError(
            E_INCONSISTENT,
            "frame feature matrix shape is inconsistent",
            category="feature_shape",
            dialog_id=dialog_id,
            shape=list(matrix.shape),
            frame_count=frame_count,
        )
    mean, std = _zscore_stats(matrix)
    normalized_frames = (matrix - mean) / std
    grouped: dict[str, list[RegistrySample]] = {}
    for sample in registry:
        grouped.setdefault(sample.speaker_id, []).append(sample)
    embeddings: dict[str, dict[str, Any]] = {}
    for speaker_id in sorted(allowed_speakers):
        samples = grouped.get(speaker_id, [])
        if not samples:
            raise DomainError(
                E_REGISTRY_INVALID,
                "dialog speaker has no registration samples",
                field="speaker_id",
                dialog_id=dialog_id,
                speaker_id=speaker_id,
            )
        vectors: list[np.ndarray] = []
        for sample in samples:
            reference_matrix = _reference_matrix(sample, config)
            reference_normalized = (reference_matrix - mean) / std
            vectors.append(np.mean(reference_normalized, axis=0))
        stacked = np.vstack(vectors)
        center = np.mean(stacked, axis=0)
        dispersion = np.std(stacked, axis=0)
        embeddings[speaker_id] = {
            "mean": [quantize(float(value), 8) for value in center],
            "std": [quantize(float(value), 8) for value in dispersion],
            "sample_count": len(samples),
            "embedding_versions": sorted({sample.embedding_version for sample in samples}),
        }
    weights = np.asarray([0.10, 0.20, 0.35, 0.35], dtype=np.float64)
    labels: list[str] = []
    scores: list[float] = []
    for frame_index in range(frame_count):
        frame_vector = normalized_frames[frame_index]
        best_speaker = sorted(allowed_speakers)[0]
        best_distance = math.inf
        for speaker_id in sorted(allowed_speakers):
            center = np.asarray(embeddings[speaker_id]["mean"], dtype=np.float64)
            distance = float(np.sum(weights * (frame_vector - center) ** 2))
            if distance < best_distance:
                best_distance = distance
                best_speaker = speaker_id
        labels.append(best_speaker)
        scores.append(quantize(-best_distance, 8))
    threshold_frames = int(round(config.segment_threshold_ms / config.frame_shift_ms)) if config.frame_shift_ms else 0
    labels = _smooth_labels(labels, threshold_frames)
    segments: list[dict[str, Any]] = []
    for run_index, (start, end, speaker_id) in enumerate(_runs(labels)):
        segments.append(
            {
                "segment_id": f"spkseg_{run_index:04d}",
                "start_frame": start,
                "end_frame": end,
                "frame_count": end - start,
                "speaker_id": speaker_id,
            }
        )
    validate_speaker_output(labels, segments, set(allowed_speakers), dialog_id)
    return {
        "schema_version": config.speaker_schema_version,
        "dialog_id": dialog_id,
        "frame_count": frame_count,
        "feature_hash": feature_hash,
        "frame_labels": labels,
        "frame_scores": scores,
        "speaker_embeddings": embeddings,
        "segments": segments,
        "segment_threshold_ms": quantize(config.segment_threshold_ms),
        "embedding_version": config.embedding_version,
    }


def _feature_design(matrix: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    mean = np.mean(matrix, axis=0, dtype=np.float64)
    std = np.std(matrix, axis=0, dtype=np.float64)
    std = np.where(std < 1e-8, 1.0, std)
    normalized = (matrix - mean) / std
    design = np.column_stack([np.ones(len(matrix), dtype=np.float64), normalized])
    return design, mean, std


def _regress_segment(
    design: np.ndarray,
    target: np.ndarray,
    ridge: float,
    z_value: float,
) -> tuple[float, float, float, list[float]]:
    if len(target) == 0:
        return 0.0, 0.0, 0.0, [0.0] * design.shape[1]
    if len(target) < 2:
        score = float(target[0])
        coefficients = [score] + [0.0] * (design.shape[1] - 1)
        return score, score, score, coefficients
    xtx = design.T @ design
    regularized = xtx + ridge * np.eye(design.shape[1], dtype=np.float64)
    xty = design.T @ target
    try:
        coefficients = np.linalg.solve(regularized, xty)
    except np.linalg.LinAlgError:
        coefficients, *_ = np.linalg.lstsq(regularized, xty, rcond=None)
    frame_mean = np.mean(design, axis=0)
    score = float(frame_mean @ coefficients)
    predictions = design @ coefficients
    residuals = target - predictions
    degrees = max(1, len(target) - design.shape[1])
    residual_variance = float(np.sum(residuals * residuals) / degrees)
    try:
        inverse = np.linalg.pinv(regularized)
        leverage = float(frame_mean @ inverse @ frame_mean.T)
    except np.linalg.LinAlgError:
        leverage = float(1.0 / max(1, len(target)))
    standard_error = math.sqrt(max(0.0, residual_variance * (1.0 / len(target) + leverage)))
    half_width = z_value * standard_error
    return score, score - half_width, score + half_width, [float(value) for value in coefficients]


def validate_intervals(segments: list[dict[str, Any]], dialog_id: str) -> None:
    for segment in segments:
        pairs = (
            ("ci_lower_valence", "ci_upper_valence"),
            ("ci_lower_arousal", "ci_upper_arousal"),
        )
        for lower_name, upper_name in pairs:
            lower = float(segment[lower_name])
            upper = float(segment[upper_name])
            if not math.isfinite(lower) or not math.isfinite(upper) or lower > upper:
                raise DomainError(
                    E_INTERVAL_INVALID,
                    "confidence interval lower bound exceeds upper bound",
                    dialog_id=dialog_id,
                    segment_id=segment.get("segment_id"),
                    field=lower_name,
                    lower=lower,
                    upper=upper,
                )


def _segment_source_digest(
    start: int,
    end: int,
    feature_rows: list[list[float]],
    labels: list[str],
    truth_rows: list[dict[str, Any]],
    config: PipelineConfig,
) -> str:
    payload = {
        "features": feature_rows[start:end],
        "labels": labels[start:end],
        "valence": [truth_rows[index]["valence"] for index in range(start, end)],
        "arousal": [truth_rows[index]["arousal"] for index in range(start, end)],
        "ridge": config.regression_ridge,
        "confidence_level": config.confidence_level,
        "schema_version": config.emotion_schema_version,
    }
    return sha256_value(payload)


def build_segment_emotions(
    dialog_id: str,
    frame_payload: dict[str, Any],
    speaker_payload: dict[str, Any],
    truth_rows: list[dict[str, Any]],
    config: PipelineConfig,
    feature_hash: str,
    speaker_hash: str,
    truth_version: str,
    correction_version: str,
    reusable_segments: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    matrix = np.asarray(frame_payload["features"], dtype=np.float64)
    frame_count = int(frame_payload["frame_count"])
    labels = speaker_payload["frame_labels"]
    if frame_count != len(truth_rows) or frame_count != len(labels):
        raise DomainError(
            E_INCONSISTENT,
            "frame, truth, and speaker label counts differ",
            category="frame_index_domain",
            dialog_id=dialog_id,
            feature_frame_count=frame_count,
            truth_frame_count=len(truth_rows),
            speaker_frame_count=len(labels),
        )
    design, _, _ = _feature_design(matrix)
    targets = {
        "valence": np.asarray([float(row["valence"]) for row in truth_rows], dtype=np.float64),
        "arousal": np.asarray([float(row["arousal"]) for row in truth_rows], dtype=np.float64),
    }
    z_value = 1.959963984540054
    details: list[dict[str, Any]] = []
    source_digests: dict[str, str] = {}
    for segment_index, speaker_segment in enumerate(speaker_payload["segments"]):
        start = int(speaker_segment["start_frame"])
        end = int(speaker_segment["end_frame"])
        segment_id = f"seg_{segment_index:04d}"
        digest = _segment_source_digest(start, end, frame_payload["features"], labels, truth_rows, config)
        source_digests[segment_id] = digest
        reusable = (reusable_segments or {}).get(segment_id)
        if reusable is not None and reusable.get("source_digest") == digest:
            detail = dict(reusable)
            detail["speaker_segment_id"] = speaker_segment["segment_id"]
            details.append(detail)
            continue
        valence, lower_valence, upper_valence, valence_coefficients = _regress_segment(
            design[start:end],
            targets["valence"][start:end],
            config.regression_ridge,
            z_value,
        )
        arousal, lower_arousal, upper_arousal, arousal_coefficients = _regress_segment(
            design[start:end],
            targets["arousal"][start:end],
            config.regression_ridge,
            z_value,
        )
        duration_ms = (end - start) * config.frame_shift_ms
        detail = {
            "segment_id": segment_id,
            "speaker_segment_id": speaker_segment["segment_id"],
            "speaker_id": speaker_segment["speaker_id"],
            "start_frame": start,
            "end_frame": end,
            "frame_count": end - start,
            "start_ms": quantize(start * config.frame_shift_ms),
            "end_ms": quantize(end * config.frame_shift_ms),
            "duration_ms": quantize(duration_ms),
            "valence": quantize(valence),
            "arousal": quantize(arousal),
            "ci_lower_valence": quantize(lower_valence),
            "ci_upper_valence": quantize(upper_valence),
            "ci_lower_arousal": quantize(lower_arousal),
            "ci_upper_arousal": quantize(upper_arousal),
            "confidence_level": quantize(config.confidence_level),
            "regression_method": "ridge_ols_normal_ci",
            "coefficient_names": FEATURE_NAMES,
            "valence_coefficients": [quantize(value, 8) for value in valence_coefficients],
            "arousal_coefficients": [quantize(value, 8) for value in arousal_coefficients],
            "source_digest": digest,
        }
        details.append(detail)
    validate_intervals(details, dialog_id)
    return {
        "schema_version": config.emotion_schema_version,
        "dialog_id": dialog_id,
        "frame_count": frame_count,
        "segment_count": len(details),
        "feature_hash": feature_hash,
        "speaker_hash": speaker_hash,
        "truth_version": truth_version,
        "truth_correction_version": correction_version,
        "segments": details,
        "source_digests": source_digests,
    }


def build_dialog_aggregate(
    dialog_id: str,
    segment_payload: dict[str, Any],
    config: PipelineConfig,
    segment_hash: str,
) -> dict[str, Any]:
    segments = segment_payload["segments"]
    if not segments:
        raise DomainError(
            E_INCONSISTENT,
            "dialog has no segment scores to aggregate",
            category="empty_segments",
            dialog_id=dialog_id,
        )
    validate_intervals(segments, dialog_id)
    total_duration = sum(float(segment["duration_ms"]) for segment in segments)
    if total_duration <= 0:
        raise DomainError(
            E_INCONSISTENT,
            "segment durations sum to zero",
            category="weight_denominator",
            dialog_id=dialog_id,
        )
    raw_weights = [float(segment["duration_ms"]) / total_duration for segment in segments]
    weights = [quantize(value, 12) for value in raw_weights]
    adjustment = quantize(1.0 - sum(weights), 12)
    weights[-1] = quantize(weights[-1] + adjustment, 12)
    weight_rows = []
    for segment, weight in zip(segments, weights):
        weight_rows.append(
            {
                "segment_id": segment["segment_id"],
                "duration_ms": segment["duration_ms"],
                "weight": weight,
            }
        )

    def weighted(field: str) -> float:
        return quantize(sum(weight * float(segment[field]) for segment, weight in zip(segments, weights)), 8)

    valence = weighted("valence")
    arousal = weighted("arousal")
    lower_valence = weighted("ci_lower_valence")
    upper_valence = weighted("ci_upper_valence")
    lower_arousal = weighted("ci_lower_arousal")
    upper_arousal = weighted("ci_upper_arousal")
    if lower_valence > upper_valence or lower_arousal > upper_arousal:
        raise DomainError(
            E_INTERVAL_INVALID,
            "aggregated confidence interval is invalid",
            dialog_id=dialog_id,
            segment_id=None,
        )
    independent = {
        "valence": quantize(sum(row["weight"] * float(segment["valence"]) for row, segment in zip(weight_rows, segments)), 8),
        "arousal": quantize(sum(row["weight"] * float(segment["arousal"]) for row, segment in zip(weight_rows, segments)), 8),
        "ci_lower_valence": quantize(
            sum(row["weight"] * float(segment["ci_lower_valence"]) for row, segment in zip(weight_rows, segments)), 8
        ),
        "ci_upper_valence": quantize(
            sum(row["weight"] * float(segment["ci_upper_valence"]) for row, segment in zip(weight_rows, segments)), 8
        ),
        "ci_lower_arousal": quantize(
            sum(row["weight"] * float(segment["ci_lower_arousal"]) for row, segment in zip(weight_rows, segments)), 8
        ),
        "ci_upper_arousal": quantize(
            sum(row["weight"] * float(segment["ci_upper_arousal"]) for row, segment in zip(weight_rows, segments)), 8
        ),
    }
    actual = {
        "valence": valence,
        "arousal": arousal,
        "ci_lower_valence": lower_valence,
        "ci_upper_valence": upper_valence,
        "ci_lower_arousal": lower_arousal,
        "ci_upper_arousal": upper_arousal,
    }
    mismatches = [field for field in actual if actual[field] != independent[field]]
    if mismatches:
        raise DomainError(
            E_INCONSISTENT,
            "dialog aggregate cannot be independently reproduced",
            category="aggregate_recomputation",
            dialog_id=dialog_id,
            mismatched_fields=mismatches,
        )
    sum_weights = quantize(sum(weights), 12)
    if abs(sum_weights - 1.0) > 1e-9:
        raise DomainError(
            E_INCONSISTENT,
            "segment weights do not sum to one",
            category="weight_sum",
            dialog_id=dialog_id,
            sum_weights=sum_weights,
        )
    return {
        "schema_version": config.aggregate_schema_version,
        "dialog_id": dialog_id,
        "segment_hash": segment_hash,
        "weight_basis": "segment_duration_ms",
        "duration_basis": "frame_count * frame_shift_ms",
        "total_duration_ms": quantize(total_duration),
        "sum_weights": sum_weights,
        "weights": weight_rows,
        "valence": valence,
        "arousal": arousal,
        "ci_lower_valence": lower_valence,
        "ci_upper_valence": upper_valence,
        "ci_lower_arousal": lower_arousal,
        "ci_upper_arousal": upper_arousal,
        "confidence_level": quantize(config.confidence_level),
        "independent_recomputation": independent,
        "recomputation_consistent": True,
    }


def cross_node_self_check(
    dialog_id: str,
    frame_payload: dict[str, Any],
    speaker_payload: dict[str, Any],
    segment_payload: dict[str, Any],
    aggregate_payload: dict[str, Any],
) -> dict[str, Any]:
    categories: list[str] = []
    frame_count = int(frame_payload["frame_count"])
    if speaker_payload.get("frame_count") != frame_count or len(speaker_payload.get("frame_labels", [])) != frame_count:
        categories.append("frame_label_count")
    speaker_segments = speaker_payload.get("segments", [])
    covered = 0
    for segment in speaker_segments:
        if int(segment["start_frame"]) != covered:
            categories.append("speaker_segment_coverage")
            break
        covered = int(segment["end_frame"])
    if covered != frame_count:
        categories.append("speaker_segment_tail")
    details = segment_payload.get("segments", [])
    if len(details) != len(speaker_segments):
        categories.append("segment_count")
    for detail, speaker_segment in zip(details, speaker_segments):
        if (
            detail.get("start_frame") != speaker_segment.get("start_frame")
            or detail.get("end_frame") != speaker_segment.get("end_frame")
            or detail.get("speaker_id") != speaker_segment.get("speaker_id")
        ):
            categories.append("segment_index_domain")
            break
    weight_rows = aggregate_payload.get("weights", [])
    if [row.get("segment_id") for row in weight_rows] != [detail.get("segment_id") for detail in details]:
        categories.append("aggregate_weight_segments")
    if len({detail.get("segment_id") for detail in details}) != len(details):
        categories.append("duplicate_segment_ids")
    if categories:
        raise DomainError(
            E_INCONSISTENT,
            "cross-node consistency self-check failed",
            category="cross_node_self_check",
            categories=sorted(set(categories)),
            dialog_id=dialog_id,
        )
    return {
        "passed": True,
        "dialog_id": dialog_id,
        "frame_index_domain": "frame_index",
        "segment_index_domain": "frame_index_half_open",
        "frame_count": frame_count,
        "speaker_segment_count": len(speaker_segments),
        "emotion_segment_count": len(details),
        "aggregate_weight_count": len(weight_rows),
    }
