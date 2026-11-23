"""Deterministic multi-speaker synthetic speech and reference generation."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .model import Dataset, FrameTruth, ReferenceClip, Utterance
from .util import atomic_write_text, jsonl_dumps, sha256_bytes

SAMPLE_RATE = 16000
FRAME_MS = 20.0
FRAME_HOP_MS = 10.0
CONTROLLED_PHONEMES = tuple(range(1, 9))


@dataclass(frozen=True)
class SynthConfig:
    seed: int = 20261124
    speakers: int = 4
    utterances_per_speaker: int = 3
    reference_frames: int = 60
    frame_ms: float = FRAME_MS
    frame_hop_ms: float = FRAME_HOP_MS
    sample_rate: int = SAMPLE_RATE


PHONEME_TABLE: dict[int, dict[str, Any]] = {
    1: {"name": "a", "formants": (730.0, 1090.0, 2440.0), "amp": 1.00, "noise": 0.03},
    2: {"name": "i", "formants": (270.0, 2290.0, 3010.0), "amp": 0.82, "noise": 0.02},
    3: {"name": "u", "formants": (300.0, 870.0, 2240.0), "amp": 0.78, "noise": 0.02},
    4: {"name": "e", "formants": (530.0, 1840.0, 2480.0), "amp": 0.92, "noise": 0.03},
    5: {"name": "o", "formants": (570.0, 840.0, 2410.0), "amp": 0.88, "noise": 0.03},
    6: {"name": "s", "formants": (320.0, 3600.0, 5200.0), "amp": 0.46, "noise": 0.36},
    7: {"name": "m", "formants": (250.0, 1000.0, 2200.0), "amp": 0.58, "noise": 0.02},
    8: {"name": "t", "formants": (400.0, 1800.0, 2600.0), "amp": 0.64, "noise": 0.20},
}


@dataclass(frozen=True)
class SpeakerParams:
    speaker_id: int
    f0_base: float
    formant_shift: float
    energy_gain: float
    phase: float


def _speaker_params(speaker_id: int, seed: int) -> SpeakerParams:
    rng = np.random.default_rng(seed + speaker_id * 104729)
    index = speaker_id - 1
    return SpeakerParams(
        speaker_id=speaker_id,
        f0_base=96.0 + 27.5 * index + float(rng.uniform(-3.0, 3.0)),
        formant_shift=0.90 + 0.045 * index + float(rng.uniform(-0.018, 0.018)),
        energy_gain=0.72 + 0.07 * index + float(rng.uniform(-0.02, 0.02)),
        phase=float(rng.uniform(0.0, 2.0 * math.pi)),
    )


def _timeline_for_utterance(
    rng: np.random.Generator,
    phoneme_ids: tuple[int, ...],
    target_frames: int,
    include_all: bool = False,
) -> tuple[int, ...]:
    timeline: list[int] = []
    if include_all:
        remaining = target_frames
        for position, phoneme in enumerate(phoneme_ids):
            slots_left = len(phoneme_ids) - position
            frames = max(2, remaining // slots_left)
            timeline.extend([phoneme] * frames)
            remaining -= frames
        while len(timeline) < target_frames:
            timeline.append(phoneme_ids[len(timeline) % len(phoneme_ids)])
        return tuple(timeline[:target_frames])
    while len(timeline) < target_frames:
        phoneme = int(phoneme_ids[int(rng.integers(0, len(phoneme_ids)))])
        segment = int(rng.integers(3, 8))
        timeline.extend([phoneme] * segment)
    return tuple(timeline[:target_frames])


def _synthesize_pcm(
    timeline: tuple[int, ...],
    speaker: SpeakerParams,
    sample_rate: int,
    frame_len: int,
    hop: int,
    rng: np.random.Generator,
) -> tuple[np.ndarray, np.ndarray]:
    frame_count = len(timeline)
    sample_count = (frame_count - 1) * hop + frame_len
    sample_time = np.arange(sample_count, dtype=np.float64) / float(sample_rate)
    frame_positions = np.arange(frame_count, dtype=np.float64)
    positions_in_frames = np.arange(sample_count, dtype=np.float64) / float(hop)

    f0_frames = np.empty(frame_count, dtype=np.float64)
    amp_frames = np.empty(frame_count, dtype=np.float64)
    formant_frames = np.empty((frame_count, 3), dtype=np.float64)
    noise_frames = np.empty(frame_count, dtype=np.float64)
    for frame_index, phoneme_id in enumerate(timeline):
        table = PHONEME_TABLE[phoneme_id]
        vibrato = 1.0 + 0.035 * math.sin(
            2.0 * math.pi * frame_index / 23.0 + speaker.phase
        )
        phrase = 0.86 + 0.14 * math.sin(
            2.0 * math.pi * frame_index / max(9.0, frame_count * 0.76) + 0.4
        )
        f0_frames[frame_index] = speaker.f0_base * vibrato
        amp_frames[frame_index] = float(table["amp"]) * speaker.energy_gain * phrase
        formant_frames[frame_index] = np.asarray(table["formants"]) * speaker.formant_shift
        noise_frames[frame_index] = float(table["noise"])

    f0_curve = np.interp(positions_in_frames, frame_positions, f0_frames)
    amp_curve = np.interp(positions_in_frames, frame_positions, amp_frames)
    noise_curve = np.interp(positions_in_frames, frame_positions, noise_frames)
    formant_curves = [
        np.interp(positions_in_frames, frame_positions, formant_frames[:, index])
        for index in range(3)
    ]
    phase = 2.0 * math.pi * np.cumsum(f0_curve) / float(sample_rate) + speaker.phase
    signal = np.zeros(sample_count, dtype=np.float64)
    bandwidths = (90.0, 150.0, 230.0)
    weights = (1.0, 0.68, 0.42)
    for harmonic in range(1, 19):
        harmonic_freq = f0_curve * harmonic
        gain = np.zeros(sample_count, dtype=np.float64)
        for formant_index in range(3):
            distance = (harmonic_freq - formant_curves[formant_index]) / bandwidths[formant_index]
            gain += weights[formant_index] * np.exp(-0.5 * distance * distance)
        gain /= harmonic**0.72
        signal += gain * np.sin(harmonic * phase + harmonic * 0.17)
    noise = rng.standard_normal(sample_count)
    highpassed = noise - np.roll(noise, 1)
    highpassed[0] = 0.0
    signal = amp_curve * signal + noise_curve * amp_curve * highpassed
    peak = float(np.max(np.abs(signal)))
    if peak < 1e-12:
        peak = 1.0
    signal = signal * (0.68 / peak)
    pcm = np.clip(np.rint(signal * 32767.0), -32768, 32767).astype("<i2")
    return pcm, f0_curve


def _frame_truth_from_pcm(
    pcm: np.ndarray,
    f0_curve: np.ndarray,
    timeline: tuple[int, ...],
    speaker_id: int,
    frame_len: int,
    hop: int,
) -> tuple[FrameTruth, ...]:
    pcm_float = pcm.astype(np.float64) / 32768.0
    rows: list[FrameTruth] = []
    for index in range(len(timeline)):
        start = index * hop
        end = start + frame_len
        frame = pcm_float[start:end]
        energy = float(np.mean(frame * frame)) if frame.size else 0.0
        center = min(start + frame_len // 2, f0_curve.size - 1)
        rows.append(
            FrameTruth(
                frame_index=index,
                speaker_id=speaker_id,
                f0_true=float(f0_curve[center]),
                energy_true=energy,
                phoneme_id=int(timeline[index]),
            )
        )
    return tuple(rows)


def _phoneme_prototypes() -> dict[int, dict[str, Any]]:
    output: dict[int, dict[str, Any]] = {}
    for phoneme_id, table in PHONEME_TABLE.items():
        formants = np.asarray(table["formants"], dtype=np.float64)
        output[phoneme_id] = {
            "phoneme_id": phoneme_id,
            "name": table["name"],
            "formants_hz": [float(v) for v in formants],
            "amplitude": float(table["amp"]),
            "noise": float(table["noise"]),
            "signature": [
                float(table["amp"]),
                float(table["noise"]),
                float(formants[0] / 1000.0),
                float(formants[1] / 3500.0),
                float(formants[2] / 5500.0),
            ],
        }
    return output


def synthesize_dataset(config: SynthConfig | None = None) -> Dataset:
    config = config or SynthConfig()
    frame_len = int(round(config.sample_rate * config.frame_ms / 1000.0))
    hop = int(round(config.sample_rate * config.frame_hop_ms / 1000.0))
    speakers = tuple(range(1, config.speakers + 1))
    references: dict[str, ReferenceClip] = {}
    utterances: list[Utterance] = []

    for speaker_id in speakers:
        speaker = _speaker_params(speaker_id, config.seed)
        ref_rng = np.random.default_rng(config.seed + speaker_id * 7919 + 11)
        ref_timeline = _timeline_for_utterance(
            ref_rng,
            CONTROLLED_PHONEMES,
            config.reference_frames,
            include_all=True,
        )
        ref_pcm, _ = _synthesize_pcm(
            ref_timeline, speaker, config.sample_rate, frame_len, hop, ref_rng
        )
        ref_id = f"ref_spk_{speaker_id:02d}_v1"
        references[ref_id] = ReferenceClip(
            ref_id=ref_id,
            speaker_id=speaker_id,
            version=1,
            clone_seed=config.seed + speaker_id * 101,
            sample_rate=config.sample_rate,
            pcm=ref_pcm,
            content_hash=sha256_bytes(ref_pcm.tobytes()),
            shape=tuple(ref_pcm.shape),
        )

        for utterance_index in range(1, config.utterances_per_speaker + 1):
            utterance_seed = (
                config.seed
                + speaker_id * 100003
                + utterance_index * 1009
                + 37
            )
            rng = np.random.default_rng(utterance_seed)
            target_frames = int(rng.integers(32, 49))
            timeline = _timeline_for_utterance(
                rng,
                CONTROLLED_PHONEMES,
                target_frames,
                include_all=(utterance_index == 1),
            )
            pcm, f0_curve = _synthesize_pcm(
                timeline, speaker, config.sample_rate, frame_len, hop, rng
            )
            truth = _frame_truth_from_pcm(
                pcm, f0_curve, timeline, speaker_id, frame_len, hop
            )
            utterance_id = f"utt_s{speaker_id:02d}_{utterance_index:03d}"
            utterances.append(
                Utterance(
                    utterance_id=utterance_id,
                    speaker_id=speaker_id,
                    ref_id=ref_id,
                    sample_rate=config.sample_rate,
                    duration_ms=round(len(pcm) * 1000.0 / config.sample_rate, 6),
                    phoneme_count=len(set(timeline)),
                    clone_seed=utterance_seed,
                    pcm=pcm,
                    truth=truth,
                    timeline=timeline,
                )
            )

    return Dataset(
        seed=config.seed,
        sample_rate=config.sample_rate,
        frame_ms=config.frame_ms,
        frame_hop_ms=config.frame_hop_ms,
        controlled_phonemes=CONTROLLED_PHONEMES,
        speakers=speakers,
        utterances=tuple(utterances),
        references=references,
        phoneme_prototypes=_phoneme_prototypes(),
    )


def synthesize_registration_clip(
    speaker_id: int,
    ref_id: str,
    version: int,
    seed: int,
    frame_count: int = 60,
    sample_rate: int = SAMPLE_RATE,
) -> ReferenceClip:
    speaker = _speaker_params(speaker_id, seed)
    rng = np.random.default_rng(seed + speaker_id * 3571 + version * 1013)
    frame_len = int(round(sample_rate * FRAME_MS / 1000.0))
    hop = int(round(sample_rate * FRAME_HOP_MS / 1000.0))
    timeline = _timeline_for_utterance(
        rng, CONTROLLED_PHONEMES, frame_count, include_all=True
    )
    pcm, _ = _synthesize_pcm(timeline, speaker, sample_rate, frame_len, hop, rng)
    return ReferenceClip(
        ref_id=ref_id,
        speaker_id=speaker_id,
        version=version,
        clone_seed=seed + speaker_id * 101 + version * 17,
        sample_rate=sample_rate,
        pcm=pcm,
        content_hash=sha256_bytes(pcm.tobytes()),
        shape=tuple(pcm.shape),
    )


def write_dataset_artifacts(dataset: Dataset, root: Path) -> dict[str, Any]:
    sample_root = root / "samples"
    pcm_root = sample_root / "pcm"
    ref_root = sample_root / "references"
    pcm_root.mkdir(parents=True, exist_ok=True)
    ref_root.mkdir(parents=True, exist_ok=True)
    manifest_rows: list[dict[str, Any]] = []
    for utterance in dataset.utterances:
        pcm_path = pcm_root / f"{utterance.utterance_id}.pcm"
        pcm_path.write_bytes(utterance.pcm.astype("<i2").tobytes())
        manifest_rows.append(utterance.manifest_row())
    (sample_root / "manifest.jsonl").write_text(
        jsonl_dumps(manifest_rows), encoding="utf-8"
    )
    (sample_root / "truth.jsonl").write_text(
        jsonl_dumps(dataset.truth_rows()), encoding="utf-8"
    )
    ref_rows: list[dict[str, Any]] = []
    for ref in dataset.references.values():
        ref_path = ref_root / f"{ref.ref_id}.pcm"
        ref_path.write_bytes(ref.pcm.astype("<i2").tobytes())
        ref_rows.append(
            {
                "ref_id": ref.ref_id,
                "speaker_id": ref.speaker_id,
                "version": ref.version,
                "clone_seed": ref.clone_seed,
                "sample_rate": ref.sample_rate,
                "shape": list(ref.shape),
                "content_hash": ref.content_hash,
                "pcm_path": f"references/{ref.ref_id}.pcm",
            }
        )
    (sample_root / "references.jsonl").write_text(
        jsonl_dumps(ref_rows), encoding="utf-8"
    )
    summary = {
        "dataset_seed": dataset.seed,
        "sample_rate": dataset.sample_rate,
        "frame_ms": dataset.frame_ms,
        "frame_hop_ms": dataset.frame_hop_ms,
        "speaker_count": len(dataset.speakers),
        "utterance_count": len(dataset.utterances),
        "total_frames": dataset.total_frames,
        "controlled_phonemes": list(dataset.controlled_phonemes),
        "manifest": "samples/manifest.jsonl",
        "truth": "samples/truth.jsonl",
        "references": "samples/references.jsonl",
        "dataset_digest": hashlib.sha256(
            "".join(utt.manifest_row()["utterance_id"] for utt in dataset.utterances).encode()
            + b"".join(utt.pcm.tobytes() for utt in dataset.utterances)
        ).hexdigest(),
    }
    atomic_write_text(
        sample_root / "dataset_summary.json",
        json.dumps(summary, ensure_ascii=False, indent=2),
    )
    return summary
