from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .config import Config
from .lyrics import Lyrics, SyllableToken


@dataclass
class SyllableMapping:
    syllable: SyllableToken
    note_indices: list[int]
    start_s: float
    end_s: float
    vowel_start_s: float
    mapping_confidence: float
    primary_note_index: int | None = None
    phoneme_ids: list[str] = field(default_factory=list)


def _group_cost(notes: list[dict[str, Any]], indices: list[int]) -> float:
    """Cost of assigning notes[start:end] to one syllable.

    Extra notes are allowed (melisma), but weak note-to-note boundaries are
    cheaper than strong ones. This keeps a melisma attached to the syllable
    whose vowel actually continues across the note change.
    """

    size = len(indices)
    if size <= 1:
        return 0.0
    cost = 0.28 * (size - 1)
    for position in range(size - 1):
        left = notes[indices[position]]
        right = notes[indices[position + 1]]
        gap = max(0.0, float(right["start_s"]) - float(left["end_s"]))
        pitch_change = abs(float(right["midi"]) - float(left["midi"]))
        evidence = str(right.get("start_evidence") or "")
        # A contiguous note change with no acoustic gap is characteristic of a
        # melisma, not a new syllable onset. Do not penalize it as a strong
        # syllable boundary merely because its pitch differs.
        strong = 0.45 if "pitch_jump" in evidence and gap > 0.020 else 0.0
        cost += 2.4 * gap + 0.035 * pitch_change + strong
    return cost


def _partition_notes(
    notes: list[dict[str, Any]], note_indices: list[int], syllable_count: int
) -> list[list[int]] | None:
    if syllable_count <= 0:
        return []
    n = len(note_indices)
    if n < syllable_count:
        return None
    if n == syllable_count:
        return [[index] for index in note_indices]

    infinity = float("inf")
    dp = np.full((syllable_count + 1, n + 1), infinity, dtype=np.float64)
    previous = np.full((syllable_count + 1, n + 1), -1, dtype=np.int64)
    dp[0, 0] = 0.0
    for syllable in range(1, syllable_count + 1):
        for used in range(syllable, n + 1):
            # Keep at least one note for every remaining syllable.
            if n - used < syllable_count - syllable:
                continue
            best = infinity
            best_previous = -1
            for group_start in range(syllable - 1, used):
                if not math.isfinite(dp[syllable - 1, group_start]):
                    continue
                cost = (
                    dp[syllable - 1, group_start]
                    + _group_cost(notes, note_indices[group_start:used])
                )
                if cost < best:
                    best = cost
                    best_previous = group_start
            dp[syllable, used] = best
            previous[syllable, used] = best_previous
    if not math.isfinite(dp[syllable_count, n]):
        return None

    groups: list[list[int]] = []
    used = n
    for syllable in range(syllable_count, 0, -1):
        group_start = int(previous[syllable, used])
        groups.append(note_indices[group_start:used])
        used = group_start
    groups.reverse()
    return groups


