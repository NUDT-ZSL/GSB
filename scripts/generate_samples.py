#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import soundfile as sf


def lcg(seed: int):
    state = seed & 0xFFFFFFFF

    def next_unit() -> float:
        nonlocal state
        state = (1664525 * state + 1013904223) & 0xFFFFFFFF
        return (state / 0xFFFFFFFF) * 2.0 - 1.0

    return next_unit


def synth_reference(utterance: dict, *, sample_rate: int, frame_length: int, frame_shift: int, seed: int) -> np.ndarray:
    total_frames = sum(utterance["duration_frames"])
    output_length = (total_frames - 1) * frame_shift + frame_length
    accumulator = np.zeros(output_length, dtype=np.float64)
    counts = np.zeros(output_length, dtype=np.int64)
    cursor = 0
    for index, phone in enumerate(utterance["phonemes"]):
        duration = utterance["duration_frames"][index]
        base_f0 = utterance["f0_hz"][index]
        base_energy = utterance["energy"][index]
        noise_floor = utterance["noise_floor"][index]
        voicing = utterance["voicing_score"][index]
        next_noise = lcg(seed ^ (index + 1) * 0x9E3779B9)
        for local in range(duration):
            phase = local % 8
            ratio = phase / 7.0
            raw_f0 = base_f0 * (0.98 + 0.04 * ratio)
            energy = base_energy * (0.90 + 0.10 * ratio)
            start = (cursor + local) * frame_shift
            n = np.arange(frame_length, dtype=np.float64) + start
            values = np.zeros(frame_length, dtype=np.float64)
            if voicing >= 0.5 and raw_f0 > 0:
                for harmonic in range(1, 17):
                    if raw_f0 * harmonic > sample_rate / 2:
                        break
                    values += (1.0 / harmonic**1.15) * np.sin(
                        2.0 * math.pi * raw_f0 * harmonic * n / sample_rate
                    )
            noise = np.asarray([next_noise() for _ in range(frame_length)], dtype=np.float64)
            values += noise_floor * 0.30 * noise
            rms = float(np.sqrt(np.mean(values * values)))
            target = max(0.0, min(1.0, energy)) * (10.0 ** (-18.0 / 20.0)) * 32767.0
            if rms > 0:
                values *= target / rms
            window = np.ones(frame_length, dtype=np.float64)
            accumulator[start : start + frame_length] += values * window
            counts[start : start + frame_length] += 1
    counts[counts == 0] = 1
    return np.clip(np.rint(accumulator / counts), -32768, 32767).astype(np.int16)


def build_utterance(identifier: str, phones: list[str], durations: list[int], values: dict, seed: int) -> dict:
    return {
        "utterance_id": identifier,
        "wav": f"{identifier}.wav",
        "phonemes": phones,
        "duration_frames": durations,
        "f0_hz": values["f0_hz"],
        "energy": values["energy"],
        "noise_floor": values["noise_floor"],
        "voicing_score": values["voicing_score"],
        "_seed": seed,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("samples/demo"))
    parser.add_argument("--seed", type=int, default=20261117)
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)

    config = {
        "sample_rate": 16000,
        "frame_length_ms": 20.0,
        "frame_shift_ms": 10.0,
        "f0_min_hz": 70.0,
        "f0_max_hz": 400.0,
        "f0_quantization_hz": 5.0,
        "spectral_bands": 16,
        "voicing_threshold": 0.5,
        "energy_target_dbfs": -18.0,
        "max_batch_frames": 12,
        "concurrency": 3,
        "snapshot_interval_frames": 16,
    }
    (out / "acoustic_config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    utterances = [
        build_utterance(
            "utt_alpha",
            ["AH", "N", "SIL", "IY"],
            [16, 10, 6, 16],
            {
                "f0_hz": [138.0, 116.0, 0.0, 182.0],
                "energy": [0.80, 0.58, 0.03, 0.72],
                "noise_floor": [0.012, 0.030, 0.155, 0.018],
                "voicing_score": [0.94, 0.78, 0.06, 0.91],
            },
            args.seed ^ 0xA1,
        ),
        build_utterance(
            "utt_beta",
            ["M", "AA", "T", "SIL", "UH"],
            [14, 18, 8, 7, 15],
            {
                "f0_hz": [104.0, 158.0, 98.0, 0.0, 132.0],
                "energy": [0.62, 0.84, 0.49, 0.02, 0.68],
                "noise_floor": [0.020, 0.010, 0.070, 0.160, 0.016],
                "voicing_score": [0.76, 0.96, 0.51, 0.05, 0.87],
            },
            args.seed ^ 0xB2,
        ),
    ]

    labels = []
    for utterance in utterances:
        seed = int(utterance.pop("_seed"))
        audio = synth_reference(
            utterance,
            sample_rate=config["sample_rate"],
            frame_length=320,
            frame_shift=160,
            seed=seed,
        )
        sf.write(
            str(out / f"{utterance['utterance_id']}.wav"),
            audio,
            config["sample_rate"],
            subtype="PCM_16",
            format="WAV",
        )
        labels.append(utterance)

    with (out / "labels.jsonl").open("w", encoding="utf-8") as handle:
        for utterance in labels:
            handle.write(json.dumps(utterance, ensure_ascii=False, sort_keys=True) + "\n")

    commands = [
        {"seq": 1, "utterance_id": "utt_alpha", "phoneme_index": 1, "op": "set_f0_scale", "value": 1.04},
        {"seq": 2, "utterance_id": "utt_alpha", "phoneme_index": 3, "op": "set_energy_scale", "value": 1.10},
        {
            "seq": 3,
            "utterance_id": "utt_beta",
            "phoneme_index": 1,
            "op": "replace_frames",
            "start_frame": 2,
            "end_frame": 4,
            "frame_count": 2,
            "frames": [
                {"f0_hz": 156.0, "energy": 0.80, "noise_floor": 0.012, "voicing_score": 0.94},
                {"f0_hz": 160.0, "energy": 0.82, "noise_floor": 0.014, "voicing_score": 0.95},
            ],
        },
        {"seq": 4, "utterance_id": "utt_beta", "phoneme_index": 4, "op": "set_duration", "duration_frames": 16},
    ]
    with (out / "edit_commands.jsonl").open("w", encoding="utf-8") as handle:
        for command in commands:
            handle.write(json.dumps(command, ensure_ascii=False, sort_keys=True) + "\n")

    (out / "generator_seed.json").write_text(
        json.dumps(
            {
                "seed": args.seed,
                "generator": "scripts/generate_samples.py",
                "sample_rate": config["sample_rate"],
                "deterministic_prng": "32-bit LCG",
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
