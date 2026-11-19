"""Reference timbre statistics and deterministic phonetic-unit alignment."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np

from .deterministic import SplitMix64, sha256_json
from .features import SPECTRAL_BANDS_HZ, FeatureTable


EMBEDDING_DIMENSION = 19
ALIGNMENT_DIMENSION = 8

EMBEDDING_COMPONENTS = (
    "mean_log_f0",
    "std_log_f0",
    "mean_log_energy",
    "std_log_energy",
    "mean_zcr",
    "std_zcr",
    "mean_autocorr_peak",
    "std_autocorr_peak",
    "mean_spectral_centroid_normalized",
    "std_spectral_centroid_normalized",
    "mean_spectral_flatness",
    "mean_spectral_bandwidth_normalized",
    *(
        f"mean_band_{int(low)}_to_{int(high)}_log_energy_ratio"
        for low, high in SPECTRAL_BANDS_HZ
    ),
)

ALIGNMENT_COMPONENTS = (
    "f0_position",
    "log_energy_position",
    "zcr",
    "autocorr_peak",
    "spectral_centroid_normalized",
    "spectral_bandwidth_normalized",
    "spectral_flatness",
    "rms_position",
)

STATISTICS_SPEC: dict[str, Any] = {
    "embedding_dimension": EMBEDDING_DIMENSION,
    "alignment_dimension": ALIGNMENT_DIMENSION,
    "components": list(EMBEDDING_COMPONENTS),
    "alignment_components": list(ALIGNMENT_COMPONENTS),
    "std_ddof": 0,
    "f0_policy": "only frames with positive autocorrelation f0 candidate",
    "energy_epsilon": 1e-15,
    "normalization": "l2 with zero-vector preservation",
    "centroid_normalization": "Hz divided by Nyquist frequency",
    "bandwidth_normalization": "Hz divided by Nyquist frequency",
    "spectral_bands_hz": [[float(low), float(high)] for low, high in SPECTRAL_BANDS_HZ],
}

TIMBRE_SIMILARITY_SCALES = (
    0.20,
    0.10,
    0.60,
    0.60,
    0.05,
    0.05,
    0.05,
    0.05,
    0.03,
    0.03,
    0.03,
    0.03,
    0.70,
    0.70,
    0.70,
    0.70,
    0.70,
    0.70,
    0.70,
)

TIMBRE_SIMILARITY_WEIGHTS = (
    3.0,
    0.7,
    0.4,
    0.4,
    1.0,
    1.0,
    0.6,
    0.6,
    1.0,
    1.0,
    1.0,
    1.0,
    1.2,
    1.2,
    1.2,
    1.2,
    1.2,
    1.2,
    1.2,
)

PHONEME_PROTOTYPES_8: tuple[tuple[float, ...], ...] = (
    (0.62, 0.30, -0.28, 0.24, -0.18, -0.10, -0.42, 0.28),
    (0.34, 0.18, 0.05, 0.30, 0.18, 0.12, -0.35, 0.20),
    (-0.05, 0.02, 0.32, 0.36, 0.55, 0.28, -0.15, 0.12),
    (-0.28, 0.20, -0.15, 0.18, -0.35, -0.22, -0.40, 0.22),
    (-0.55, 0.15, -0.36, 0.12, -0.52, -0.40, -0.45, 0.18),
    (0.10, -0.18, -0.40, -0.15, -0.30, -0.25, -0.30, -0.20),
    (0.22, -0.55, 0.72, -0.65, 0.60, 0.48, 0.70, -0.58),
    (0.05, -0.48, 0.56, -0.55, 0.38, 0.36, 0.52, -0.45),
)


@dataclass(frozen=True)
class TimbreEstimate:
    owner_id: str
    speaker_id: int
    frame_count: int
    raw_embedding: np.ndarray
    embedding: np.ndarray
    embedding_norm: float
    alignment_base: np.ndarray
    voiced_frame_count: int

    @property
    def dimension(self) -> int:
        return int(self.embedding.shape[0])

    def to_dict(self, *, include_arrays: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "owner_id": self.owner_id,
            "speaker_id": self.speaker_id,
            "frame_count": self.frame_count,
            "voiced_frame_count": self.voiced_frame_count,
            "embedding_dimension": self.dimension,
            "embedding_norm": self.embedding_norm,
            "statistics_spec": STATISTICS_SPEC,
            "embedding_checksum": sha256_json([float(value) for value in self.embedding]),
        }
        if include_arrays:
            result["raw_embedding"] = [float(value) for value in self.raw_embedding]
            result["embedding"] = [float(value) for value in self.embedding]
            result["alignment_base"] = [float(value) for value in self.alignment_base]
        return result

    def snapshot_payload(self) -> dict[str, Any]:
        return self.to_dict(include_arrays=True)


@dataclass(frozen=True)
class CandidateUnit:
    speaker_id: int
    phoneme_id: int

    def sort_key(self) -> tuple[int, int]:
        return (self.speaker_id, self.phoneme_id)


@dataclass(frozen=True)
class UnitAssignment:
    frame_index: int
    speaker_id: int
    phoneme_id: int
    score: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "frame_index": self.frame_index,
            "speaker_id": self.speaker_id,
            "phoneme_id": self.phoneme_id,
            "score": float(self.score),
        }


@dataclass(frozen=True)
class AlignmentResult:
    assignments: tuple[UnitAssignment, ...]
    candidates: tuple[CandidateUnit, ...]
    assignment_digest: str
    metadata: dict[str, Any]


def _normalize(vector: np.ndarray, epsilon: float = 1e-12) -> tuple[np.ndarray, float]:
    norm = float(np.sqrt(np.sum(vector * vector)))
    if norm <= epsilon:
        return np.zeros_like(vector, dtype=np.float64), norm
    return vector / norm, norm


def _safe_std(values: np.ndarray) -> float:
    if values.size == 0:
        return 0.0
    return float(np.std(values, ddof=0))


def frame_alignment_vectors(features: FeatureTable) -> np.ndarray:
    sample_rate = float(features.sample_rate)
    f0 = np.asarray(features.f0_candidate, dtype=np.float64)
    valid_f0 = f0[f0 > 0]
    fallback_f0 = float(np.median(valid_f0)) if valid_f0.size else 90.0
    f0_used = np.where(f0 > 0, f0, fallback_f0)
    f0_position = 2.0 * (
        (np.log(f0_used) - math.log(70.0)) / (math.log(400.0) - math.log(70.0))
    ) - 1.0
    log_energy = np.log(np.asarray(features.energy, dtype=np.float64) + 1e-15)
    log_energy_position = np.tanh((log_energy + 7.0) / 3.2)
    zcr = np.clip(np.asarray(features.zcr, dtype=np.float64), -1.0, 1.0)
    peak = np.clip(np.asarray(features.autocorr_peak, dtype=np.float64), -1.0, 1.0)
    centroid = np.asarray(features.spectral_centroid_hz, dtype=np.float64) / (sample_rate / 2.0)
    bandwidth = np.asarray(features.spectral_bandwidth_hz, dtype=np.float64) / (
        sample_rate / 2.0
    )
    flatness = np.asarray(features.spectral_flatness, dtype=np.float64)
    rms_position = np.tanh(np.asarray(features.rms, dtype=np.float64) * 11.0)
    vectors = np.stack(
        [
            np.clip(f0_position, -1.0, 1.0),
            log_energy_position,
            zcr,
            peak,
            np.clip(centroid, 0.0, 1.0),
            np.clip(bandwidth, 0.0, 1.0),
            np.clip(flatness, 0.0, 1.0),
            rms_position,
        ],
        axis=1,
    )
    return np.asarray(vectors, dtype=np.float64)


def estimate_timbre(
    features: FeatureTable,
    *,
    owner_id: str,
    speaker_id: int,
) -> TimbreEstimate:
    sample_rate = float(features.sample_rate)
    f0 = np.asarray(features.f0_candidate, dtype=np.float64)
    voiced = f0[f0 > 0]
    if voiced.size:
        log_f0 = np.log(voiced)
        mean_log_f0 = float(np.mean(log_f0))
        std_log_f0 = _safe_std(log_f0)
    else:
        mean_log_f0 = 0.0
        std_log_f0 = 0.0
    log_energy = np.log(np.asarray(features.energy, dtype=np.float64) + 1e-15)
    centroid = np.asarray(features.spectral_centroid_hz, dtype=np.float64) / (
        sample_rate / 2.0
    )
    bandwidth = np.asarray(features.spectral_bandwidth_hz, dtype=np.float64) / (
        sample_rate / 2.0
    )
    raw = np.asarray(
        [
            mean_log_f0,
            std_log_f0,
            float(np.mean(log_energy)),
            _safe_std(log_energy),
            float(np.mean(features.zcr)),
            _safe_std(features.zcr),
            float(np.mean(features.autocorr_peak)),
            _safe_std(features.autocorr_peak),
            float(np.mean(centroid)),
            _safe_std(centroid),
            float(np.mean(features.spectral_flatness)),
            float(np.mean(bandwidth)),
            *[
                float(np.mean(features.band_log_energy_ratio[:, band_index]))
                for band_index in range(len(SPECTRAL_BANDS_HZ))
            ],
        ],
        dtype=np.float64,
    )
    embedding, norm = _normalize(raw)
    f0_position = 2.0 * (
        (mean_log_f0 - math.log(70.0)) / (math.log(400.0) - math.log(70.0))
    ) - 1.0
    alignment_base = np.asarray(
        [
            float(np.clip(f0_position, -1.0, 1.0)),
            float(np.tanh((float(np.mean(log_energy)) + 7.0) / 3.2)),
            float(np.clip(np.mean(features.zcr), 0.0, 1.0)),
            float(np.clip(np.mean(features.autocorr_peak), 0.0, 1.0)),
            float(np.clip(np.mean(centroid), 0.0, 1.0)),
            float(np.clip(np.mean(bandwidth), 0.0, 1.0)),
            float(np.clip(np.mean(features.spectral_flatness), 0.0, 1.0)),
            float(np.tanh(np.mean(features.rms) * 11.0)),
        ],
        dtype=np.float64,
    )
    return TimbreEstimate(
        owner_id=owner_id,
        speaker_id=speaker_id,
        frame_count=features.frame_count,
        raw_embedding=raw,
        embedding=embedding,
        embedding_norm=norm,
        alignment_base=alignment_base,
        voiced_frame_count=int(voiced.size),
    )


def timbre_distance_similarity(
    left_raw: np.ndarray,
    right_raw: np.ndarray,
) -> tuple[float, float]:
    """Scale-aware exponential similarity; returns (similarity, distance)."""

    left = np.asarray(left_raw, dtype=np.float64)
    right = np.asarray(right_raw, dtype=np.float64)
    if left.shape != right.shape:
        raise ValueError("timbre embeddings must have equal dimensions")
    if left.shape[0] != EMBEDDING_DIMENSION:
        raise ValueError(
            f"expected {EMBEDDING_DIMENSION} timbre dimensions, found {left.shape[0]}"
        )
    scales = np.asarray(TIMBRE_SIMILARITY_SCALES, dtype=np.float64)
    weights = np.asarray(TIMBRE_SIMILARITY_WEIGHTS, dtype=np.float64)
    normalized = (left - right) / scales
    distance = float(np.sqrt(np.sum(weights * normalized * normalized)))
    similarity = float(math.exp(-distance / 8.0))
    return similarity, distance


def phoneme_prototypes(num_phonemes: int) -> np.ndarray:
    if num_phonemes <= 0:
        raise ValueError("num_phonemes must be positive")
    rows: list[np.ndarray] = [
        np.asarray(row, dtype=np.float64) for row in PHONEME_PROTOTYPES_8
    ]
    if num_phonemes > len(rows):
        rng = SplitMix64(0x50484F4E454D45)
        while len(rows) < num_phonemes:
            rows.append(
                np.asarray(
                    [rng.uniform_range(-0.72, 0.72) for _ in range(ALIGNMENT_DIMENSION)],
                    dtype=np.float64,
                )
            )
    return np.stack(rows[:num_phonemes], axis=0)


def candidate_catalog_and_bases(
    estimates: Iterable[TimbreEstimate],
    num_phonemes: int,
) -> tuple[tuple[CandidateUnit, ...], np.ndarray]:
    ordered = sorted(estimates, key=lambda estimate: estimate.speaker_id)
    prototypes = phoneme_prototypes(num_phonemes)
    catalog: list[CandidateUnit] = []
    vectors: list[np.ndarray] = []
    for estimate in ordered:
        for phoneme_id in range(num_phonemes):
            catalog.append(CandidateUnit(estimate.speaker_id, phoneme_id))
            vectors.append(estimate.alignment_base + 0.65 * prototypes[phoneme_id])
    return tuple(catalog), np.asarray(vectors, dtype=np.float64)


def cosine_rows(left: np.ndarray, right: np.ndarray, epsilon: float = 1e-12) -> np.ndarray:
    left_array = np.asarray(left, dtype=np.float64)
    right_array = np.asarray(right, dtype=np.float64)
    numerator = np.sum(left_array * right_array, axis=1)
    denominator = np.sqrt(
        np.maximum(np.sum(left_array * left_array, axis=1), 0.0)
        * np.maximum(np.sum(right_array * right_array, axis=1), 0.0)
    )
    return np.where(denominator > epsilon, numerator / denominator, 0.0)


def select_best_index(
    scores: np.ndarray,
    candidates: tuple[CandidateUnit, ...],
) -> int:
    """Select argmax with explicit (score desc, speaker asc, phoneme asc) order."""

    if scores.shape[0] != len(candidates):
        raise ValueError("score and candidate counts differ")
    best_index = 0
    best_score = -math.inf
    for index, candidate in enumerate(candidates):
        score = float(scores[index])
        if not math.isfinite(score):
            score = -math.inf
        if score > best_score:
            best_index = index
            best_score = score
        elif score == best_score:
            current = candidates[best_index]
            if candidate.sort_key() < current.sort_key():
                best_index = index
    return best_index


def align_units(
    frame_vectors: np.ndarray,
    candidate_vectors: np.ndarray,
    candidates: tuple[CandidateUnit, ...],
    *,
    stage_name: str = "log_transform",
) -> AlignmentResult:
    if frame_vectors.ndim != 2 or candidate_vectors.ndim != 2:
        raise ValueError("alignment vectors must be two-dimensional")
    assignments: list[UnitAssignment] = []
    for frame_index in range(frame_vectors.shape[0]):
        scores = cosine_rows(
            np.repeat(frame_vectors[frame_index : frame_index + 1], len(candidates), axis=0),
            candidate_vectors,
        )
        best = select_best_index(scores, candidates)
        selected = candidates[best]
        assignments.append(
            UnitAssignment(
                frame_index=frame_index,
                speaker_id=selected.speaker_id,
                phoneme_id=selected.phoneme_id,
                score=float(scores[best]),
            )
        )
    digest = sha256_json([assignment.to_dict() for assignment in assignments])
    metadata = {
        "alignment_dimension": ALIGNMENT_DIMENSION,
        "embedding_dimension": EMBEDDING_DIMENSION,
        "candidate_count": len(candidates),
        "candidate_order": "speaker_id ascending, then phoneme_id ascending",
        "tie_break": "speaker_id ascending, then phoneme_id ascending",
        "score": "cosine similarity",
        "assignment_source_stage": stage_name,
    }
    return AlignmentResult(
        assignments=tuple(assignments),
        candidates=candidates,
        assignment_digest=digest,
        metadata=metadata,
    )


def assignment_identity(alignment: AlignmentResult) -> list[tuple[int, int, int]]:
    return [
        (assignment.frame_index, assignment.speaker_id, assignment.phoneme_id)
        for assignment in alignment.assignments
    ]
