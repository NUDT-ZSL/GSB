from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from .alignment import (
    align_phonemes,
    map_notes_to_syllables,
    serialize_syllables,
)
from .audio import frame_audio, read_wav
from .config import Config
from .dataset import SampleFiles
from .lyrics import load_lyrics
from .notes import segment_notes
from .pitch import estimate_f0, smooth_and_correct_pitch
from .reference import load_reference, serialize_reference
from .report import build_low_confidence_intervals
from .scoring import phrase_summaries, score_against_reference


def analyze_sample(sample: SampleFiles, config: Config) -> dict[str, Any]:
    config.validate()
    audio = read_wav(sample.wav)
    framed = frame_audio(audio, config.frame_length_s, config.hop_length_s)
    frames = estimate_f0(framed, config)
    frames = smooth_and_correct_pitch(frames, config)
    notes = segment_notes(frames, config)
    lyrics = load_lyrics(sample.lyrics)
    if not lyrics.syllables:
        raise ValueError(f"{sample.lyrics}: no singable syllables found")
    mappings, unmapped_notes, mapping_validation = map_notes_to_syllables(
        notes, lyrics, config
    )
    phonemes = align_phonemes(mappings, frames, notes, config)
    syllables = serialize_syllables(mappings, notes, phonemes, config)
    reference = load_reference(sample.reference)
    scoring = score_against_reference(notes, reference, config)
    phrases = phrase_summaries(lyrics, syllables, notes, scoring, phonemes, config)
    low_confidence_intervals = build_low_confidence_intervals(
        frames, notes, phonemes, unmapped_notes, config
    )

    warnings: list[str] = []
    if not mapping_validation["syllable_count_matches_text"]:
        warnings.append(
            "音节映射数与歌词文本不一致："
            f"{mapping_validation['mapped_syllable_count']}/"
            f"{mapping_validation['text_syllable_count']}"
        )
    if unmapped_notes:
        warnings.append(f"存在 {len(unmapped_notes)} 个未映射音符")
    low_phoneme_count = sum(phoneme["low_confidence"] for phoneme in phonemes)
    if low_phoneme_count:
        warnings.append(f"存在 {low_phoneme_count} 个低置信度音素")
    low_note_count = sum(
        note["confidence"] < config.low_confidence_note for note in notes
    )
    if low_note_count:
        warnings.append(f"存在 {low_note_count} 个低置信度音符")
    if not any(frame["voiced"] for frame in frames):
        warnings.append("未检测到浊音帧")

    midi_values = [
        float(frame["midi"]) for frame in frames if frame["midi"] is not None
    ]
    f0_values = [
        float(frame["f0_hz"]) for frame in frames if frame["f0_hz"] is not None
    ]
    summary = {
        "duration_s": round(audio.duration_s, 6),
        "sample_rate_hz": audio.sample_rate,
        "frame_count": len(frames),
        "voiced_frame_count": sum(bool(frame["voiced"]) for frame in frames),
        "f0_min_hz": round(min(f0_values), 4) if f0_values else None,
        "f0_max_hz": round(max(f0_values), 4) if f0_values else None,
        "midi_min": round(min(midi_values), 4) if midi_values else None,
        "midi_max": round(max(midi_values), 4) if midi_values else None,
        "note_count": len(notes),
        "phoneme_count": len(phonemes),
        "syllable_count": len(lyrics.syllables),
        "mapped_syllable_count": mapping_validation["mapped_syllable_count"],
        "syllable_count_matches_text": mapping_validation[
            "syllable_count_matches_text"
        ],
        "unmapped_note_count": len(unmapped_notes),
        "low_confidence_phoneme_count": low_phoneme_count,
        "low_confidence_note_count": low_note_count,
        "pitch_hit_rate": scoring["summary"]["pitch_hit_rate"],
        "rhythm_stability_score": scoring["summary"]["rhythm_stability_score"],
        "warnings": warnings,
    }
    report = {
        "schema_version": "1.0",
        "sample_id": sample.sample_id,
        "files": {
            "audio": str(sample.wav),
            "lyrics": str(sample.lyrics),
            "reference": str(sample.reference),
            "annotations": str(sample.annotations) if sample.annotations else None,
        },
        "config": config.to_dict(),
        "summary": summary,
        "lyrics": {
            "text": Path(sample.lyrics).read_text(encoding="utf-8").strip(),
            "phrases": [
                {
                    "index": phrase.index,
                    "text": phrase.text,
                    "syllable_indices": phrase.syllable_indices,
                }
                for phrase in lyrics.phrases
            ],
        },
        "reference": serialize_reference(reference),
        "frames": frames,
        "notes": notes,
        "phonemes": phonemes,
        "syllables": syllables,
        "mapping_validation": mapping_validation,
        "unmapped_notes": unmapped_notes,
        "scoring": scoring,
        "phrases": phrases,
        "low_confidence_intervals": low_confidence_intervals,
        "warnings": warnings,
        "method_notes": {
            "pitch": "per-frame autocorrelation versus cepstrum candidate selection using harmonic comb strength, coverage, band priors and voiced-periodicity confidence",
            "smoothing": "isolated-octave correction with raw observations retained; local-rate adaptive median windows (3-9 frames)",
            "notes": "pitch-jump, energy-dip and voicing-drop boundaries; short fragments merged by pitch proximity",
            "alignment": "monotonic note-to-syllable partition with melisma merge, then category-aware onset/vowel boundary alignment",
        },
    }
    return report

