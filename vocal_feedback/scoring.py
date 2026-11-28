from __future__ import annotations

import math
from typing import Any

import numpy as np

from .config import Config
from .reference import ReferenceNote


def _overlap(left_start: float, left_end: float, right_start: float, right_end: float) -> float:
    return max(0.0, min(left_end, right_end) - max(left_start, right_start))


def score_against_reference(
    notes: list[dict[str, Any]],
    reference: list[ReferenceNote],
    config: Config,
) -> dict[str, Any]:
    used: set[int] = set()
    matches: list[dict[str, Any]] = []
    unmatched_reference: list[dict[str, Any]] = []

    for ref in reference:
        ref_duration = max(ref.end_s - ref.start_s, 1e-9)
        candidates: list[tuple[float, int]] = []
        for note_index, note in enumerate(notes):
            if note_index in used:
                continue
            overlap = _overlap(
                ref.start_s, ref.end_s, float(note["start_s"]), float(note["end_s"])
            )
            overlap_ratio = overlap / ref_duration
            onset_distance = abs(float(note["start_s"]) - ref.start_s)
            if overlap_ratio > 0.15 or onset_distance <= config.boundary_tolerance_s:
                candidates.append((overlap_ratio - 0.25 * onset_distance, note_index))
        if not candidates:
            unmatched_reference.append(
                {
                    "reference_index": ref.index,
                    "start_s": round(ref.start_s, 6),
                    "end_s": round(ref.end_s, 6),
                    "pitch": ref.note_name,
                    "reason": "no_overlapping_detected_note",
                }
            )
            continue
        _, note_index = max(candidates, key=lambda item: item[0])
        used.add(note_index)
        note = notes[note_index]
        cents = (float(note["midi"]) - ref.midi) * 100.0
        abs_cents = abs(cents)
        hit = abs_cents <= config.pitch_hit_cents
        if cents > 5.0:
            direction = "sharp"
        elif cents < -5.0:
            direction = "flat"
        else:
            direction = "centered"
        note_duration = max(float(note["end_s"]) - float(note["start_s"]), 1e-9)
        matches.append(
            {
                "reference_index": ref.index,
                "note_id": note["id"],
                "reference_pitch": ref.note_name,
                "reference_midi": round(ref.midi, 4),
                "detected_midi": round(float(note["midi"]), 4),
                "detected_note": note["note_name"],
                "cents_deviation": round(cents, 3),
                "absolute_cents": round(abs_cents, 3),
                "pitch_hit": bool(hit),
                "direction": direction,
                "reference_start_s": round(ref.start_s, 6),
                "reference_end_s": round(ref.end_s, 6),
                "detected_start_s": note["start_s"],
                "detected_end_s": note["end_s"],
                "onset_error_ms": round((float(note["start_s"]) - ref.start_s) * 1000.0, 3),
                "offset_error_ms": round((float(note["end_s"]) - ref.end_s) * 1000.0, 3),
                "duration_error_ms": round((note_duration - ref_duration) * 1000.0, 3),
                "temporal_overlap_ratio": round(
                    _overlap(
                        ref.start_s,
                        ref.end_s,
                        float(note["start_s"]),
                        float(note["end_s"]),
                    )
                    / ref_duration,
                    6,
                ),
            }
        )

    for note_index, note in enumerate(notes):
        if note_index not in used:
            unmatched_reference.append(
                {
                    "reference_index": None,
                    "note_id": note["id"],
                    "start_s": note["start_s"],
                    "end_s": note["end_s"],
                    "pitch": note["note_name"],
                    "reason": "detected_note_without_reference",
                }
            )

    matched = [match for match in matches if match["reference_index"] is not None]
    hit_count = sum(match["pitch_hit"] for match in matched)
    pitch_hit_rate = hit_count / len(reference) if reference else 0.0
    absolute_cents = np.asarray([match["absolute_cents"] for match in matched], dtype=np.float64)
    signed_cents = np.asarray([match["cents_deviation"] for match in matched], dtype=np.float64)
    onset_errors_ms = np.asarray([match["onset_error_ms"] for match in matched], dtype=np.float64)
    duration_errors_ms = np.asarray(
        [match["duration_error_ms"] for match in matched], dtype=np.float64
    )

    ordered = sorted(matched, key=lambda item: item["reference_start_s"])
    ioi_errors_ms: list[float] = []
    for previous, current in zip(ordered, ordered[1:]):
        reference_ioi = current["reference_start_s"] - previous["reference_start_s"]
        detected_ioi = current["detected_start_s"] - previous["detected_start_s"]
        ioi_errors_ms.append((detected_ioi - reference_ioi) * 1000.0)
    ioi_array = np.asarray(ioi_errors_ms, dtype=np.float64) if ioi_errors_ms else np.zeros(0)

    onset_mae = float(np.mean(np.abs(onset_errors_ms))) if len(onset_errors_ms) else 0.0
    onset_std = float(np.std(onset_errors_ms)) if len(onset_errors_ms) else 0.0
    ioi_mae = float(np.mean(np.abs(ioi_array))) if len(ioi_array) else 0.0
    stability_score = 100.0 * math.exp(-(onset_std / 120.0 + ioi_mae / 180.0))
    direction_counts = {
        "sharp": sum(1 for match in matched if match["direction"] == "sharp"),
        "flat": sum(1 for match in matched if match["direction"] == "flat"),
        "centered": sum(1 for match in matched if match["direction"] == "centered"),
    }
    summary = {
        "reference_note_count": len(reference),
        "matched_note_count": len(matched),
        "pitch_hit_count": hit_count,
        "pitch_hit_rate": round(pitch_hit_rate, 6),
        "mean_absolute_cents": round(float(np.mean(absolute_cents)), 3)
        if len(absolute_cents)
        else None,
        "mean_signed_cents": round(float(np.mean(signed_cents)), 3)
        if len(signed_cents)
        else None,
        "max_absolute_cents": round(float(np.max(absolute_cents)), 3)
        if len(absolute_cents)
        else None,
        "direction_counts": direction_counts,
        "onset_mae_ms": round(onset_mae, 3),
        "onset_std_ms": round(onset_std, 3),
        "duration_mae_ms": round(float(np.mean(np.abs(duration_errors_ms))), 3)
        if len(duration_errors_ms)
        else 0.0,
        "ioi_mae_ms": round(ioi_mae, 3),
        "rhythm_stability_score": round(float(np.clip(stability_score, 0.0, 100.0)), 3),
        "pitch_tolerance_cents": config.pitch_hit_cents,
    }
    return {
        "summary": summary,
        "notes": matches,
        "unmatched": unmatched_reference,
        "matched_detected_note_ids": [notes[index]["id"] for index in sorted(used)],
    }