def map_notes_to_syllables(
    notes: list[dict[str, Any]], lyrics: Lyrics, config: Config
) -> tuple[list[SyllableMapping], list[dict[str, Any]], dict[str, Any]]:
    eligible_indices = [
        index
        for index, note in enumerate(notes)
        if float(note["confidence"]) >= config.note_min_confidence
        and float(note["duration_s"]) > 0.0
    ]
    groups = _partition_notes(notes, eligible_indices, len(lyrics.syllables))
    mappings: list[SyllableMapping] = []
    unmapped_notes: list[dict[str, Any]] = []

    if groups is None:
        mapping_reason = "fewer_eligible_notes_than_syllables"
        for syllable in lyrics.syllables:
            mappings.append(
                SyllableMapping(
                    syllable=syllable,
                    note_indices=[],
                    start_s=0.0,
                    end_s=0.0,
                    vowel_start_s=0.0,
                    mapping_confidence=0.0,
                )
            )
    else:
        mapping_reason = "monotonic_partition_with_melisma_merge"
        for syllable, group in zip(lyrics.syllables, groups):
            mapping = SyllableMapping(
                syllable=syllable,
                note_indices=group,
                start_s=float(notes[group[0]]["start_s"]),
                end_s=float(notes[group[-1]]["end_s"]),
                vowel_start_s=float(notes[group[0]]["start_s"]),
                mapping_confidence=float(
                    np.mean([notes[index]["confidence"] for index in group])
                ),
            )
            mappings.append(mapping)
            for position, note_index in enumerate(group):
                note = notes[note_index]
                note["mapped"] = True
                note["syllable_index"] = syllable.index
                note["mapping_role"] = "member"
                note["melisma_position"] = position

    mapped_note_indices = {
        index for mapping in mappings for index in mapping.note_indices
    }
    for index, note in enumerate(notes):
        if index in mapped_note_indices:
            continue
        if index in eligible_indices:
            reason = "not_selected_by_monotonic_alignment"
        elif float(note["confidence"]) < config.note_min_confidence:
            reason = "low_note_confidence"
        else:
            reason = "invalid_note_interval"
        unmapped_notes.append(
            {
                "note_id": note["id"],
                "note_index": index,
                "start_s": note["start_s"],
                "end_s": note["end_s"],
                "midi": note["midi"],
                "confidence": note["confidence"],
                "reason": reason,
            }
        )

    unmapped_syllables = [
        {
            "index": mapping.syllable.index,
            "text": mapping.syllable.text,
            "pinyin": mapping.syllable.pinyin,
            "reason": "no_eligible_note_available",
        }
        for mapping in mappings
        if not mapping.note_indices
    ]
    mapped_syllable_count = sum(bool(mapping.note_indices) for mapping in mappings)
    validation = {
        "text_syllable_count": len(lyrics.syllables),
        "mapped_syllable_count": mapped_syllable_count,
        "mapped_note_count": len(mapped_note_indices),
        "unmapped_note_count": len(unmapped_notes),
        "unmapped_syllable_count": len(unmapped_syllables),
        "syllable_count_matches_text": mapped_syllable_count == len(lyrics.syllables),
        "mapping_method": mapping_reason,
        "unmapped_syllables": unmapped_syllables,
    }
    return mappings, unmapped_notes, validation


def _interval_indices(times: np.ndarray, start_s: float, end_s: float) -> np.ndarray:
    if end_s < start_s:
        return np.empty(0, dtype=np.int64)
    lo = int(np.searchsorted(times, start_s, side="left"))
    hi = int(np.searchsorted(times, end_s, side="right"))
    lo = max(0, min(lo, len(times)))
    hi = max(lo, min(hi, len(times)))
    return np.arange(lo, hi, dtype=np.int64)


def _energy_presence(energy_db: np.ndarray, indices: np.ndarray, silence_dbfs: float) -> float:
    if len(indices) == 0:
        return 0.0
    relative = np.clip((energy_db[indices] - silence_dbfs) / 18.0, 0.0, 1.0)
    return float(np.mean(relative))


def _phoneme_confidence(
    kind: str,
    indices: np.ndarray,
    voiced: np.ndarray,
    confidence: np.ndarray,
    energy: np.ndarray,
    zcr: np.ndarray,
    silence_dbfs: float,
) -> tuple[float, float]:
    if len(indices) == 0:
        return 0.0, 1.0
    voiced_ratio = float(np.mean(voiced[indices]))
    unvoiced_ratio = 1.0 - voiced_ratio
    mean_confidence = float(np.mean(confidence[indices]))
    energy_score = _energy_presence(energy, indices, silence_dbfs)
    zcr_score = float(np.clip(np.mean(zcr[indices]) * 3.0, 0.0, 1.0))
    if kind == "consonant":
        affinity = (
            0.52 * unvoiced_ratio
            + 0.22 * zcr_score
            + 0.18 * energy_score
            + 0.08 * (1.0 - mean_confidence)
        )
    else:
        affinity = 0.66 * voiced_ratio + 0.24 * mean_confidence + 0.10 * energy_score
    affinity = float(np.clip(affinity, 0.0, 1.0))
    return affinity, 1.0 - affinity


