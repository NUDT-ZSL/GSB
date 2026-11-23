"""Deterministic pronunciation-unit alignment with exact and approximate paths."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from .errors import AppError
from .model import AlignmentFrame, FeatureFrame, FrameTruth
from .util import digest_payload, stable_round


def _prototype_signature(prototype: dict[str, Any]) -> tuple[float, ...]:
    f1, f2, f3 = (float(v) for v in prototype["formants_hz"])
    amplitude = float(prototype["amplitude"])
    noise = float(prototype["noise"])
    energy = math.log1p(0.03 * amplitude * amplitude)
    zcr = min(0.48, noise * 0.72 + (0.12 if f2 > 3000.0 else 0.0))
    f0_term = math.log1p(145.0) / 8.0
    centroid = (0.2 * f1 + 0.52 * f2 + 0.28 * f3) / 4000.0
    bands = np.asarray(
        [
            math.exp(-0.5 * ((f1 - 270.0) / 260.0) ** 2),
            math.exp(-0.5 * ((f2 - 950.0) / 620.0) ** 2),
            math.exp(-0.5 * ((f2 - 2050.0) / 950.0) ** 2),
            math.exp(-0.5 * ((f3 - 3400.0) / 1100.0) ** 2),
        ],
        dtype=np.float64,
    )
    bands = bands / max(float(np.sum(bands)), 1e-12)
    return (energy, zcr, f0_term, centroid, *[float(v) for v in bands])


def _score_phoneme(
    frame_vector: tuple[float, ...],
    prototype_vector: tuple[float, ...],
) -> float:
    frame = np.asarray(frame_vector, dtype=np.float64)
    proto = np.asarray(prototype_vector, dtype=np.float64)
    frame_norm = math.sqrt(float(np.dot(frame, frame)))
    proto_norm = math.sqrt(float(np.dot(proto, proto)))
    if frame_norm <= 1e-15 or proto_norm <= 1e-15:
        return 0.0
    cosine = float(np.dot(frame, proto) / (frame_norm * proto_norm))
    return stable_round(0.5 * (cosine + 1.0), 12)


def _rank_candidates(
    frame: FeatureFrame,
    phoneme_ids: tuple[int, ...],
    prototypes: dict[int, dict[str, Any]],
    allowed: tuple[int, ...] | None = None,
) -> list[tuple[float, int]]:
    candidate_ids = allowed if allowed is not None else phoneme_ids
    ranked = [
        (
            _score_phoneme(
                frame.timbre_vector,
                _prototype_signature(prototypes[phoneme_id]),
            ),
            int(phoneme_id),
        )
        for phoneme_id in candidate_ids
    ]
    ranked.sort(key=lambda item: (-item[0], frame.speaker_id, item[1]))
    return ranked


def _validate_label(
    utterance_id: str,
    frame_index: int,
    phoneme_id: int,
    controlled: tuple[int, ...],
    stage: str,
) -> None:
    if phoneme_id not in controlled:
        raise AppError(
            "E_PHONEME_OUT_OF_RANGE",
            "Phoneme label is outside the controlled phoneme set.",
            stage=stage,
            field="phoneme_id",
            details={
                "utterance_id": utterance_id,
                "frame_index": frame_index,
                "phoneme_id": phoneme_id,
                "controlled_phonemes": list(controlled),
            },
        )


def validate_truth_phonemes(
    truth_by_utterance: dict[str, tuple[FrameTruth, ...]],
    controlled_phonemes: tuple[int, ...],
    stage: str = "alignment.validate_truth",
) -> None:
    controlled = set(controlled_phonemes)
    for utterance_id in sorted(truth_by_utterance):
        for frame in truth_by_utterance[utterance_id]:
            if frame.phoneme_id not in controlled:
                _validate_label(
                    utterance_id,
                    frame.frame_index,
                    frame.phoneme_id,
                    controlled_phonemes,
                    stage,
                )


def align_exact(
    frames_by_utterance: dict[str, tuple[FeatureFrame, ...]],
    controlled_phonemes: tuple[int, ...],
    prototypes: dict[int, dict[str, Any]],
) -> dict[str, tuple[AlignmentFrame, ...]]:
    aligned: dict[str, tuple[AlignmentFrame, ...]] = {}
    for utterance_id in sorted(frames_by_utterance):
        rows: list[AlignmentFrame] = []
        for frame in frames_by_utterance[utterance_id]:
            ranked = _rank_candidates(frame, controlled_phonemes, prototypes)
            best_score, best = ranked[0]
            runner_up = ranked[1][1] if len(ranked) > 1 else best
            second_score = ranked[1][0] if len(ranked) > 1 else best_score
            _validate_label(
                utterance_id,
                frame.frame_index,
                best,
                controlled_phonemes,
                "alignment.exact",
            )
            rows.append(
                AlignmentFrame(
                    utterance_id=utterance_id,
                    speaker_id=frame.speaker_id,
                    frame_index=frame.frame_index,
                    phoneme_id=best,
                    score=best_score,
                    runner_up=runner_up,
                    margin=stable_round(best_score - second_score, 12),
                )
            )
        aligned[utterance_id] = tuple(rows)
    return aligned


@dataclass(frozen=True)
class ApproximateAlignment:
    alignment: dict[str, tuple[AlignmentFrame, ...]]
    candidate_phonemes: tuple[int, ...]
    missing_phonemes: tuple[int, ...]
    anchors: dict[str, tuple[int, ...]]
    comparisons: int


def align_approximate(
    frames_by_utterance: dict[str, tuple[FeatureFrame, ...]],
    controlled_phonemes: tuple[int, ...],
    prototypes: dict[int, dict[str, Any]],
    coarse_hop_frames: int = 2,
    candidate_count: int | None = None,
) -> ApproximateAlignment:
    if coarse_hop_frames <= 0:
        raise AppError(
            "E_INPUT_INVALID",
            "coarse_hop_frames must be positive.",
            stage="alignment.approx",
            field="coarse_hop_frames",
        )
    candidate_k = max(1, candidate_count or max(1, math.ceil(len(controlled_phonemes) * 0.6)))
    candidate_union: set[int] = set()
    comparisons = 0
    anchors_by_utterance: dict[str, tuple[int, ...]] = {}
    provisional: dict[str, dict[int, int]] = {}
    for utterance_id in sorted(frames_by_utterance):
        frames = frames_by_utterance[utterance_id]
        anchors = tuple(range(0, len(frames), coarse_hop_frames))
        anchors_by_utterance[utterance_id] = anchors
        label_by_anchor: dict[int, int] = {}
        for anchor in anchors:
            ranked = _rank_candidates(frames[anchor], controlled_phonemes, prototypes)
            comparisons += len(ranked)
            chosen = ranked[:candidate_k]
            candidate_union.update(item[1] for item in chosen)
            label_by_anchor[anchor] = chosen[0][1]
        provisional[utterance_id] = label_by_anchor

    missing_before_guard = tuple(
        phoneme_id
        for phoneme_id in controlled_phonemes
        if phoneme_id not in candidate_union
    )
    if missing_before_guard and frames_by_utterance:
        first_utterance = sorted(frames_by_utterance)[0]
        first_frames = frames_by_utterance[first_utterance]
        if first_frames:
            frame = first_frames[0]
            ranked = _rank_candidates(frame, controlled_phonemes, prototypes)
            scores = {phoneme_id: score for score, phoneme_id in ranked}
            missing_best = max(
                missing_before_guard,
                key=lambda phoneme_id: (scores.get(phoneme_id, 0.0), -phoneme_id),
            )
            comparisons += len(ranked)
            provisional[first_utterance][0] = missing_best
            candidate_union.update(missing_before_guard)

    missing_after_guard = tuple(
        phoneme_id
        for phoneme_id in controlled_phonemes
        if phoneme_id not in candidate_union
    )
    if missing_after_guard:
        raise AppError(
            "E_INCONSISTENT",
            "Approximate path failed to cover every controlled phoneme.",
            stage="alignment.approx.coverage",
            details={
                "missing_phonemes": list(missing_after_guard),
                "controlled_phonemes": list(controlled_phonemes),
            },
        )

    aligned: dict[str, tuple[AlignmentFrame, ...]] = {}
    for utterance_id in sorted(frames_by_utterance):
        frames = frames_by_utterance[utterance_id]
        anchors = anchors_by_utterance[utterance_id]
        label_by_anchor = provisional[utterance_id]
        rows: list[AlignmentFrame] = []
        for index, frame in enumerate(frames):
            anchor = min(anchors, key=lambda value: abs(value - index))
            phoneme_id = label_by_anchor[anchor]
            ranked = _rank_candidates(
                frame,
                controlled_phonemes,
                prototypes,
                allowed=tuple(sorted(
                    set(
                        item[1]
                        for item in _rank_candidates(frame, controlled_phonemes, prototypes)[
                            :candidate_k
                        ]
                    )
                )),
            )
            best_score = ranked[0][0]
            runner_up = ranked[1][1] if len(ranked) > 1 else phoneme_id
            second_score = ranked[1][0] if len(ranked) > 1 else best_score
            _validate_label(
                utterance_id,
                frame.frame_index,
                phoneme_id,
                controlled_phonemes,
                "alignment.approx",
            )
            rows.append(
                AlignmentFrame(
                    utterance_id=utterance_id,
                    speaker_id=frame.speaker_id,
                    frame_index=frame.frame_index,
                    phoneme_id=phoneme_id,
                    score=best_score,
                    runner_up=runner_up,
                    margin=stable_round(best_score - second_score, 12),
                )
            )
        aligned[utterance_id] = tuple(rows)

    return ApproximateAlignment(
        alignment=aligned,
        candidate_phonemes=tuple(sorted(candidate_union)),
        missing_phonemes=missing_after_guard,
        anchors=anchors_by_utterance,
        comparisons=comparisons,
    )


def alignment_digest(
    alignment: dict[str, tuple[AlignmentFrame, ...]],
) -> str:
    return digest_payload(alignment)


def flatten_alignment(
    alignment: dict[str, tuple[AlignmentFrame, ...]],
) -> list[dict[str, Any]]:
    return [
        row.to_dict()
        for utterance_id in sorted(alignment)
        for row in alignment[utterance_id]
    ]
