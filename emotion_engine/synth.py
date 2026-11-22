"""Deterministic synthetic multi-speaker dialog and ground-truth generation."""

from __future__ import annotations

import json
import math
import wave
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from .config import EngineConfig
from .features import complete_frame_count
from .util import clamp


DIALOG_SPECS: tuple[dict[str, Any], ...] = (
    {
        "dialog_id": "dialog_0001",
        "duration_ms": 1800,
        "chunk_ms": 80,
        "turns": (
            ("spk_a", 0, 680, 0.88, 0.10, 0.08),
            ("spk_b", 720, 1200, 0.92, -0.12, 0.12),
            ("spk_c", 1160, 1520, 0.82, 0.18, 0.05),
            ("spk_a", 1480, 1800, 0.76, -0.04, 0.02),
        ),
    },
    {
        "dialog_id": "dialog_0002",
        "duration_ms": 1560,
        "chunk_ms": 60,
        "turns": (
            ("spk_c", 0, 420, 0.80, -0.20, 0.15),
            ("spk_a", 380, 900, 0.90, 0.16, 0.04),
            ("spk_b", 860, 1260, 0.84, -0.08, 0.18),
            ("spk_c", 1220, 1560, 0.78, 0.24, -0.02),
        ),
    },
)


def _speaker_catalog(rng: np.random.Generator, speaker_ids: Sequence[str]) -> list[dict[str, Any]]:
    speakers: list[dict[str, Any]] = []
    for speaker_id in speaker_ids:
        base_f0 = float(rng.uniform(105.0, 225.0))
        speakers.append(
            {
                "speaker_id": speaker_id,
                "base_f0_hz": round(base_f0, 6),
                "formants_hz": [
                    round(float(rng.uniform(450.0, 850.0)), 6),
                    round(float(rng.uniform(1050.0, 1900.0)), 6),
                    round(float(rng.uniform(2300.0, 3200.0)), 6),
                ],
                "formant_bandwidths_hz": [
                    round(float(rng.uniform(90.0, 150.0)), 6),
                    round(float(rng.uniform(120.0, 220.0)), 6),
                    round(float(rng.uniform(180.0, 300.0)), 6),
                ],
                "vibrato_phase": round(float(rng.uniform(0.0, 2.0 * math.pi)), 9),
                "syllable_phase": round(float(rng.uniform(0.0, 2.0 * math.pi)), 9),
                "base_valence": round(float(rng.uniform(-0.45, 0.45)), 6),
                "base_arousal": round(float(rng.uniform(0.05, 0.55)), 6),
            }
        )
    return speakers


def _render_speaker(
    speaker: Mapping[str, Any],
    turns: Sequence[Sequence[Any]],
    time: np.ndarray,
    sample_rate: int,
) -> np.ndarray:
    output = np.zeros(time.shape[0], dtype=np.float64)
    base_f0 = float(speaker["base_f0_hz"])
    formants = [float(item) for item in speaker["formants_hz"]]
    bandwidths = [float(item) for item in speaker["formant_bandwidths_hz"]]
    vibrato_phase = float(speaker["vibrato_phase"])
    syllable_phase = float(speaker["syllable_phase"])

    for turn_speaker, start_ms, end_ms, _gain, _valence, _arousal in turns:
        if str(turn_speaker) != str(speaker["speaker_id"]):
            continue
        start = round(int(start_ms) * sample_rate / 1000)
        end = round(int(end_ms) * sample_rate / 1000)
        if end <= start:
            continue
        local_time = time[start:end] - time[start]
        f0 = base_f0 * (
            1.0
            + 0.018 * np.sin(2.0 * math.pi * 0.73 * local_time + vibrato_phase)
            + 0.006 * np.sin(2.0 * math.pi * 2.13 * local_time + 0.37 * vibrato_phase)
        )
        phase = np.cumsum(2.0 * math.pi * f0 / sample_rate)
        signal = np.zeros(end - start, dtype=np.float64)
        for harmonic in range(1, 9):
            harmonic_hz = harmonic * f0
            formant_weight = np.zeros_like(harmonic_hz)
            for formant, bandwidth in zip(formants, bandwidths):
                formant_weight += np.exp(
                    -0.5 * np.square((harmonic_hz - formant) / bandwidth)
                )
            amplitude = (0.22 + 0.78 * formant_weight) / harmonic
            signal += amplitude * np.cos(harmonic * phase + 0.11 * harmonic * vibrato_phase)
        duration = (end - start) / sample_rate
        attack = np.minimum(1.0, local_time / 0.018)
        release = np.minimum(1.0, np.maximum(0.0, (duration - local_time) / 0.024))
        syllable = 0.58 + 0.42 * np.square(
            np.sin(math.pi * 3.2 * local_time + syllable_phase)
        )
        envelope = np.clip(attack, 0.0, 1.0) * np.clip(release, 0.0, 1.0) * syllable
        output[start:end] += signal * envelope
    return output