def _find_consonant_onset(
    times: np.ndarray,
    energy_db: np.ndarray,
    span_start: float,
    vowel_start: float,
    config: Config,
) -> float:
    lo = max(0.0, span_start)
    hi = max(lo, vowel_start)
    indices = _interval_indices(times, lo, hi + config.hop_length_s)
    if len(indices):
        threshold = config.silence_dbfs + 4.0
        active = indices[energy_db[indices] >= threshold]
        if len(active):
            hop = (
                float(np.median(np.diff(times)))
                if len(times) > 1
                else config.hop_length_s
            )
            return max(lo, float(times[int(active[0])] - 0.5 * hop))
    return max(lo, vowel_start - config.min_consonant_duration_s)


def align_phonemes(
    mappings: list[SyllableMapping],
    frames: list[dict[str, Any]],
    notes: list[dict[str, Any]],
    config: Config,
) -> list[dict[str, Any]]:
    if not frames:
        return []
    times = np.asarray([frame["timestamp_s"] for frame in frames], dtype=np.float64)
    voiced = np.asarray([bool(frame["voiced"]) for frame in frames], dtype=bool)
    confidence = np.asarray([frame["voicing_confidence"] for frame in frames], dtype=np.float64)
    energy = np.asarray([frame["energy_dbfs"] for frame in frames], dtype=np.float64)
    zcr = np.asarray([frame["zero_crossing_rate"] for frame in frames], dtype=np.float64)
    hop = float(np.median(np.diff(times))) if len(times) > 1 else config.hop_length_s

    phonemes: list[dict[str, Any]] = []
    previous_end = 0.0
    for mapping in mappings:
        if not mapping.note_indices:
            continue
        syllable = mapping.syllable
        first_note = notes[mapping.note_indices[0]]
        last_note = notes[mapping.note_indices[-1]]
        vowel_start = float(first_note["start_s"])
        syllable_end = float(last_note["end_s"])
        span_start = max(previous_end, vowel_start - config.max_consonant_duration_s)
        onset_start = vowel_start
        if syllable.initial:
            onset_start = _find_consonant_onset(
                times, energy, span_start, vowel_start, config
            )
            if vowel_start - onset_start > config.max_consonant_duration_s:
                onset_start = vowel_start - config.max_consonant_duration_s
            if vowel_start - onset_start < config.min_consonant_duration_s:
                onset_start = max(span_start, vowel_start - config.min_consonant_duration_s)
        else:
            onset_start = vowel_start

        mapping.start_s = onset_start
        mapping.end_s = syllable_end
        mapping.vowel_start_s = vowel_start

        if syllable.initial:
            consonant_indices = _interval_indices(times, onset_start, vowel_start)
            affinity, cost = _phoneme_confidence(
                "consonant",
                consonant_indices,
                voiced,
                confidence,
                energy,
                zcr,
                config.silence_dbfs,
            )
            duration = vowel_start - onset_start
            duration_score = float(np.clip(duration / 0.070, 0.0, 1.0))
            consonant_confidence = float(
                np.clip(0.72 * affinity + 0.28 * duration_score, 0.0, 1.0)
            )
            phonemes.append(
                {
                    "id": f"p{len(phonemes) + 1:04d}",
                    "syllable_index": syllable.index,
                    "text": syllable.text,
                    "pinyin": syllable.pinyin,
                    "label": syllable.initial,
                    "kind": "consonant",
                    "start_s": round(onset_start, 6),
                    "end_s": round(vowel_start, 6),
                    "duration_s": round(max(0.0, duration), 6),
                    "confidence": round(consonant_confidence, 6),
                    "alignment_cost": round(float(cost), 6),
                    "category_affinity": round(float(affinity), 6),
                    "low_confidence": (
                        consonant_confidence < config.low_confidence_phoneme
                    ),
                }
            )

        vowel_indices = _interval_indices(times, vowel_start, syllable_end)
        affinity, cost = _phoneme_confidence(
            "vowel",
            vowel_indices,
            voiced,
            confidence,
            energy,
            zcr,
            config.silence_dbfs,
        )
        duration = max(0.0, syllable_end - vowel_start)
        duration_score = float(np.clip(duration / 0.120, 0.0, 1.0))
        vowel_confidence = float(np.clip(0.78 * affinity + 0.22 * duration_score, 0.0, 1.0))
        vowel_id = f"p{len(phonemes) + 1:04d}"
        phonemes.append(
            {
                "id": vowel_id,
                "syllable_index": syllable.index,
                "text": syllable.text,
                "pinyin": syllable.pinyin,
                "label": syllable.final or syllable.pinyin or syllable.text,
                "kind": "vowel",
                "start_s": round(vowel_start, 6),
                "end_s": round(syllable_end, 6),
                "duration_s": round(duration, 6),
                "confidence": round(vowel_confidence, 6),
                "alignment_cost": round(float(cost), 6),
                "category_affinity": round(float(affinity), 6),
                "low_confidence": vowel_confidence < config.low_confidence_phoneme,
            }
        )
        mapping.phoneme_ids = [
            phoneme["id"]
            for phoneme in phonemes
            if phoneme["syllable_index"] == syllable.index
        ]

        # The main note is the note on which the syllable vowel begins. For a
        # melisma the following notes remain attached as continuation notes.
        best_index = mapping.note_indices[0]
        for note_index in mapping.note_indices:
            note = notes[note_index]
            if (
                float(note["start_s"]) - 1e-6
                <= vowel_start
                <= float(note["end_s"]) + 1e-6
            ):
                best_index = note_index
                break
        mapping.primary_note_index = best_index
        for note_index in mapping.note_indices:
            note = notes[note_index]
            note["mapping_role"] = (
                "primary" if note_index == best_index else "melisma"
            )
            note["primary_rule"] = "vowel_onset"
            note["vowel_overlap_s"] = round(
                max(
                    0.0,
                    min(float(note["end_s"]), syllable_end)
                    - max(float(note["start_s"]), vowel_start),
                ),
                6,
            )
        previous_end = syllable_end
    return phonemes


