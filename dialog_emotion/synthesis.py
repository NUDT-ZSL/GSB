"""Deterministic, offline synthesis of speaker voices and dialog audio."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .canonical import quantize, stable_seed


@dataclass(frozen=True)
class SpeakerVoice:
    speaker_id: str
    f0_hz: float
    formants_hz: tuple[float, float, float]
    gain: float
    syllable_rate: float
    phase_offset: float


@dataclass(frozen=True)
class TimelineBlock:
    start_ms: float
    end_ms: float
    speaker_id: str
    valence: float
    arousal: float
    is_speech: bool


def speaker_voice(speaker_id: str) -> SpeakerVoice:
    seed = stable_seed("speaker-voice-v1", speaker_id)
    rng = np.random.default_rng(seed)
    f0 = float(rng.uniform(105.0, 245.0))
    formants = (
        float(rng.uniform(420.0, 900.0)),
        float(rng.uniform(1150.0, 2050.0)),
        float(rng.uniform(2450.0, 3450.0)),
    )
    gain = float(rng.uniform(0.18, 0.34))
    syllable_rate = float(rng.uniform(3.2, 6.4))
    phase_offset = float(rng.uniform(0.0, 2.0 * np.pi))
    return SpeakerVoice(
        speaker_id=speaker_id,
        f0_hz=round(f0, 8),
        formants_hz=tuple(round(value, 8) for value in formants),
        gain=round(gain, 8),
        syllable_rate=round(syllable_rate, 8),
        phase_offset=round(phase_offset, 8),
    )


def make_timeline(
    duration_ms: int,
    speaker_ids: list[str],
    seed: int,
) -> list[TimelineBlock]:
    if duration_ms <= 0 or not speaker_ids:
        return []
    rng = np.random.default_rng(stable_seed("dialog-timeline-v1", seed, speaker_ids))
    order = list(speaker_ids)
    rng.shuffle(order)
    blocks: list[TimelineBlock] = []
    cursor_ms = 0.0
    speaker_index = 0
    turn_index = 0

    while cursor_ms < duration_ms - 40.0:
        if turn_index > 0 and turn_index % 4 == 0:
            gap_ms = float(rng.integers(35, 71))
            gap_end = min(float(duration_ms), cursor_ms + gap_ms)
            blocks.append(
                TimelineBlock(
                    start_ms=quantize(cursor_ms),
                    end_ms=quantize(gap_end),
                    speaker_id=blocks[-1].speaker_id,
                    valence=blocks[-1].valence,
                    arousal=blocks[-1].arousal,
                    is_speech=False,
                )
            )
            cursor_ms = gap_end
            if cursor_ms >= duration_ms:
                break

        remaining = duration_ms - cursor_ms
        if remaining <= 40.0:
            break
        speech_ms = float(rng.integers(300, 681))
        speech_ms = min(speech_ms, remaining)
        if remaining - speech_ms < 70.0:
            speech_ms = remaining
        speech_ms = round(speech_ms / 5.0) * 5.0
        speech_ms = min(speech_ms, remaining)
        end_ms = min(float(duration_ms), cursor_ms + speech_ms)
        speaker_id = order[speaker_index % len(order)]
        speaker_index += 1
        valence = float(
            np.clip(0.5 + rng.normal(0.0, 0.16) + 0.05 * (speaker_index % 3 - 1), 0.05, 0.95)
        )
        arousal = float(
            np.clip(0.5 + rng.normal(0.0, 0.15) + 0.04 * (turn_index % 2), 0.05, 0.95)
        )
        blocks.append(
            TimelineBlock(
                start_ms=quantize(cursor_ms),
                end_ms=quantize(end_ms),
                speaker_id=speaker_id,
                valence=quantize(valence),
                arousal=quantize(arousal),
                is_speech=True,
            )
        )
        cursor_ms = end_ms
        turn_index += 1

    if not blocks:
        blocks.append(
            TimelineBlock(
                start_ms=0.0,
                end_ms=float(duration_ms),
                speaker_id=speaker_ids[0],
                valence=0.5,
                arousal=0.5,
                is_speech=True,
            )
        )
    if blocks[-1].end_ms < duration_ms:
        blocks.append(
            TimelineBlock(
                start_ms=blocks[-1].end_ms,
                end_ms=float(duration_ms),
                speaker_id=blocks[-1].speaker_id,
                valence=blocks[-1].valence,
                arousal=blocks[-1].arousal,
                is_speech=False,
            )
        )
    return blocks


def synthesize_dialog(
    duration_ms: int,
    speaker_ids: list[str],
    seed: int,
    sample_rate: int,
) -> tuple[np.ndarray, list[TimelineBlock]]:
    blocks = make_timeline(duration_ms, speaker_ids, seed)
    total_samples = int(round(duration_ms * sample_rate / 1000.0))
    speaker_tracks = {
        speaker_id: np.zeros(total_samples, dtype=np.float64)
        for speaker_id in speaker_ids
    }

    for block_index, block in enumerate(blocks):
        if not block.is_speech:
            continue
        start_sample = int(round(block.start_ms * sample_rate / 1000.0))
        end_sample = int(round(block.end_ms * sample_rate / 1000.0))
        count = max(0, end_sample - start_sample)
        if count == 0:
            continue
        rng = np.random.default_rng(
            stable_seed(
                "dialog-block-v1",
                seed,
                block_index,
                block.speaker_id,
                block.start_ms,
            )
        )
        signal = _synth_voiced(
            speaker_voice(block.speaker_id),
            count,
            sample_rate,
            rng,
            block.valence,
            block.arousal,
        )
        speaker_tracks[block.speaker_id][start_sample:end_sample] += signal

    mixed = np.zeros(total_samples, dtype=np.float64)
    for speaker_id in speaker_ids:
        mixed += speaker_tracks[speaker_id]
    peak = float(np.max(np.abs(mixed))) if mixed.size else 0.0
    if peak > 0.92:
        mixed = mixed * (0.92 / peak)
    pcm = np.clip(np.rint(mixed * 32767.0), -32768, 32767).astype("<i2")
    return pcm, blocks


def synthesize_registration(
    speaker_id: str,
    register_seed: int,
    register_ms: int,
    sample_rate: int,
) -> np.ndarray:
    count = int(round(register_ms * sample_rate / 1000.0))
    rng = np.random.default_rng(
        stable_seed("registration-v1", speaker_id, register_seed, register_ms)
    )
    signal = _synth_voiced(
        speaker_voice(speaker_id),
        count,
        sample_rate,
        rng,
        0.5,
        0.5,
    )
    peak = float(np.max(np.abs(signal))) if signal.size else 0.0
    if peak > 0.0:
        signal = signal / peak * 0.78
    return np.clip(np.rint(signal * 32767.0), -32768, 32767).astype("<i2")


def truth_rows_for_variant(
    blocks: list[TimelineBlock],
    frame_ms: float,
    hop_ms: float,
    sample_rate: int,
    duration_ms: int,
) -> list[dict[str, Any]]:
    frame_samples = int(round(frame_ms * sample_rate / 1000.0))
    hop_samples = int(round(hop_ms * sample_rate / 1000.0))
    total_samples = int(round(duration_ms * sample_rate / 1000.0))
    if total_samples < frame_samples or hop_samples <= 0:
        return []
    frame_count = 1 + (total_samples - frame_samples) // hop_samples
    rows: list[dict[str, Any]] = []
    for frame_index in range(frame_count):
        start_sample = frame_index * hop_samples
        midpoint_ms = (start_sample + frame_samples / 2.0) * 1000.0 / sample_rate
        block = _block_at(blocks, midpoint_ms)
        phase = 2.0 * np.pi * 0.73 * midpoint_ms / 1000.0
        valence = float(
            np.clip(block.valence + 0.018 * np.sin(phase) + 0.006 * np.cos(phase * 1.7), 0.0, 1.0)
        )
        arousal = float(
            np.clip(block.arousal + 0.016 * np.cos(phase * 0.83 + 0.4), 0.0, 1.0)
        )
        rows.append(
            {
                "frame_index": frame_index,
                "speaker_id": block.speaker_id,
                "valence": quantize(valence),
                "arousal": quantize(arousal),
                "is_speech": bool(block.is_speech),
                "time_ms": quantize(midpoint_ms),
                "frame_ms": quantize(frame_ms),
                "hop_ms": quantize(hop_ms),
            }
        )
    return rows


def _block_at(blocks: list[TimelineBlock], time_ms: float) -> TimelineBlock:
    for block in blocks:
        if block.start_ms <= time_ms < block.end_ms:
            return block
    return blocks[-1]


def _synth_voiced(
    voice: SpeakerVoice,
    count: int,
    sample_rate: int,
    rng: np.random.Generator,
    valence: float,
    arousal: float,
) -> np.ndarray:
    if count <= 0:
        return np.zeros(0, dtype=np.float64)
    t = np.arange(count, dtype=np.float64) / sample_rate
    vibrato = 0.012 * np.sin(2.0 * np.pi * (4.5 + 1.5 * arousal) * t)
    f0_curve = voice.f0_hz * (1.0 + 0.10 * (valence - 0.5) + vibrato)
    phase = voice.phase_offset + 2.0 * np.pi * np.cumsum(f0_curve) / sample_rate
    signal = np.zeros(count, dtype=np.float64)
    harmonics = range(1, 18)
    for harmonic in harmonics:
        frequency = harmonic * voice.f0_hz * (1.0 + 0.10 * (valence - 0.5))
        amplitude = _formant_amplitude(frequency, voice.formants_hz)
        signal += amplitude * np.sin(harmonic * phase + 0.17 * harmonic)

    syllable_rate = voice.syllable_rate * (0.9 + 0.25 * arousal)
    envelope = 0.32 + 0.68 * np.abs(np.sin(np.pi * syllable_rate * t)) ** 1.35
    fade_samples = max(1, int(0.012 * sample_rate))
    fade = np.ones(count, dtype=np.float64)
    fade_len = min(fade_samples, count // 2)
    if fade_len > 0:
        ramp = np.linspace(0.0, 1.0, fade_len, endpoint=False)
        fade[:fade_len] = ramp
        fade[-fade_len:] = ramp[::-1]
    noise = rng.normal(0.0, 0.012, count)
    gain = voice.gain * (0.86 + 0.20 * valence + 0.22 * arousal)
    signal = gain * envelope * fade * (signal / (np.max(np.abs(signal)) + 1e-12))
    return signal + 0.008 * noise


def _formant_amplitude(
    frequency_hz: float, formants_hz: tuple[float, float, float]
) -> float:
    widths = (150.0, 230.0, 320.0)
    weights = (1.0, 0.72, 0.48)
    amplitude = 0.06
    for formant, width, weight in zip(formants_hz, widths, weights):
        amplitude += weight * np.exp(-0.5 * ((frequency_hz - formant) / width) ** 2)
    return float(amplitude)