def phrase_summaries(
    lyrics: Any,
    syllables: list[dict[str, Any]],
    notes: list[dict[str, Any]],
    scoring: dict[str, Any],
    phonemes: list[dict[str, Any]],
    config: Config,
) -> list[dict[str, Any]]:
    note_by_id = {note["id"]: note for note in notes}
    match_by_note = {
        match["note_id"]: match
        for match in scoring["notes"]
        if match.get("note_id") is not None
    }
    syllable_by_index = {item["index"]: item for item in syllables}
    phoneme_by_id = {phoneme["id"]: phoneme for phoneme in phonemes}
    output: list[dict[str, Any]] = []
    for phrase in lyrics.phrases:
        phrase_syllables = [
            syllable_by_index[index]
            for index in phrase.syllable_indices
            if index in syllable_by_index
        ]
        phrase_notes: list[dict[str, Any]] = []
        for syllable in phrase_syllables:
            for note_id in syllable["note_ids"]:
                if note_id in note_by_id:
                    phrase_notes.append(note_by_id[note_id])
        phrase_phonemes = [
            phoneme_by_id[phoneme_id]
            for syllable in phrase_syllables
            for phoneme_id in syllable["phoneme_ids"]
            if phoneme_id in phoneme_by_id
        ]
        hits = 0
        directions: list[str] = []
        cents_values: list[float] = []
        for note in phrase_notes:
            match = match_by_note.get(note["id"])
            if match:
                hits += int(match["pitch_hit"])
                directions.append(match["direction"])
                cents_values.append(float(match["cents_deviation"]))
        summary_text = (
            f"{phrase.text}：{len(phrase_syllables)} 个音节，"
            f"{len(phrase_notes)} 个音符，音准命中 {hits}/{len(phrase_notes)}"
        )
        if cents_values:
            summary_text += f"，平均偏差 {float(np.mean(cents_values)):+.1f} 音分"
        low_phonemes = [phoneme["id"] for phoneme in phrase_phonemes if phoneme["low_confidence"]]
        if low_phonemes:
            summary_text += f"，低置信音素 {len(low_phonemes)} 个"
        output.append(
            {
                "index": phrase.index,
                "text": phrase.text,
                "syllable_count": len(phrase_syllables),
                "note_count": len(phrase_notes),
                "pitch_hit_count": hits,
                "mean_cents_deviation": round(float(np.mean(cents_values)), 3)
                if cents_values
                else None,
                "flat_count": directions.count("flat"),
                "sharp_count": directions.count("sharp"),
                "low_confidence_phoneme_ids": low_phonemes,
                "human_summary": summary_text,
            }
        )
    return output

