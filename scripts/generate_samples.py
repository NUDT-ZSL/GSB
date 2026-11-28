#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vocal_feedback.lyrics import parse_lyrics_text
from vocal_feedback.pitch import hz_to_midi, midi_to_note_name


SAMPLE_RATE = 22050
FRAME_LENGTH_S = 0.040
HOP_LENGTH_S = 0.010


@dataclass
class PlannedNote:
    midi: float
    duration_s: float
    cents: float = 0.0
    vibrato_depth: float = 0.13
    vibrato_rate_hz: float = 5.1
    glissando_semitones: float = 0.0
    glissando_duration_s: float = 0.30
    join_previous: bool = False


@dataclass
class ScheduledNote:
    midi: float
    start_s: float
    end_s: float
    syllable_index: int
    cents: float
    vibrato_depth: float
    vibrato_rate_hz: float
    glissando_semitones: float
    glissando_duration_s: float
    consonant_start_s: float | None = None
    consonant_end_s: float | None = None
    consonant_label: str | None = None
    vowel_label: str = "a"

    def midi_at(self, time_s: float) -> float:
        local = max(0.0, min(time_s - self.start_s, self.end_s - self.start_s))
        glissando = 0.0
        if self.glissando_semitones:
            progress = min(1.0, local / max(self.glissando_duration_s, 1e-6))
            smooth = progress * progress * (3.0 - 2.0 * progress)
            glissando = self.glissando_semitones * smooth
        vibrato = self.vibrato_depth * math.sin(
            2.0 * math.pi * self.vibrato_rate_hz * local
        )
        return self.midi + self.cents / 100.0 + glissando + vibrato

    def hz_at(self, time_s: float) -> float:
        return 440.0 * (2.0 ** ((self.midi_at(time_s) - 69.0) / 12.0))


@dataclass
class SamplePlan:
    sample_id: str
    seed: int
    lyrics_text: str
    syllables: list[list[PlannedNote]]
    phrase_syllable_counts: list[int] = field(default_factory=list)


SAMPLE_PLANS = [
    SamplePlan(
        sample_id="s01_ballad",
        seed=1041,
        lyrics_text="春风吹过小河旁\n白云落在青山上",
        syllables=[
            [PlannedNote(62.0, 0.48)],
            [PlannedNote(64.0, 0.52, cents=-20.0)],
            [PlannedNote(65.0, 0.55, cents=15.0)],
            [PlannedNote(67.0, 0.78, vibrato_depth=0.18)],
            [PlannedNote(64.0, 0.48)],
            [PlannedNote(62.0, 0.52, cents=-30.0)],
            [PlannedNote(60.0, 0.90, vibrato_depth=0.16)],
            [PlannedNote(67.0, 0.55)],
            [PlannedNote(65.0, 0.48)],
            [PlannedNote(64.0, 0.55, cents=-20.0)],
            [PlannedNote(62.0, 0.48)],
            [PlannedNote(60.0, 0.62, cents=25.0)],
            [PlannedNote(62.0, 0.55)],
            [PlannedNote(64.0, 1.00, cents=-15.0, vibrato_depth=0.19)],
        ],
        phrase_syllable_counts=[7, 7],
    ),
    SamplePlan(
        sample_id="s02_high_melisma",
        seed=2047,
        lyrics_text="月光轻轻照山岗\n晚风慢慢送花香",
        syllables=[
            [PlannedNote(67.0, 0.55)],
            [PlannedNote(69.0, 0.58)],
            [PlannedNote(71.0, 0.52)],
            [PlannedNote(72.0, 0.64, cents=-18.0, vibrato_depth=0.20)],
            [PlannedNote(69.0, 0.52)],
            [PlannedNote(65.0, 0.88, glissando_semitones=0.46)],
            [
                PlannedNote(67.0, 0.48),
                PlannedNote(69.0, 0.52, join_previous=True, cents=12.0),
            ],
            [PlannedNote(64.0, 0.52)],
            [PlannedNote(67.0, 0.55)],
            [PlannedNote(69.0, 0.52)],
            [PlannedNote(71.0, 0.56, cents=-22.0)],
            [PlannedNote(69.0, 0.50)],
            [PlannedNote(67.0, 0.54)],
            [PlannedNote(64.0, 0.94, cents=-16.0, vibrato_depth=0.20)],
        ],
        phrase_syllable_counts=[7, 7],
    ),
    SamplePlan(
        sample_id="s03_low_range",
        seed=3079,
        lyrics_text="歌声飞过山岗",
        syllables=[
            [PlannedNote(57.0, 0.55, cents=-12.0)],
            [PlannedNote(60.0, 0.55)],
            [PlannedNote(64.0, 0.58, cents=28.0)],
            [PlannedNote(65.0, 0.52)],
            [PlannedNote(67.0, 0.86, vibrato_depth=0.17)],
            [PlannedNote(69.0, 0.96, cents=-24.0, vibrato_depth=0.21)],
        ],
        phrase_syllable_counts=[6],
    ),
]