def _dominant_turn(
    turns: Sequence[Sequence[Any]],
    time_seconds: float,
) -> tuple[Sequence[Any] | None, list[Sequence[Any]]]:
    active = [
        turn
        for turn in turns
        if float(turn[1]) / 1000.0 <= time_seconds < float(turn[2]) / 1000.0
    ]
    if not active:
        return None, active
    dominant = max(active, key=lambda turn: (float(turn[3]), -float(turn[1])))
    return dominant, active


def _write_wav(path: Path, samples: np.ndarray, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(samples.astype("<i2", copy=False).tobytes())


def _write_jsonl(path: Path, records: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(
                json.dumps(
                    record,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            )
            handle.write("\n")


def synthesize_dialog(
    spec: Mapping[str, Any],
    seed: int,
    config: EngineConfig,
    output_dir: str | Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    output_root = Path(output_dir)
    sample_rate = 16000
    duration_ms = int(spec["duration_ms"])
    sample_count = round(duration_ms * sample_rate / 1000)
    frame_samples = config.frame_samples(sample_rate)
    hop_samples = config.hop_samples(sample_rate)
    if sample_count % hop_samples:
        raise ValueError("dialog duration must align to the hop grid")
    rng = np.random.default_rng(seed)
    speaker_ids = sorted({str(turn[0]) for turn in spec["turns"]})
    catalog = _speaker_catalog(rng, speaker_ids)
    by_id = {str(item["speaker_id"]): item for item in catalog}
    time = np.arange(sample_count, dtype=np.float64) / sample_rate

    mix = np.zeros(sample_count, dtype=np.float64)
    for speaker in catalog:
        signal = _render_speaker(speaker, spec["turns"], time, sample_rate)
        gains = [
            float(turn[3])
            for turn in spec["turns"]
            if str(turn[0]) == str(speaker["speaker_id"])
        ]
        mix += signal * (max(gains) if gains else 0.0)

    peak = float(np.max(np.abs(mix))) if mix.size else 0.0
    scale = (0.72 * 32767.0 / peak) if peak > 1e-12 else 0.0
    pcm = np.rint(mix * scale).astype(np.int16)
    wav_relative = f"{spec['dialog_id']}.wav"
    truth_relative = f"truth/{spec['dialog_id']}.jsonl"
    _write_wav(output_root / wav_relative, pcm, sample_rate)

    truth: list[dict[str, Any]] = []
    last_speaker = speaker_ids[0]
    total_frames = complete_frame_count(sample_count, frame_samples, hop_samples)
    for frame_index in range(total_frames):
        midpoint_seconds = (frame_index * hop_samples + frame_samples / 2.0) / sample_rate
        dominant, active = _dominant_turn(spec["turns"], midpoint_seconds)
        if dominant is None:
            speaker_id = last_speaker
            valence = 0.0
            arousal = 0.0
            is_speech = False
        else:
            speaker_id = str(dominant[0])
            last_speaker = speaker_id
            speaker = by_id[speaker_id]
            valence = (
                float(speaker["base_valence"])
                + float(dominant[4])
                + 0.06 * math.sin(2.0 * math.pi * 0.37 * midpoint_seconds)
            )
            arousal = (
                float(speaker["base_arousal"])
                + float(dominant[5])
                + 0.05 * math.sin(2.0 * math.pi * 0.53 * midpoint_seconds + 0.4)
            )
            is_speech = True
        truth.append(
            {
                "frame_index": frame_index,
                "speaker_id": speaker_id,
                "valence": round(clamp(valence, -1.0, 1.0), 6),
                "arousal": round(clamp(arousal, -1.0, 1.0), 6),
                "is_speech": bool(is_speech),
            }
        )
    _write_jsonl(output_root / truth_relative, truth)
    manifest_entry = {
        "dialog_id": str(spec["dialog_id"]),
        "sample_rate": sample_rate,
        "duration_ms": duration_ms,
        "chunk_ms": int(spec["chunk_ms"]),
        "tier_schema": config.tier_schema,
        "speakers": speaker_ids,
        "sample_count": sample_count,
        "seed": int(seed),
        "wav_path": wav_relative,
        "truth_path": truth_relative,
    }
    speaker_record = {
        "dialog_id": str(spec["dialog_id"]),
        "seed": int(seed),
        "speakers": catalog,
        "timeline": [
            {
                "speaker_id": str(turn[0]),
                "start_ms": int(turn[1]),
                "end_ms": int(turn[2]),
                "gain": float(turn[3]),
                "valence_offset": float(turn[4]),
                "arousal_offset": float(turn[5]),
            }
            for turn in spec["turns"]
        ],
    }
    return manifest_entry, speaker_record


def generate_samples(
    output_dir: str | Path,
    config: EngineConfig,
    seed: int = 20261123,
) -> list[dict[str, Any]]:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, Any]] = []
    catalog: list[dict[str, Any]] = []
    for index, spec in enumerate(DIALOG_SPECS):
        entry, speaker_record = synthesize_dialog(
            spec,
            seed + index * 1009,
            config,
            root,
        )
        manifest.append(entry)
        catalog.append(speaker_record)
    _write_jsonl(root / "dialogs.jsonl", manifest)
    _write_jsonl(root / "speakers.jsonl", catalog)
    return manifest
