from __future__ import annotations

import math
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .util import write_jsonl


@dataclass(frozen=True)
class VoiceProfile:
    speaker_id: str
    f0_hz: float
    formants_hz: tuple[float, float, float]
    bandwidths_hz: tuple[float, float, float]
    speech_rate: float
    gain: float
    phase_seed: int


@dataclass(frozen=True)
class Turn:
    speaker_id: str
    start_ms: int
    end_ms: int
    valence: float
    arousal: float


VOICE_PROFILES: dict[str, VoiceProfile] = {
    "spk_a": VoiceProfile("spk_a", 118.0, (680.0, 1250.0, 2550.0), (110.0, 170.0, 240.0), 1.00, 0.82, 1101),
    "spk_b": VoiceProfile("spk_b", 188.0, (440.0, 1880.0, 2760.0), (95.0, 190.0, 260.0), 1.17, 0.74, 2203),
    "spk_c": VoiceProfile("spk_c", 146.0, (570.0, 1460.0, 2360.0), (105.0, 150.0, 220.0), 0.88, 0.78, 3307),
}


DIALOG_SPECS: list[dict[str, Any]] = [
    {
        "dialog_id": "dlg_001",
        "seed": 20261121,
        "turns": [
            Turn("spk_a", 0, 720, 0.45, 0.28),
            Turn("spk_b", 720, 1480, -0.35, 0.62),
            Turn("spk_c", 1480, 2320, 0.12, -0.15),
            Turn("spk_a", 2320, 3160, -0.55, 0.35),
            Turn("spk_b", 3160, 4000, 0.68, 0.48),
        ],
    },
    {
        "dialog_id": "dlg_002",
        "seed": 20261122,
        "turns": [
            Turn("spk_c", 0, 620, -0.20, -0.40),
            Turn("spk_a", 620, 1320, 0.55, 0.10),
            Turn("spk_b", 1320, 2140, -0.65, 0.70),
            Turn("spk_c", 2140, 2860, 0.30, -0.25),
            Turn("spk_a", 2860, 3600, -0.10, 0.40),
        ],
    },
    {
        "dialog_id": "dlg_003",
        "seed": 20261123,
        "turns": [
            Turn("spk_b", 0, 520, 0.20, 0.55),
            Turn("spk_c", 520, 1200, -0.45, -0.10),
            Turn("spk_a", 1200, 2020, 0.70, 0.25),
            Turn("spk_b", 2020, 2660, -0.30, -0.55),
            Turn("spk_c", 2660, 3200, 0.05, 0.65),
        ],
    },
]


REGISTRY_ROWS: list[dict[str, Any]] = [
    {
        "speaker_id": "spk_a",
        "ref_seed": 7101,
        "ref_duration_ms": 1000,
        "embedding_version": "embed-v1",
    },
    {
        "speaker_id": "spk_b",
        "ref_seed": 7102,
        "ref_duration_ms": 1000,
        "embedding_version": "embed-v1",
    },
    {
        "speaker_id": "spk_c",
        "ref_seed": 7103,
        "ref_duration_ms": 1000,
        "embedding_version": "embed-v1",
    },
]


def _profile_for(speaker_id: str) -> VoiceProfile:
    try:
        return VOICE_PROFILES[speaker_id]
    except KeyError as exc:
        raise ValueError(f"unknown speaker profile {speaker_id}") from exc