_FORMANTS = {
    "a": (820.0, 1250.0),
    "o": (470.0, 820.0),
    "e": (520.0, 1750.0),
    "i": (310.0, 2300.0),
    "u": (350.0, 720.0),
    "v": (330.0, 2100.0),
}


def _smoothstep(value: float) -> float:
    value = float(np.clip(value, 0.0, 1.0))
    return value * value * (3.0 - 2.0 * value)


def _vowel_formants(vowel: str) -> tuple[float, float]:
    for character in vowel.lower():
        if character in _FORMANTS:
            return _FORMANTS[character]
    return _FORMANTS["a"]


def schedule_notes(plan: SamplePlan, rng: np.random.Generator) -> list[ScheduledNote]:
    lyrics = parse_lyrics_text(plan.lyrics_text)
    if len(lyrics.syllables) != len(plan.syllables):
        raise ValueError(
            f"{plan.sample_id}: {len(lyrics.syllables)} lyric syllables but "
            f"{len(plan.syllables)} syllable plans"
        )
    scheduled: list[ScheduledNote] = []
    cursor = 0.16
    phrase_position = 0
    for syllable_index, (syllable, planned_group) in enumerate(
        zip(lyrics.syllables, plan.syllables)
    ):
        if phrase_position == 0 and syllable_index > 0:
            cursor += 0.34
        consonant_duration = float(rng.uniform(0.075, 0.125))
        consonant_label = syllable.initial or "h"
        for note_position, planned in enumerate(planned_group):
            if planned.join_previous:
                start = cursor
            else:
                start = cursor + 0.16
            end = start + planned.duration_s
            scheduled_note = ScheduledNote(
                midi=planned.midi,
                start_s=start,
                end_s=end,
                syllable_index=syllable_index,
                cents=planned.cents,
                vibrato_depth=planned.vibrato_depth,
                vibrato_rate_hz=planned.vibrato_rate_hz,
                glissando_semitones=planned.glissando_semitones,
                glissando_duration_s=planned.glissando_duration_s,
                vowel_label=syllable.final or syllable.pinyin or "a",
            )
            if note_position == 0 and consonant_label:
                scheduled_note.consonant_label = consonant_label
                scheduled_note.consonant_start_s = max(0.02, start - consonant_duration)
                scheduled_note.consonant_end_s = start
            scheduled.append(scheduled_note)
            cursor = end
        phrase_position += 1
        phrase_index = scheduled_phrase_index(plan, syllable_index)
        if phrase_position >= plan.phrase_syllable_counts[phrase_index]:
            phrase_position = 0
    return scheduled


def scheduled_phrase_index(plan: SamplePlan, syllable_index: int) -> int:
    offset = 0
    for index, count in enumerate(plan.phrase_syllable_counts):
        if syllable_index < offset + count:
            return index
        offset += count
    return len(plan.phrase_syllable_counts) - 1


