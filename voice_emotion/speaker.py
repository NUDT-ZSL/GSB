"""Stage 2: deterministic speaker embeddings and frame assignment."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import numpy as np

from .canonical import decode_array, encode_array, hash_value
from .config import FeatureConfig, SpeakerConfig
from .dataset import EnrollmentRecord
from .errors import E_ENROLLMENT_MISSING, E_SPEAKER_UNKNOWN, raise_error
from .features import FeatureResult, extract_features
from .synthesis import synthesize_registration


@dataclass(frozen=True)
class SpeakerEmbedding:
    speaker_id: int
    mean: np.ndarray
    std: np.ndarray
    sample_count: int
    registration_frame_count: int

    def normalized_mean(self) -> np.ndarray:
        vector = np.asarray(self.mean, dtype=np.float64)
        norm = float(np.linalg.norm(vector))
        return vector / norm if norm > 0.0 else vector


@dataclass(frozen=True)
class SpeakerRegistry:
    embeddings: dict[int, SpeakerEmbedding]
    feature_config_hash: str
    enrollment_version: str

    @property
    def output_hash(self) -> str:
        return hash_value(
            {
                "kind": "speaker-registry",
                "feature_config_hash": self.feature_config_hash,
                "enrollment_version": self.enrollment_version,
                "embeddings": [
                    {
                        "speaker_id": embedding.speaker_id,
                        "mean": encode_array(embedding.mean),
                        "std": encode_array(embedding.std),
                        "sample_count": embedding.sample_count,
                        "registration_frame_count": embedding.registration_frame_count,
                    }
                    for _, embedding in sorted(self.embeddings.items())
                ],
            }
        )


@dataclass(frozen=True)
class Segment:
    segment_id: str
    start_frame: int
    end_frame: int
    speaker_id: int
    start_sample: int
    end_sample: int
    frame_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "start_frame": self.start_frame,
            "end_frame": self.end_frame,
            "speaker_id": self.speaker_id,
            "start_sample": self.start_sample,
            "end_sample": self.end_sample,
            "frame_count": self.frame_count,
        }


@dataclass(frozen=True)
class AssignmentResult:
    labels: np.ndarray
    segments: tuple[Segment, ...]
    best_scores: np.ndarray
    runner_up_margins: np.ndarray

    @property
    def output_hash(self) -> str:
        return hash_value(
            {
                "kind": "assignment-output",
                "labels": encode_array(self.labels),
                "segments": [segment.to_dict() for segment in self.segments],
                "best_scores": encode_array(self.best_scores),
                "runner_up_margins": encode_array(self.runner_up_margins),
            }
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "labels": encode_array(self.labels),
            "segments": [segment.to_dict() for segment in self.segments],
            "best_scores": encode_array(self.best_scores),
            "runner_up_margins": encode_array(self.runner_up_margins),
            "output_hash": self.output_hash,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "AssignmentResult":
        result = cls(
            labels=decode_array(payload["labels"]),
            segments=tuple(
                Segment(
                    segment_id=str(item["segment_id"]),
                    start_frame=int(item["start_frame"]),
                    end_frame=int(item["end_frame"]),
                    speaker_id=int(item["speaker_id"]),
                    start_sample=int(item["start_sample"]),
                    end_sample=int(item["end_sample"]),
                    frame_count=int(item["frame_count"]),
                )
                for item in payload["segments"]
            ),
            best_scores=decode_array(payload["best_scores"]),
            runner_up_margins=decode_array(payload["runner_up_margins"]),
        )
        if result.output_hash != payload.get("output_hash"):
            from .errors import E_CACHE_CORRUPT

            raise_error(E_CACHE_CORRUPT, "assignment payload output hash mismatch")
        return result


def embedding_vectors(features: np.ndarray, sample_rate: int = 16_000) -> np.ndarray:
    values = np.asarray(features, dtype=np.float64)
    if values.ndim != 2 or values.shape[1] != 4:
        raise_error(E_SPEAKER_UNKNOWN, "feature matrix must have four columns")
    nyquist = float(sample_rate) / 2.0
    return np.column_stack(
        (
            np.log1p(np.maximum(values[:, 0], 0.0)) * 0.35,
            values[:, 1] * 4.0,
            values[:, 2] / nyquist,
            values[:, 3] / nyquist,
        )
    )


def build_speaker_registry(
    enrollments: Iterable[EnrollmentRecord],
    feature_config: FeatureConfig,
    enrollment_version: str,
    sample_rate: int = 16_000,
) -> SpeakerRegistry:
    grouped: dict[int, list[EnrollmentRecord]] = {}
    for record in enrollments:
        grouped.setdefault(int(record.speaker_id), []).append(record)
    if not grouped:
        raise_error(E_ENROLLMENT_MISSING, "enrollment manifest is empty")
    embeddings: dict[int, SpeakerEmbedding] = {}
    for speaker_id in sorted(grouped):
        vectors: list[np.ndarray] = []
        registration_frames = 0
        for record in sorted(
            grouped[speaker_id],
            key=lambda item: (item.sample_index, item.seed, item.duration_ms),
        ):
            signal = synthesize_registration(
                speaker_id=speaker_id,
                seed=record.seed,
                duration_ms=record.duration_ms,
                sample_rate=sample_rate,
                sample_index=record.sample_index,
            )
            features = extract_features(signal, feature_config, sample_rate=sample_rate)
            if features.frame_count == 0:
                raise_error(
                    E_ENROLLMENT_MISSING,
                    "registration clip is shorter than one analysis frame",
                    speaker_id=speaker_id,
                    duration_ms=record.duration_ms,
                )
            vectors.append(embedding_vectors(features.features, sample_rate))
            registration_frames += features.frame_count
        joined = np.concatenate(vectors, axis=0)
        embeddings[speaker_id] = SpeakerEmbedding(
            speaker_id=speaker_id,
            mean=np.ascontiguousarray(np.mean(joined, axis=0, dtype=np.float64)),
            std=np.ascontiguousarray(np.std(joined, axis=0, dtype=np.float64)),
            sample_count=len(grouped[speaker_id]),
            registration_frame_count=registration_frames,
        )
    return SpeakerRegistry(
        embeddings=embeddings,
        feature_config_hash=feature_config.params_hash(),
        enrollment_version=enrollment_version,
    )


def choose_speaker(scores: Mapping[int, float]) -> int:
    if not scores:
        raise_error(E_SPEAKER_UNKNOWN, "no speaker candidates")
    best_speaker = min(scores, key=lambda speaker_id: (-float(scores[speaker_id]), int(speaker_id)))
    return int(best_speaker)


def _similarity_scores(
    frame_vectors: np.ndarray,
    registry: SpeakerRegistry,
    allowed_speaker_ids: Iterable[int],
) -> tuple[list[int], np.ndarray]:
    speakers = sorted(int(item) for item in set(allowed_speaker_ids))
    if not speakers:
        raise_error(E_SPEAKER_UNKNOWN, "dialog declares no speakers")
    missing = [speaker for speaker in speakers if speaker not in registry.embeddings]
    if missing:
        raise_error(
            E_SPEAKER_UNKNOWN,
            "dialog speaker has no enrollment embedding",
            missing_speaker_ids=missing,
        )
    columns = []
    for speaker in speakers:
        embedding = registry.embeddings[speaker]
        scale = np.maximum(np.asarray(embedding.std, dtype=np.float64), 1e-9)
        z_distance = np.sum(
            ((frame_vectors - embedding.mean) / scale) ** 2,
            axis=1,
            dtype=np.float64,
        )
        columns.append(-z_distance)
    return speakers, np.column_stack(columns)


def _merge_short_runs(labels: np.ndarray, minimum: int) -> np.ndarray:
    if minimum <= 1 or labels.size == 0:
        return labels.copy()
    result = labels.astype(np.int64, copy=True)
    while True:
        runs: list[tuple[int, int, int]] = []
        start = 0
        while start < result.size:
            end = start + 1
            while end < result.size and result[end] == result[start]:
                end += 1
            runs.append((start, end, int(result[start])))
            start = end
        bad = [(length, start, end, index) for index, (start, end, _) in enumerate(runs) for length in [end - start] if length < minimum]
        if not bad:
            break
        _, start, end, index = min(bad, key=lambda item: (item[0], item[1]))
        if index == 0:
            target = runs[1][2] if len(runs) > 1 else runs[0][2]
        elif index == len(runs) - 1:
            target = runs[index - 1][2]
        else:
            left_len = runs[index - 1][1] - runs[index - 1][0]
            right_len = runs[index + 1][1] - runs[index + 1][0]
            target = runs[index - 1][2] if left_len >= right_len else runs[index + 1][2]
        result[start:end] = target
        if len(runs) == 1:
            break
    return result


def labels_to_segments(
    labels: np.ndarray,
    frame_starts: np.ndarray,
    frame_samples: int,
) -> tuple[Segment, ...]:
    values = np.asarray(labels, dtype=np.int64)
    starts = np.asarray(frame_starts, dtype=np.int64)
    if values.shape[0] != starts.shape[0]:
        raise_error(E_SPEAKER_UNKNOWN, "label and frame start counts differ")
    segments: list[Segment] = []
    cursor = 0
    while cursor < values.shape[0]:
        end = cursor + 1
        while end < values.shape[0] and values[end] == values[cursor]:
            end += 1
        segments.append(
            Segment(
                segment_id=f"seg-{len(segments):04d}",
                start_frame=cursor,
                end_frame=end,
                speaker_id=int(values[cursor]),
                start_sample=int(starts[cursor]),
                end_sample=int(starts[end - 1] + frame_samples),
                frame_count=end - cursor,
            )
        )
        cursor = end
    return tuple(segments)


def assign_speakers(
    features: FeatureResult,
    registry: SpeakerRegistry,
    allowed_speaker_ids: Iterable[int],
    config: SpeakerConfig,
    frame_samples: int,
    sample_rate: int = 16_000,
) -> tuple[AssignmentResult, list[int]]:
    config.validate()
    speakers, score_matrix = _similarity_scores(
        embedding_vectors(features.features, sample_rate),
        registry,
        allowed_speaker_ids,
    )
    raw_labels = np.zeros(features.frame_count, dtype=np.int64)
    best_scores = np.zeros(features.frame_count, dtype=np.float64)
    margins = np.zeros(features.frame_count, dtype=np.float64)
    for index in range(features.frame_count):
        scores = {
            speaker: float(score_matrix[index, position])
            for position, speaker in enumerate(speakers)
        }
        chosen = choose_speaker(scores)
        raw_labels[index] = chosen
        ordered = sorted(scores.values(), reverse=True)
        best_scores[index] = scores[chosen]
        margins[index] = ordered[0] - ordered[1] if len(ordered) > 1 else ordered[0]
    labels = _merge_short_runs(raw_labels, config.segment_threshold)
    unknown = sorted(set(int(item) for item in labels.tolist()) - set(speakers))
    if unknown:
        raise_error(
            E_SPEAKER_UNKNOWN,
            "assigned speaker is outside the dialog declaration",
            speaker_ids=unknown,
            declared=sorted(speakers),
        )
    segments = labels_to_segments(
        labels,
        features.frame_starts,
        int(frame_samples),
    )
    return (
        AssignmentResult(
            labels=np.ascontiguousarray(labels, dtype=np.int64),
            segments=segments,
            best_scores=np.ascontiguousarray(best_scores),
            runner_up_margins=np.ascontiguousarray(margins),
        ),
        speakers,
    )