def synthesize_utterance(
    speaker_id: str,
    seed: int,
    duration_ms: int,
    valence: float,
    arousal: float,
    sample_rate: int = 16000,
    speech_rate_multiplier: float = 1.0,
) -> np.ndarray:
    profile = _profile_for(speaker_id)
    sample_count = int(round(duration_ms * sample_rate / 1000.0))
    rng = np.random.default_rng((int(seed), profile.phase_seed))
    time = np.arange(sample_count, dtype=np.float64) / sample_rate
    rate = profile.speech_rate * speech_rate_multiplier * (1.0 + 0.08 * arousal)
    vibrato = 0.010 * np.sin(2.0 * math.pi * (4.2 + 0.8 * arousal) * time + 0.35)
    drift = 0.006 * np.sin(2.0 * math.pi * 0.31 * time + 1.1)
    f0 = profile.f0_hz * (1.0 + 0.045 * valence + vibrato + drift)
    phase = 2.0 * math.pi * np.cumsum(f0) / sample_rate
    harmonic_limit = max(1, min(40, int(3900.0 / profile.f0_hz)))
    voiced = np.zeros(sample_count, dtype=np.float64)
    for harmonic in range(1, harmonic_limit + 1):
        frequency = profile.f0_hz * harmonic
        formant_weight = 0.0
        for formant, bandwidth in zip(profile.formants_hz, profile.bandwidths_hz):
            formant_weight += 1.0 / (1.0 + ((frequency - formant) / bandwidth) ** 2)
        amplitude = (0.08 + formant_weight) / (harmonic ** 0.72)
        harmonic_phase = rng.uniform(0.0, 2.0 * math.pi)
        voiced += amplitude * np.sin(harmonic * phase + harmonic_phase)
    syllable_phase = 2.0 * math.pi * rate * 2.7 * time
    envelope = 0.30 + 0.70 * (0.5 + 0.5 * np.sin(syllable_phase + 0.6)) ** 1.4
    envelope *= 0.82 + 0.18 * np.sin(2.0 * math.pi * 0.43 * time + 0.2) ** 2
    breath = rng.normal(0.0, 1.0, sample_count)
    breath = np.convolve(breath, np.ones(7, dtype=np.float64) / 7.0, mode="same")
    consonant_gate = np.maximum(0.0, np.sin(syllable_phase * 1.9 + 1.3)) ** 5
    signal = voiced * envelope + 0.035 * breath * consonant_gate
    energy_factor = 0.82 + 0.18 * (arousal + 1.0) / 2.0
    signal *= profile.gain * energy_factor
    peak = float(np.max(np.abs(signal)))
    if peak > 0:
        signal *= 0.86 / max(0.86, peak)
    fade_samples = min(sample_count // 2, int(0.004 * sample_rate))
    if fade_samples > 0:
        fade = np.linspace(0.0, 1.0, fade_samples, endpoint=False)
        signal[:fade_samples] *= fade
        signal[-fade_samples:] *= fade[::-1]
    return signal


def pcm16_bytes(signal: np.ndarray) -> bytes:
    clipped = np.clip(signal, -32767.0 / 32768.0, 32767.0 / 32768.0)
    return np.asarray(np.round(clipped * 32768.0), dtype="<i2").tobytes()


def synthesize_reference(speaker_id: str, ref_seed: int, duration_ms: int) -> np.ndarray:
    if speaker_id in VOICE_PROFILES:
        return synthesize_utterance(speaker_id, ref_seed, duration_ms, 0.0, 0.0)
    seed_value = int(ref_seed) & 0xFFFFFFFF
    f0 = 100.0 + float(seed_value % 100)
    formants = (
        450.0 + float((seed_value >> 3) % 350),
        1200.0 + float((seed_value >> 7) % 800),
        2300.0 + float((seed_value >> 11) % 700),
    )
    profile = VoiceProfile(
        speaker_id=speaker_id,
        f0_hz=f0,
        formants_hz=formants,
        bandwidths_hz=(100.0, 170.0, 240.0),
        speech_rate=0.9 + float((seed_value >> 13) % 30) / 100.0,
        gain=0.78,
        phase_seed=seed_value % 100000,
    )
    original = VOICE_PROFILES.get(speaker_id)
    VOICE_PROFILES[speaker_id] = profile
    try:
        return synthesize_utterance(speaker_id, ref_seed, duration_ms, 0.0, 0.0)
    finally:
        if original is None:
            VOICE_PROFILES.pop(speaker_id, None)
        else:
            VOICE_PROFILES[speaker_id] = original


def write_wav(path: Path, signal: np.ndarray, sample_rate: int = 16000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm16_bytes(signal))


def _truth_rows(dialog: dict[str, Any], frame_ms: int, frame_shift_ms: int, sample_rate: int) -> list[dict[str, Any]]:
    turns: list[Turn] = dialog["turns"]
    total_ms = max(turn.end_ms for turn in turns)
    frame_samples = int(round(frame_ms * sample_rate / 1000.0))
    shift_samples = int(round(frame_shift_ms * sample_rate / 1000.0))
    total_samples = int(round(total_ms * sample_rate / 1000.0))
    frame_count = 0 if total_samples < frame_samples else (total_samples - frame_samples) // shift_samples + 1
    rows: list[dict[str, Any]] = []
    for frame_index in range(frame_count):
        center_ms = (frame_index * shift_samples + frame_samples / 2.0) / sample_rate * 1000.0
        selected = turns[-1]
        for turn in turns:
            if turn.start_ms <= center_ms < turn.end_ms:
                selected = turn
                break
        phase = frame_index / 17.0
        valence = selected.valence + 0.035 * math.sin(2.0 * math.pi * phase + 0.2)
        valence += 0.012 * math.cos(2.0 * math.pi * frame_index / 29.0 + 1.3)
        arousal = selected.arousal + 0.030 * math.cos(2.0 * math.pi * phase + 0.7)
        arousal += 0.010 * math.sin(2.0 * math.pi * frame_index / 31.0 + 0.5)
        rows.append(
            {
                "frame_index": frame_index,
                "speaker_id": selected.speaker_id,
                "valence": round(float(max(-1.0, min(1.0, valence))), 6),
                "arousal": round(float(max(-1.0, min(1.0, arousal))), 6),
                "is_speech": 1,
            }
        )
    return rows


def _timeline_rows(dialog: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "turn_index": index,
            "speaker_id": turn.speaker_id,
            "start_ms": turn.start_ms,
            "end_ms": turn.end_ms,
            "gain": 0.92 + 0.02 * (index % 3),
            "valence": turn.valence,
            "arousal": turn.arousal,
        }
        for index, turn in enumerate(dialog["turns"])
    ]


