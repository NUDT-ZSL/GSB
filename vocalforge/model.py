"""Typed domain objects for deterministic speech samples and diagnostics."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass(frozen=True)
class FrameTruth:
    frame_index: int
    speaker_id: int
    f0_true: float
    energy_true: float
    phoneme_id: int

    def to_dict(self, utterance_id: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "frame_index": self.frame_index,
            "speaker_id": self.speaker_id,
            "f0_true": round(self.f0_true, 9),
            "energy_true": round(self.energy_true, 12),
            "phoneme_id": self.phoneme_id,
        }
        if utterance_id is not None:
            payload["utterance_id"] = utterance_id
        return payload


@dataclass(frozen=True)
class Utterance:
    utterance_id: str
    speaker_id: int
    ref_id: str
    sample_rate: int
    duration_ms: float
    phoneme_count: int
    clone_seed: int
    pcm: np.ndarray
    truth: tuple[FrameTruth, ...]
    timeline: tuple[int, ...]

    def manifest_row(self) -> dict[str, Any]:
        return {
            "utterance_id": self.utterance_id,
            "speaker_id": self.speaker_id,
            "ref_id": self.ref_id,
            "sample_rate": self.sample_rate,
            "duration_ms": round(self.duration_ms, 6),
            "phoneme_count": self.phoneme_count,
            "clone_seed": self.clone_seed,
            "pcm_path": f"pcm/{self.utterance_id}.pcm",
        }


@dataclass(frozen=True)
class ReferenceClip:
    ref_id: str
    speaker_id: int
    version: int
    clone_seed: int
    sample_rate: int
    pcm: np.ndarray
    content_hash: str
    shape: tuple[int, ...]


@dataclass(frozen=True)
class Dataset:
    seed: int
    sample_rate: int
    frame_ms: float
    frame_hop_ms: float
    controlled_phonemes: tuple[int, ...]
    speakers: tuple[int, ...]
    utterances: tuple[Utterance, ...]
    references: dict[str, ReferenceClip]
    phoneme_prototypes: dict[int, dict[str, Any]]

    @property
    def total_frames(self) -> int:
        return sum(len(utt.truth) for utt in self.utterances)

    def utterance(self, utterance_id: str) -> Utterance:
        for utt in self.utterances:
            if utt.utterance_id == utterance_id:
                return utt
        raise KeyError(utterance_id)

    def truth_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for utt in self.utterances:
            rows.extend(truth.to_dict(utt.utterance_id) for truth in utt.truth)
        return rows


@dataclass(frozen=True)
class PrecisionConfig:
    profile: str = "float64"
    fixed_scale: int = 1_000_000
    rounding_mode: str = "nearest_even"
    decision_quantum: float = 0.00001
    subnormal_floor: float = 1e-12
    threshold: float = 0.985
    top_k: int = 5
    comparison_strategy: str = "tolerance_band"
    tolerance: float = 0.00002
    path_similarity_tolerance: float = 0.01
    max_alignment_offset: int = 3
    min_similarity_agreement: float = 0.80

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "fixed_scale": self.fixed_scale,
            "rounding_mode": self.rounding_mode,
            "decision_quantum": self.decision_quantum,
            "subnormal_floor": self.subnormal_floor,
            "threshold": self.threshold,
            "top_k": self.top_k,
            "comparison_strategy": self.comparison_strategy,
            "tolerance": self.tolerance,
            "path_similarity_tolerance": self.path_similarity_tolerance,
            "max_alignment_offset": self.max_alignment_offset,
            "min_similarity_agreement": self.min_similarity_agreement,
        }


@dataclass(frozen=True)
class FeatureFrame:
    utterance_id: str
    speaker_id: int
    frame_index: int
    start_sample: int
    end_sample: int
    energy: float
    zcr: float
    f0_candidate: float
    log_energy: float
    centroid_hz: float
    band_energies: tuple[float, ...]
    timbre_vector: tuple[float, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "utterance_id": self.utterance_id,
            "speaker_id": self.speaker_id,
            "frame_index": self.frame_index,
            "start_sample": self.start_sample,
            "end_sample": self.end_sample,
            "energy": round(self.energy, 12),
            "zcr": round(self.zcr, 12),
            "f0_candidate": round(self.f0_candidate, 9),
            "log_energy": round(self.log_energy, 12),
            "centroid_hz": round(self.centroid_hz, 6),
            "band_energies": [round(v, 12) for v in self.band_energies],
            "timbre_vector": [round(v, 12) for v in self.timbre_vector],
        }


@dataclass(frozen=True)
class AlignmentFrame:
    utterance_id: str
    speaker_id: int
    frame_index: int
    phoneme_id: int
    score: float
    runner_up: int
    margin: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "utterance_id": self.utterance_id,
            "speaker_id": self.speaker_id,
            "frame_index": self.frame_index,
            "phoneme_id": self.phoneme_id,
            "score": round(self.score, 12),
            "runner_up": self.runner_up,
            "margin": round(self.margin, 12),
        }


@dataclass(frozen=True)
class EmbeddingSummary:
    speaker_id: int
    vector: tuple[float, ...]
    normalized: tuple[float, ...]
    reference_versions: tuple[dict[str, Any], ...]
    embedding_version: int
    embedding_digest: str
    reference_content_hash: str
    summary_bytes: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "speaker_id": self.speaker_id,
            "vector": [round(v, 12) for v in self.vector],
            "normalized": [round(v, 12) for v in self.normalized],
            "reference_versions": list(self.reference_versions),
            "embedding_version": self.embedding_version,
            "embedding_digest": self.embedding_digest,
            "reference_content_hash": self.reference_content_hash,
            "summary_bytes": self.summary_bytes,
        }


@dataclass
class ReferenceRegistryState:
    clips: dict[int, list[ReferenceClip]] = field(default_factory=dict)
    embeddings: dict[int, EmbeddingSummary] = field(default_factory=dict)
    embedding_version: int = 1


@dataclass(frozen=True)
class ProfileResult:
    profile: str
    frames_by_utterance: dict[str, tuple[FeatureFrame, ...]]
    alignment: dict[str, tuple[AlignmentFrame, ...]]
    embedding_vectors: dict[int, tuple[float, ...]]
    clone_scores: dict[str, float]
    feature_digest: str
    alignment_digest: str
    score_digest: str
    fixed_trace: tuple[dict[str, Any], ...] = ()