def serialize_syllables(
    mappings: list[SyllableMapping],
    notes: list[dict[str, Any]],
    phonemes: list[dict[str, Any]],
    config: Config,
) -> list[dict[str, Any]]:
    phonemes_by_syllable: dict[int, list[dict[str, Any]]] = {}
    for phoneme in phonemes:
        phonemes_by_syllable.setdefault(int(phoneme["syllable_index"]), []).append(phoneme)
    output: list[dict[str, Any]] = []
    for mapping in mappings:
        primary = (
            notes[mapping.primary_note_index]["id"]
            if mapping.primary_note_index is not None
            else None
        )
        output.append(
            {
                "index": mapping.syllable.index,
                "text": mapping.syllable.text,
                "pinyin": mapping.syllable.pinyin,
                "phrase_index": mapping.syllable.phrase_index,
                "start_s": round(mapping.start_s, 6) if mapping.note_indices else None,
                "end_s": round(mapping.end_s, 6) if mapping.note_indices else None,
                "vowel_start_s": (
                    round(mapping.vowel_start_s, 6) if mapping.note_indices else None
                ),
                "mapped": bool(mapping.note_indices),
                "note_ids": [notes[index]["id"] for index in mapping.note_indices],
                "primary_note_id": primary,
                "mapping_confidence": round(mapping.mapping_confidence, 6),
                "low_confidence": mapping.mapping_confidence < config.low_confidence_note,
                "phoneme_ids": [phoneme["id"] for phoneme in phonemes_by_syllable.get(mapping.syllable.index, [])],
            }
        )
    return output