def generate_dataset(root: str | Path, force: bool = False) -> dict[str, Any]:
    root = Path(root)
    marker = root / "dataset_manifest.json"
    if marker.exists() and not force:
        from .util import read_json

        return read_json(marker)
    root.mkdir(parents=True, exist_ok=True)
    (root / "audio").mkdir(exist_ok=True)
    (root / "truth").mkdir(exist_ok=True)
    (root / "refs").mkdir(exist_ok=True)
    (root / "timeline").mkdir(exist_ok=True)
    dialog_rows: list[dict[str, Any]] = []
    for dialog in DIALOG_SPECS:
        dialog_id = dialog["dialog_id"]
        turns: list[Turn] = dialog["turns"]
        duration_ms = max(turn.end_ms for turn in turns)
        sample_count = int(round(duration_ms * 16000 / 1000.0))
        signal = np.zeros(sample_count, dtype=np.float64)
        for index, turn in enumerate(turns):
            start = int(round(turn.start_ms * 16000 / 1000.0))
            end = int(round(turn.end_ms * 16000 / 1000.0))
            utterance = synthesize_utterance(
                turn.speaker_id,
                int(dialog["seed"]) + index * 101,
                turn.end_ms - turn.start_ms,
                turn.valence,
                turn.arousal,
            )
            signal[start:end] += (0.92 + 0.02 * (index % 3)) * utterance
        audio_path = root / "audio" / f"{dialog_id}.wav"
        write_wav(audio_path, signal)
        for frame_shift in (20, 10):
            rows = _truth_rows(dialog, 20, frame_shift, 16000)
            write_jsonl(root / "truth" / f"{dialog_id}_hop{frame_shift}ms.jsonl", rows)
        write_jsonl(root / "timeline" / f"{dialog_id}.jsonl", _timeline_rows(dialog))
        speaker_ids = sorted({turn.speaker_id for turn in turns})
        dialog_rows.append(
            {
                "dialog_id": dialog_id,
                "sample_rate": 16000,
                "duration_ms": duration_ms,
                "seed": dialog["seed"],
                "speaker_ids": speaker_ids,
                "audio_path": f"audio/{dialog_id}.wav",
                "truth_path": f"truth/{dialog_id}_hop20ms.jsonl",
                "truth_index": [
                    {
                        "frame_ms": 20,
                        "frame_shift_ms": 20,
                        "path": f"truth/{dialog_id}_hop20ms.jsonl",
                    },
                    {
                        "frame_ms": 20,
                        "frame_shift_ms": 10,
                        "path": f"truth/{dialog_id}_hop10ms.jsonl",
                    },
                ],
            }
        )
    registry_rows: list[dict[str, Any]] = []
    for row in REGISTRY_ROWS:
        reference = synthesize_utterance(
            row["speaker_id"],
            int(row["ref_seed"]),
            int(row["ref_duration_ms"]),
            0.0,
            0.0,
        )
        reference_path = root / "refs" / f"{row['speaker_id']}_{row['ref_seed']}.wav"
        write_wav(reference_path, reference)
        enriched = dict(row)
        enriched["ref_path"] = f"refs/{reference_path.name}"
        enriched["sample_id"] = f"{row['speaker_id']}_{row['ref_seed']}"
        registry_rows.append(enriched)
    write_jsonl(root / "dialog.jsonl", dialog_rows)
    write_jsonl(root / "registry.jsonl", registry_rows)
    write_jsonl(root / "corrections.jsonl", [])
    manifest = {
        "schema_version": "synthetic-dialog-v1",
        "sample_rate": 16000,
        "frame_truth_ms": 20,
        "dialog_count": len(dialog_rows),
        "registry_sample_count": len(registry_rows),
        "dialogs": [row["dialog_id"] for row in dialog_rows],
        "seeds": [row["seed"] for row in dialog_rows],
        "deterministic": True,
    }
    from .util import atomic_write_json

    atomic_write_json(marker, manifest)
    return manifest


