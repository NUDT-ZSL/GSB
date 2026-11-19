"""Local, deterministic synthetic dialogue generator.

No external audio, weights or services. A fixed-seed ``numpy.random.Generator``
drives per-speaker pitch (f0), formants, speech rate (syllable rate) and energy
envelopes. Turns are placed on a validated timeline and summed (with per-turn
gain) into a mono 16 kHz PCM buffer. A frame-level ground-truth file and a
JSONL dialogue manifest accompany every dialogue.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

import numpy as np

from .config import SAMPLE_RATE
from .errors import BusinessError, ErrorCode
from .framing import frame_count, ms_to_samples


@dataclass(frozen=True)
class TurnSpec:
    speaker_id: int
    start_ms: float
    end_ms: float
    valence: float
    arousal: float
    gain: float = 1.0


def _speaker_profile(rng: np.random.Generator, speaker_id: int) -> dict:
    """Stable acoustic identity: distinct f0 / formants / rate / energy."""
    base_f0 = float(95.0 + 14.0 * speaker_id + rng.uniform(-6.0, 6.0))
    return {
        "speaker_id": speaker_id,
        "base_f0": base_f0,
        "formant_hz": [
            float(650.0 + 70.0 * speaker_id + rng.uniform(-25.0, 25.0)),
            float(1100.0 + 90.0 * speaker_id + rng.uniform(-30.0, 30.0)),
            float(2600.0 + 120.0 * speaker_id + rng.uniform(-40.0, 40.0)),
        ],
        "syllable_hz": float(3.1 + 0.35 * speaker_id + rng.uniform(-0.2, 0.2)),
        "energy_depth": float(0.45 + rng.uniform(0.0, 0.2)),
        "noise_floor": float(0.012 + rng.uniform(0.0, 0.01)),
    }


def _render_turn(
    rng: np.random.Generator,
    length: int,
    profile: dict,
    turn: TurnSpec,
) -> np.ndarray:
    t = np.arange(length, dtype=np.float64) / SAMPLE_RATE
    # Emotion modulates pitch and energy: high arousal -> faster/louder/higher.
    f0 = profile["base_f0"] * (1.0 + 0.18 * turn.arousal + 0.05 * turn.valence)
    syllable_rate = profile["syllable_hz"] * (1.0 + 0.12 * turn.arousal)
    phase0 = rng.uniform(0.0, 2.0 * np.pi)
    syllable = 0.5 * (1.0 + np.cos(2.0 * np.pi * syllable_rate * t + phase0))
    envelope = (1.0 - profile["energy_depth"]) + profile["energy_depth"] * syllable

    phase = 2.0 * np.pi * f0 * t + rng.uniform(0.0, 2.0 * np.pi)
    glottal = np.sin(phase) + 0.35 * np.sin(2.0 * phase)
    colored = np.zeros(length, dtype=np.float64)
    for k, fc in enumerate(profile["formant_hz"], start=1):
        fc_adj = fc * (1.0 + 0.04 * turn.valence + 0.03 * turn.arousal)
        colored += (1.0 / k) * np.sin(
            2.0 * np.pi * fc_adj * t + rng.uniform(0.0, 2.0 * np.pi)
        )
    noise = rng.standard_normal(length) * profile["noise_floor"]
    # 30 ms raised-cosine fade in/out avoids boundary clicks deterministically.
    fade = min(ms_to_samples(30.0, SAMPLE_RATE), length // 2)
    if fade > 0:
        ramp = 0.5 * (1.0 - np.cos(np.pi * np.arange(fade) / fade))
        envelope[:fade] *= ramp
        envelope[-fade:] *= ramp[::-1]
    signal = (0.55 * glottal + 0.30 * colored + noise) * envelope
    return signal * turn.gain


def _validate_timeline(turns: list[TurnSpec], duration_ms: float) -> None:
    cursor = 0.0
    for idx, turn in enumerate(turns):
        if turn.end_ms <= turn.start_ms:
            raise BusinessError(
                ErrorCode.E_CONFIG_INVALID,
                f"turn {idx} end_ms must be greater than start_ms",
                field=f"turns[{idx}].end_ms",
            )
        if turn.start_ms < cursor - 1e-9:
            raise BusinessError(
                ErrorCode.E_CONFIG_INVALID,
                f"turn {idx} overlaps or starts before previous turn end",
                field=f"turns[{idx}].start_ms",
            )
        if turn.end_ms > duration_ms + 1e-9:
            raise BusinessError(
                ErrorCode.E_CONFIG_INVALID,
                f"turn {idx} end_ms {turn.end_ms} exceeds duration {duration_ms}",
                field=f"turns[{idx}].end_ms",
            )
        if not (-1.0 <= turn.valence <= 1.0) or not (-1.0 <= turn.arousal <= 1.0):
            raise BusinessError(
                ErrorCode.E_CONFIG_INVALID,
                f"turn {idx} emotion dimensions must lie in [-1, 1]",
                field=f"turns[{idx}]",
            )
        if turn.gain < 0.0 or turn.gain > 2.0:
            raise BusinessError(
                ErrorCode.E_CONFIG_INVALID,
                f"turn {idx} gain must lie in [0, 2]",
                field=f"turns[{idx}].gain",
            )
        cursor = turn.end_ms


def generate_dialog(
    dialog_id: str,
    turns: list[TurnSpec],
    duration_ms: float,
    seed: int,
    out_dir: str,
    frame_ms: float = 20.0,
    hop_ms: float = 20.0,
) -> dict:
    """Synthesize one dialogue and persist PCM, ground truth and manifest row."""
    if not isinstance(dialog_id, str) or not dialog_id:
        raise BusinessError(
            ErrorCode.E_CONFIG_INVALID, "dialog_id must be a non-empty string",
            field="dialog_id",
        )
    _validate_timeline(turns, duration_ms)
    rng = np.random.default_rng(
        np.random.SeedSequence([int(seed), sum(ord(c) for c in dialog_id)])
    )
    speaker_ids = sorted({turn.speaker_id for turn in turns})
    profiles = [
        _speaker_profile(rng, sid)
        for sid in speaker_ids
    ]
    profile_by_id = {p["speaker_id"]: p for p in profiles}

    total_samples = ms_to_samples(duration_ms, SAMPLE_RATE)
    mix = np.zeros(total_samples, dtype=np.float64)
    for turn in turns:
        start = ms_to_samples(turn.start_ms, SAMPLE_RATE)
        end = ms_to_samples(turn.end_ms, SAMPLE_RATE)
        length = end - start
        if length <= 0:
            raise BusinessError(
                ErrorCode.E_CONFIG_INVALID,
                "turn is shorter than one sample",
                dialog_id=dialog_id,
                field="turns",
            )
        mix[start:end] += _render_turn(
            rng, length, profile_by_id[turn.speaker_id], turn
        )

    peak = float(np.max(np.abs(mix))) if mix.size else 0.0
    if peak > 0:
        mix = mix / peak * 0.9
    pcm16 = np.round(mix * 32767.0).astype(np.int16)

    frame_size = ms_to_samples(frame_ms, SAMPLE_RATE)
    hop_size = ms_to_samples(hop_ms, SAMPLE_RATE)
    n_frames = frame_count(total_samples, frame_size, hop_size)
    gt = np.zeros(
        n_frames,
        dtype=[
            ("frame_index", np.int64),
            ("speaker_id", np.int64),
            ("valence", np.float64),
            ("arousal", np.float64),
            ("is_speech", np.int64),
        ],
    )
    gt["frame_index"] = np.arange(n_frames)
    gt["speaker_id"] = -1
    for turn in turns:
        start = ms_to_samples(turn.start_ms, SAMPLE_RATE)
        end = ms_to_samples(turn.end_ms, SAMPLE_RATE)
        first_frame = max(0, (start - frame_size) // hop_size + 1)
        last_frame = (end - frame_size) // hop_size
        span = np.arange(int(first_frame), int(last_frame) + 1)
        span = span[(span >= 0) & (span < n_frames)]
        if span.size == 0:
            continue
        starts = span * hop_size
        frac = ((starts + frame_size / 2.0) - start) / max(end - start, 1)
        frac = np.clip(frac, 0.0, 1.0)
        gt["speaker_id"][span] = turn.speaker_id
        gt["is_speech"][span] = 1
        gt["valence"][span] = turn.valence + 0.02 * np.sin(2.0 * np.pi * frac)
        gt["arousal"][span] = turn.arousal + 0.02 * np.cos(2.0 * np.pi * frac)

    os.makedirs(out_dir, exist_ok=True)
    pcm_path = os.path.join(out_dir, f"{dialog_id}.pcm")
    gt_path = os.path.join(out_dir, f"{dialog_id}.gt.csv")
    pcm16.tofile(pcm_path)
    header = "frame_index,speaker_id,valence,arousal,is_speech"
    np.savetxt(
        gt_path,
        gt,
        delimiter=",",
        header=header,
        comments="",
        fmt=["%d", "%d", "%.10f", "%.10f", "%d"],
    )
    manifest_row = {
        "dialog_id": dialog_id,
        "sample_rate": SAMPLE_RATE,
        "frame_ms": frame_ms,
        "hop_ms": hop_ms,
        "duration_ms": duration_ms,
        "num_samples": int(total_samples),
        "num_frames": int(n_frames),
        "speaker_ids": speaker_ids,
        "pcm_path": os.path.basename(pcm_path),
        "gt_path": os.path.basename(gt_path),
        "turns": [
            {
                "speaker_id": turn.speaker_id,
                "start_ms": turn.start_ms,
                "end_ms": turn.end_ms,
                "valence": turn.valence,
                "arousal": turn.arousal,
                "gain": turn.gain,
            }
            for turn in turns
        ],
    }
    return manifest_row


def _build_turns(
    rng: np.random.Generator,
    n_speakers: int,
    n_turns: int,
    duration_ms: float,
) -> list[TurnSpec]:
    turns: list[TurnSpec] = []
    gap_ms = 120.0
    usable = duration_ms - gap_ms * (n_turns + 1)
    if usable <= 0:
        raise BusinessError(
            ErrorCode.E_CONFIG_INVALID,
            "duration too short for requested turns and silence gaps",
        )
    slot = usable / n_turns
    cursor = gap_ms
    for k in range(n_turns):
        sid = int(k % n_speakers)
        length = slot * float(rng.uniform(0.7, 0.95))
        start = cursor
        end = cursor + length
        turns.append(
            TurnSpec(
                speaker_id=sid,
                start_ms=round(start, 3),
                end_ms=round(end, 3),
                valence=round(float(rng.uniform(-0.9, 0.9)), 4),
                arousal=round(float(rng.uniform(-0.9, 0.9)), 4),
                gain=round(float(rng.uniform(0.8, 1.15)), 4),
            )
        )
        cursor += length + gap_ms
    return turns


def generate_sample_bundle(
    out_dir: str,
    seed: int = 20261120,
    small_dialogs: int = 3,
    large_dialogs: int = 6,
) -> str:
    """Generate a bundle covering both fine and approximate scale regimes.

    Returns the path to the generated ``manifest.jsonl``.
    """
    os.makedirs(out_dir, exist_ok=True)
    manifest_path = os.path.join(out_dir, "manifest.jsonl")
    rows: list[dict] = []
    rng = np.random.default_rng(np.random.SeedSequence([int(seed), 777]))
    for i in range(small_dialogs):
        turns = _build_turns(rng, n_speakers=2, n_turns=6, duration_ms=6000.0)
        rows.append(
            generate_dialog(
                f"small_{i:02d}", turns, 6000.0, seed, out_dir
            )
        )
    for i in range(large_dialogs):
        turns = _build_turns(rng, n_speakers=4, n_turns=24, duration_ms=30000.0)
        rows.append(
            generate_dialog(
                f"large_{i:02d}", turns, 30000.0, seed, out_dir
            )
        )
    with open(manifest_path, "w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
    return manifest_path