def render_voice(
    signal: np.ndarray,
    note: ScheduledNote,
    sample_rate: int,
    rng: np.random.Generator,
) -> None:
    start = int(round(note.start_s * sample_rate))
    end = int(round(note.end_s * sample_rate))
    if end <= start:
        return
    time = np.arange(end - start) / sample_rate
    local_midi = np.full_like(time, note.midi + note.cents / 100.0)
    local_midi += note.vibrato_depth * np.sin(
        2.0 * np.pi * note.vibrato_rate_hz * time
    )
    if note.glissando_semitones:
        progress = np.clip(time / max(note.glissando_duration_s, 1e-6), 0.0, 1.0)
        local_midi += note.glissando_semitones * (
            progress * progress * (3.0 - 2.0 * progress)
        )
    frequency = 440.0 * np.power(2.0, (local_midi - 69.0) / 12.0)
    phase = 2.0 * np.pi * np.cumsum(frequency) / sample_rate
    length = len(time)
    attack = max(1, int(round(0.028 * sample_rate)))
    release = max(1, int(round(0.045 * sample_rate)))
    envelope = np.ones(length, dtype=np.float64)
    envelope[: min(attack, length)] = np.linspace(0.0, 1.0, min(attack, length))
    if release < length:
        envelope[-release:] *= np.linspace(1.0, 0.0, release)
    else:
        envelope *= np.linspace(1.0, 0.0, length)
    formant1, formant2 = _vowel_formants(note.vowel_label)
    harmonics = max(1, int((sample_rate * 0.45) // max(float(np.mean(frequency)), 1.0)))
    harmonics = min(harmonics, 36)
    harmonic_wave = np.zeros(length, dtype=np.float64)
    amplitude_sum = 0.0
    for harmonic in range(1, harmonics + 1):
        harmonic_hz = float(np.mean(frequency)) * harmonic
        first = math.exp(-0.5 * ((harmonic_hz - formant1) / 220.0) ** 2)
        second = math.exp(-0.5 * ((harmonic_hz - formant2) / 320.0) ** 2)
        amplitude = (1.0 / (harmonic**1.12)) * (0.32 + 1.35 * first + 0.70 * second)
        harmonic_wave += amplitude * np.sin(harmonic * phase + 0.13 * harmonic)
        amplitude_sum += amplitude
    if amplitude_sum > 0:
        harmonic_wave /= amplitude_sum
    breath = rng.normal(0.0, 1.0, length)
    breath = np.convolve(breath, np.ones(5) / 5.0, mode="same")
    voiced = 0.30 * harmonic_wave * envelope + 0.0026 * breath * envelope
    signal[start:end] += voiced


def render_consonant(
    signal: np.ndarray,
    note: ScheduledNote,
    sample_rate: int,
    rng: np.random.Generator,
) -> None:
    if note.consonant_start_s is None or note.consonant_end_s is None:
        return
    start = int(round(note.consonant_start_s * sample_rate))
    end = int(round(note.consonant_end_s * sample_rate))
    if end <= start:
        return
    length = end - start
    noise = rng.normal(0.0, 1.0, length + 1)
    high_passed = np.diff(noise)
    high_passed = high_passed / (float(np.std(high_passed)) + 1e-9)
    envelope = np.ones(length, dtype=np.float64)
    attack = max(1, int(0.012 * sample_rate))
    release = max(1, int(0.020 * sample_rate))
    envelope[: min(attack, length)] = np.linspace(0.0, 1.0, min(attack, length))
    if release < length:
        envelope[-release:] *= np.linspace(1.0, 0.0, release)
    signal[start:end] += 0.034 * high_passed[:length] * envelope


def render_sample(plan: SamplePlan) -> tuple[np.ndarray, list[ScheduledNote]]:
    rng = np.random.default_rng(plan.seed)
    scheduled = schedule_notes(plan, rng)
    duration = max(note.end_s for note in scheduled) + 0.26
    signal = np.zeros(int(math.ceil(duration * SAMPLE_RATE)), dtype=np.float64)
    for note in scheduled:
        render_consonant(signal, note, SAMPLE_RATE, rng)
        render_voice(signal, note, SAMPLE_RATE, rng)
    signal += rng.normal(0.0, 1e-5, len(signal))
    peak = float(np.max(np.abs(signal)))
    if peak > 0.92:
        signal *= 0.92 / peak
    return signal, scheduled


def write_wav(path: Path, signal: np.ndarray) -> None:
    pcm = np.clip(signal, -1.0, 1.0)
    pcm = np.round(pcm * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.setcomptype("NONE", "not compressed")
        handle.writeframes(pcm.tobytes())


def build_annotations(
    plan: SamplePlan, scheduled: list[ScheduledNote]
) -> dict[str, object]:
    lyrics = parse_lyrics_text(plan.lyrics_text)
    frame_length = int(round(FRAME_LENGTH_S * SAMPLE_RATE))
    hop_length = int(round(HOP_LENGTH_S * SAMPLE_RATE))
    duration = max(note.end_s for note in scheduled) + 0.26
    sample_count = int(math.ceil(duration * SAMPLE_RATE))
    starts = np.arange(0, sample_count - frame_length + 1, hop_length)
    times = (starts + frame_length / 2.0) / SAMPLE_RATE
    frame_annotations: list[dict[str, object]] = []
    for time_s in times:
        active = next(
            (
                note
                for note in scheduled
                if note.start_s <= float(time_s) < note.end_s
            ),
            None,
        )
        if active is None:
            frame_annotations.append(
                {
                    "timestamp_s": round(float(time_s), 6),
                    "f0_hz": None,
                    "midi": None,
                    "voiced": False,
                }
            )
        else:
            frequency = active.hz_at(float(time_s))
            frame_annotations.append(
                {
                    "timestamp_s": round(float(time_s), 6),
                    "f0_hz": round(float(frequency), 6),
                    "midi": round(float(hz_to_midi(frequency)), 6),
                    "voiced": True,
                }
            )

    notes_annotation = [
        {
            "index": index,
            "start_s": round(note.start_s, 6),
            "end_s": round(note.end_s, 6),
            "midi": round(note.midi + note.cents / 100.0, 6),
            "note_name": midi_to_note_name(note.midi, cents=True),
            "syllable_index": note.syllable_index,
            "glissando_semitones": note.glissando_semitones,
        }
        for index, note in enumerate(scheduled)
    ]
    phonemes: list[dict[str, object]] = []
    syllables_output: list[dict[str, object]] = []
    for syllable in lyrics.syllables:
        group = [
            note for note in scheduled if note.syllable_index == syllable.index
        ]
        if not group:
            continue
        first = group[0]
        last = group[-1]
        if first.consonant_start_s is not None and first.consonant_end_s is not None:
            phonemes.append(
                {
                    "syllable_index": syllable.index,
                    "text": syllable.text,
                    "pinyin": syllable.pinyin,
                    "label": syllable.initial,
                    "kind": "consonant",
                    "start_s": round(first.consonant_start_s, 6),
                    "end_s": round(first.consonant_end_s, 6),
                }
            )
        phonemes.append(
            {
                "syllable_index": syllable.index,
                "text": syllable.text,
                "pinyin": syllable.pinyin,
                "label": syllable.final,
                "kind": "vowel",
                "start_s": round(first.start_s, 6),
                "end_s": round(last.end_s, 6),
            }
        )
        syllables_output.append(
            {
                "index": syllable.index,
                "text": syllable.text,
                "pinyin": syllable.pinyin,
                "note_indices": [
                    index for index, note in enumerate(scheduled) if note in group
                ],
                "primary_note_index": next(
                    index for index, note in enumerate(scheduled) if note is first
                ),
            }
        )
    return {
        "schema_version": "1.0",
        "sample_id": plan.sample_id,
        "syllable_count": len(lyrics.syllables),
        "notes": notes_annotation,
        "frames": frame_annotations,
        "phonemes": phonemes,
        "syllables": syllables_output,
        "tolerances": {
            "pitch_cents": 50.0,
            "note_boundary_s": 0.100,
            "phoneme_boundary_s": 0.100,
            "min_pitch_hit_rate": 0.90,
            "min_voiced_f1": 0.88,
            "max_note_boundary_mae_ms": 100.0,
            "max_phoneme_boundary_mae_ms": 100.0,
            "min_phoneme_boundary_hit_rate": 0.85,
        },
    }


def write_sample(root: Path, plan: SamplePlan) -> None:
    sample_dir = root / plan.sample_id
    sample_dir.mkdir(parents=True, exist_ok=True)
    signal, scheduled = render_sample(plan)
    wav_path = sample_dir / "audio.wav"
    lyrics_path = sample_dir / "lyrics.txt"
    reference_path = sample_dir / "reference.json"
    annotations_path = sample_dir / "annotations.json"
    manifest_path = sample_dir / "sample.json"
    write_wav(wav_path, signal)
    lyrics_path.write_text(plan.lyrics_text + "\n", encoding="utf-8")
    reference = [
        {
            "start_s": round(note.start_s, 6),
            "end_s": round(note.end_s, 6),
            "pitch": midi_to_note_name(note.midi),
            "midi": round(note.midi, 4),
        }
        for note in scheduled
    ]
    reference_path.write_text(
        json.dumps(reference, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    annotations = build_annotations(plan, scheduled)
    annotations_path.write_text(
        json.dumps(annotations, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    manifest = {
        "id": plan.sample_id,
        "audio": wav_path.name,
        "lyrics": lyrics_path.name,
        "reference": reference_path.name,
        "annotations": annotations_path.name,
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成确定性单声道清唱样本与手工标注")
    parser.add_argument(
        "--output",
        default=str(REPO_ROOT / "samples"),
        help="样本输出根目录（默认：samples）",
    )
    args = parser.parse_args(argv)
    root = Path(args.output)
    root.mkdir(parents=True, exist_ok=True)
    for plan in SAMPLE_PLANS:
        write_sample(root, plan)
        print(f"generated {root / plan.sample_id}")
    config = {
        "frame_length_s": FRAME_LENGTH_S,
        "hop_length_s": HOP_LENGTH_S,
        "fmin_hz": 70.0,
        "fmax_hz": 1000.0,
    }
    (root / "analysis_config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