def append_registry_sample(root: str | Path, sample: dict[str, Any]) -> dict[str, Any]:
    from .util import read_jsonl

    root = Path(root)
    speaker_id = sample.get("speaker_id")
    if speaker_id not in VOICE_PROFILES:
        raise ValueError(f"unknown speaker_id {speaker_id!r}")
    ref_seed = int(sample["ref_seed"])
    duration_ms = int(sample["ref_duration_ms"])
    reference = synthesize_utterance(speaker_id, ref_seed, duration_ms, 0.0, 0.0)
    reference_path = root / "refs" / f"{speaker_id}_{ref_seed}.wav"
    write_wav(reference_path, reference)
    rows = read_jsonl(root / "registry.jsonl")
    row = {
        "speaker_id": speaker_id,
        "ref_seed": ref_seed,
        "ref_duration_ms": duration_ms,
        "embedding_version": sample.get("embedding_version", "embed-v1"),
        "ref_path": f"refs/{reference_path.name}",
        "sample_id": sample.get("sample_id", f"{speaker_id}_{ref_seed}"),
    }
    rows.append(row)
    write_jsonl(root / "registry.jsonl", rows)
    return row


def append_registry_file(
    dataset_root: str | Path,
    target_path: str | Path,
    sample: dict[str, Any],
    source_path: str | Path | None = None,
) -> dict[str, Any]:
    from .util import read_jsonl

    dataset_root = Path(dataset_root)
    target_path = Path(target_path)
    source_path = Path(source_path) if source_path else dataset_root / "registry.jsonl"
    speaker_id = str(sample.get("speaker_id", ""))
    if speaker_id not in VOICE_PROFILES:
        raise ValueError(f"unknown speaker_id {speaker_id!r}")
    ref_seed = int(sample["ref_seed"])
    duration_ms = int(sample["ref_duration_ms"])
    reference = synthesize_utterance(speaker_id, ref_seed, duration_ms, 0.0, 0.0)
    reference_path = dataset_root / "refs" / f"{speaker_id}_{ref_seed}.wav"
    write_wav(reference_path, reference)
    rows = read_jsonl(source_path)
    row = {
        "speaker_id": speaker_id,
        "ref_seed": ref_seed,
        "ref_duration_ms": duration_ms,
        "embedding_version": sample.get("embedding_version", "embed-v1"),
        "ref_path": f"refs/{reference_path.name}",
        "sample_id": sample.get("sample_id", f"{speaker_id}_{ref_seed}"),
    }
    rows.append(row)
    write_jsonl(target_path, rows)
    return row
