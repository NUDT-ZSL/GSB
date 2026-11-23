"""Reference timbre embedding estimates, content hashes, and drift detection."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from .errors import AppError
from .features import extract_pcm_features
from .model import (
    Dataset,
    EmbeddingSummary,
    ReferenceClip,
    ReferenceRegistryState,
    Utterance,
)
from .precision import NumericOps
from .util import canonical_json, digest_payload, frame_intervals, stable_round


def estimate_timbre_embedding(
    pcm: np.ndarray,
    sample_rate: int,
    ops: NumericOps,
    frame_ms: float = 20.0,
    frame_hop_ms: float = 10.0,
) -> tuple[tuple[float, ...], tuple[float, ...], int]:
    frames = extract_pcm_features(
        pcm=pcm,
        sample_rate=sample_rate,
        frame_ms=frame_ms,
        frame_hop_ms=frame_hop_ms,
        ops=ops,
        utterance_id="embedding-source",
        speaker_id=0,
    )
    if not frames:
        vector = tuple([0.0] * 12)
        return vector, vector, 0
    matrix = np.asarray([frame.timbre_vector for frame in frames], dtype=np.float64)
    mean = np.mean(matrix, axis=0)
    std_selected = np.std(matrix[:, :4], axis=0)
    vector_np = np.concatenate([mean, std_selected]).astype(np.float64)
    vector_np = ops.cast(vector_np)
    normalized = ops.normalize(vector_np)
    return (
        tuple(float(v) for v in vector_np),
        tuple(float(v) for v in normalized),
        len(frames),
    )


def _reference_version_rows(clips: list[ReferenceClip]) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "ref_id": clip.ref_id,
            "version": clip.version,
            "content_hash": clip.content_hash,
            "clone_seed": clip.clone_seed,
            "shape": list(clip.shape),
        }
        for clip in sorted(clips, key=lambda item: (item.ref_id, item.version))
    )


def _reference_content_hash(clips: list[ReferenceClip]) -> str:
    return digest_payload(_reference_version_rows(clips))


def compute_embedding_drift(
    old_vector: tuple[float, ...] | list[float],
    old_normalized: tuple[float, ...] | list[float],
    new_vector: tuple[float, ...] | list[float],
    new_normalized: tuple[float, ...] | list[float],
    old_ref_versions: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    new_ref_versions: tuple[dict[str, Any], ...] | list[dict[str, Any]],
) -> dict[str, Any]:
    old_v = np.asarray(old_vector, dtype=np.float64)
    new_v = np.asarray(new_vector, dtype=np.float64)
    old_n = np.asarray(old_normalized, dtype=np.float64)
    new_n = np.asarray(new_normalized, dtype=np.float64)
    if old_v.shape != new_v.shape or old_n.shape != new_n.shape:
        raise AppError(
            "E_INCONSISTENT",
            "Old and new embedding dimensions differ.",
            stage="embedding.drift",
            field="embedding_dimension",
            details={"old_shape": list(old_v.shape), "new_shape": list(new_v.shape)},
        )
    delta = new_v - old_v
    normalized_delta = new_n - old_n
    l2 = math.sqrt(float(np.dot(delta, delta)))
    normalized_l2 = math.sqrt(float(np.dot(normalized_delta, normalized_delta)))
    old_norm = math.sqrt(float(np.dot(old_n, old_n)))
    relative = normalized_l2 / max(old_norm, 1e-12)
    cosine = float(np.dot(old_n, new_n) / max(old_norm * math.sqrt(float(np.dot(new_n, new_n))), 1e-12))
    payload = {
        "formula_version": "embedding-drift-v1",
        "old_vector": [float(v) for v in old_v],
        "new_vector": [float(v) for v in new_v],
        "old_normalized": [float(v) for v in old_n],
        "new_normalized": [float(v) for v in new_n],
        "old_ref_versions": list(old_ref_versions),
        "new_ref_versions": list(new_ref_versions),
        "drift_l2": stable_round(l2, 12),
        "drift_normalized_l2": stable_round(normalized_l2, 12),
        "drift_relative": stable_round(relative, 12),
        "cosine_distance": stable_round(1.0 - cosine, 12),
    }
    payload["recompute_digest"] = digest_payload(payload)
    return payload


@dataclass(frozen=True)
class RegistrationResult:
    changed_speakers: tuple[int, ...]
    unchanged_speakers: tuple[int, ...]
    drifts: tuple[dict[str, Any], ...]
    affected_frames: tuple[int, ...]
    recomputed_frames: tuple[int, ...]
    reused_frames: tuple[int, ...]
    affected_intervals: tuple[dict[str, Any], ...]
    embedding_component_version: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "changed_speakers": list(self.changed_speakers),
            "unchanged_speakers": list(self.unchanged_speakers),
            "drifts": list(self.drifts),
            "affected_frames": list(self.affected_frames),
            "recomputed_frames": list(self.recomputed_frames),
            "reused_frames": list(self.reused_frames),
            "affected_intervals": list(self.affected_intervals),
            "embedding_component_version": self.embedding_component_version,
        }


class ReferenceRegistry:
    def __init__(
        self,
        dataset: Dataset,
        state: ReferenceRegistryState | None = None,
    ) -> None:
        self.dataset = dataset
        if state is not None:
            self.state = state
            return
        state = ReferenceRegistryState()
        self.state = state
        display = NumericOps("float64")
        for clip in dataset.references.values():
            state.clips.setdefault(clip.speaker_id, []).append(clip)
        for speaker_id in sorted(state.clips):
            state.embeddings[speaker_id] = self._compute_summary(
                speaker_id, embedding_version=1, ops=display
            )

    def _compute_summary(
        self,
        speaker_id: int,
        embedding_version: int,
        ops: NumericOps,
    ) -> EmbeddingSummary:
        clips = sorted(
            self.state.clips.get(speaker_id, []),
            key=lambda item: (item.ref_id, item.version),
        )
        if not clips:
            raise AppError(
                "E_REF_MISSING",
                f"No reference clip is registered for speaker {speaker_id}.",
                stage="embedding.reference",
                field="ref_id",
                details={"speaker_id": speaker_id},
            )
        weighted_vector = np.zeros(12, dtype=np.float64)
        total_weight = 0
        for clip in clips:
            vector, _normalized, frame_count = estimate_timbre_embedding(
                clip.pcm, clip.sample_rate, ops
            )
            weight = max(1, frame_count)
            weighted_vector += np.asarray(vector, dtype=np.float64) * weight
            total_weight += weight
        vector = weighted_vector / max(1, total_weight)
        normalized = ops.normalize(vector)
        version_rows = _reference_version_rows(clips)
        ref_hash = _reference_content_hash(clips)
        digest = digest_payload(
            {
                "speaker_id": speaker_id,
                "vector": [float(v) for v in vector],
                "normalized": [float(v) for v in normalized],
                "reference_versions": version_rows,
                "embedding_version": embedding_version,
            }
        )
        payload = {
            "speaker_id": speaker_id,
            "vector": [stable_round(float(v)) for v in vector],
            "normalized": [stable_round(float(v)) for v in normalized],
            "reference_versions": list(version_rows),
            "embedding_version": embedding_version,
            "embedding_digest": digest,
            "reference_content_hash": ref_hash,
        }
        return EmbeddingSummary(
            speaker_id=speaker_id,
            vector=tuple(float(v) for v in vector),
            normalized=tuple(float(v) for v in normalized),
            reference_versions=version_rows,
            embedding_version=embedding_version,
            embedding_digest=digest,
            reference_content_hash=ref_hash,
            summary_bytes=canonical_json(payload),
        )

    def all_summaries(self) -> dict[int, EmbeddingSummary]:
        return dict(self.state.embeddings)

    def embedding_for_profile(
        self, speaker_id: int, ops: NumericOps
    ) -> tuple[float, ...]:
        summary = self.state.embeddings.get(speaker_id)
        if summary is None:
            raise AppError(
                "E_REF_MISSING",
                f"Embedding for speaker {speaker_id} is unavailable.",
                stage="embedding.lookup",
                field="speaker_id",
                details={"speaker_id": speaker_id},
            )
        vector = ops.cast(summary.vector)
        normalized = ops.normalize(vector)
        return tuple(float(v) for v in normalized)

    def _frames_for_speakers(
        self, speaker_ids: set[int]
    ) -> tuple[int, ...]:
        frames: list[int] = []
        offset = 0
        for utterance in self.dataset.utterances:
            if utterance.speaker_id in speaker_ids:
                frames.extend(range(offset, offset + len(utterance.truth)))
            offset += len(utterance.truth)
        return tuple(sorted(frames))

    def _all_frames(self) -> tuple[int, ...]:
        return tuple(range(self.dataset.total_frames))

    def _affected_intervals(
        self, speaker_ids: set[int]
    ) -> tuple[dict[str, Any], ...]:
        intervals: list[dict[str, Any]] = []
        for utterance in sorted(self.dataset.utterances, key=lambda item: item.utterance_id):
            if utterance.speaker_id not in speaker_ids:
                continue
            indices = [frame.frame_index for frame in utterance.truth]
            intervals.append(
                {
                    "utterance_id": utterance.utterance_id,
                    "speaker_id": utterance.speaker_id,
                    "start_frame": min(indices),
                    "end_frame": max(indices),
                    "frame_count": len(indices),
                }
            )
        return tuple(intervals)

    def append_clip(
        self,
        clip: ReferenceClip,
        expected_seed: int | None = None,
        expected_shape: tuple[int, ...] | None = None,
    ) -> RegistrationResult:
        if clip.sample_rate != 16000:
            raise AppError(
                "E_SAMPLE_RATE",
                "Reference clip sample rate must be 16000 Hz.",
                stage="registration.validate",
                field="sample_rate",
                details={"sample_rate": clip.sample_rate, "required": 16000},
            )
        if not clip.ref_id:
            raise AppError(
                "E_REF_MISSING",
                "ref_id is required for registration.",
                stage="registration.validate",
                field="ref_id",
                details={"speaker_id": clip.speaker_id},
            )
        if clip.speaker_id not in self.dataset.speakers:
            raise AppError(
                "E_INPUT_INVALID",
                f"Unknown speaker_id {clip.speaker_id}.",
                stage="registration.validate",
                field="speaker_id",
            )
        if expected_seed is not None and clip.clone_seed != expected_seed:
            raise AppError(
                "E_SEED_SHAPE",
                "Reference seed does not match the declared registration seed.",
                stage="registration.validate",
                field="clone_seed",
                details={"expected": expected_seed, "actual": clip.clone_seed},
            )
        if expected_shape is not None and tuple(clip.shape) != tuple(expected_shape):
            raise AppError(
                "E_SEED_SHAPE",
                "Reference clip shape does not match the declared shape.",
                stage="registration.validate",
                field="shape",
                details={
                    "expected": list(expected_shape),
                    "actual": list(clip.shape),
                },
            )

        existing = self.state.clips.get(clip.speaker_id, [])
        hash_exists = any(item.content_hash == clip.content_hash for item in existing)
        if hash_exists:
            reused = self._all_frames()
            return RegistrationResult(
                changed_speakers=(),
                unchanged_speakers=tuple(self.dataset.speakers),
                drifts=(),
                affected_frames=reused,
                recomputed_frames=(),
                reused_frames=reused,
                affected_intervals=(),
                embedding_component_version=self.state.embedding_version,
            )

        old_summary = self.state.embeddings.get(clip.speaker_id)
        if old_summary is None:
            raise AppError(
                "E_REF_MISSING",
                "No cached embedding exists for the speaker.",
                stage="registration.embed",
                field="ref_id",
                details={"speaker_id": clip.speaker_id},
            )
        replaced = False
        for index, item in enumerate(existing):
            if item.ref_id == clip.ref_id:
                existing[index] = clip
                replaced = True
                break
        if not replaced:
            existing.append(clip)
        self.state.clips[clip.speaker_id] = existing
        new_version = old_summary.embedding_version + 1
        new_summary = self._compute_summary(
            clip.speaker_id,
            embedding_version=new_version,
            ops=NumericOps("float64"),
        )
        self.state.embeddings[clip.speaker_id] = new_summary
        self.state.embedding_version += 1
        drift = compute_embedding_drift(
            old_summary.vector,
            old_summary.normalized,
            new_summary.vector,
            new_summary.normalized,
            old_summary.reference_versions,
            new_summary.reference_versions,
        )
        drift = {
            "speaker_id": clip.speaker_id,
            **drift,
            "old_embedding_version": old_summary.embedding_version,
            "new_embedding_version": new_summary.embedding_version,
        }
        recomputed = self._frames_for_speakers({clip.speaker_id})
        unchanged_speakers = tuple(
            speaker_id
            for speaker_id in self.dataset.speakers
            if speaker_id != clip.speaker_id
        )
        reused = self._frames_for_speakers(set(unchanged_speakers))
        affected = tuple(sorted(set(recomputed) | set(reused)))
        return RegistrationResult(
            changed_speakers=(clip.speaker_id,),
            unchanged_speakers=unchanged_speakers,
            drifts=(drift,),
            affected_frames=affected,
            recomputed_frames=recomputed,
            reused_frames=reused,
            affected_intervals=self._affected_intervals({clip.speaker_id}),
            embedding_component_version=self.state.embedding_version,
        )

    def summary_payload(self) -> dict[str, Any]:
        return {
            "embedding_component_version": self.state.embedding_version,
            "speakers": {
                str(speaker_id): summary.to_dict()
                for speaker_id, summary in sorted(self.state.embeddings.items())
            },
        }

    def reference_summary(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for speaker_id in sorted(self.state.clips):
            for clip in sorted(
                self.state.clips[speaker_id], key=lambda item: (item.ref_id, item.version)
            ):
                rows.append(
                    {
                        "speaker_id": speaker_id,
                        "ref_id": clip.ref_id,
                        "version": clip.version,
                        "clone_seed": clip.clone_seed,
                        "sample_rate": clip.sample_rate,
                        "shape": list(clip.shape),
                        "content_hash": clip.content_hash,
                        "frame_intervals": frame_intervals(
                            range(
                                0,
                                (clip.pcm.size - 160) // 160 + 1,
                            )
                        ),
                    }
                )
        return rows


def replaced_frame_guard(
    result: RegistrationResult,
    all_frames: tuple[int, ...],
) -> dict[str, Any]:
    recomputed = set(result.recomputed_frames)
    reused = set(result.reused_frames)
    overlap = sorted(recomputed & reused)
    union = recomputed | reused
    missing = sorted(set(all_frames) - union)
    extra = sorted(union - set(all_frames))
    return {
        "overlap": overlap,
        "missing": missing,
        "extra": extra,
        "disjoint": not overlap,
        "covers_all": not missing and not extra,
    }
