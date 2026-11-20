"""Deterministic synthetic dialogue speech generation.

Everything is derived from an explicit integer seed and an explicit dialogue
timeline.  The generator never reads the wall clock, iterates dictionaries in
insertion order only after sorting keys, and uses independent ``np.random``
generators created from stable ``SeedSequence`` children so that adding more
dialogues never changes earlier ones.
"""

from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass

import numpy as np

from ..errors import E_DUPLICATE_DIALOG, E_INPUT_INVALID, EngineError

SAMPLE_RATE = 16000

SPEAKER_BASE = {
    # speaker_id: (valence base, arousal base, f0 base, formant shift)
    1: (0.35, 0.55, 140.0, 700.0),
    2: (-0.25, 0.40, 190.0, 800.0),
    3: (0.10, -0.30, 115.0, 650.0),
}


@dataclass(frozen=True)
class SpeechTurn:
    start_ms: float
    end_ms: float
    speaker_id: int
    emotion_hint: str  # "calm" | "excited" | "low"


EMOTION_DELTA = {
    "calm": (0.05, -0.10),
    "excited": (0.35, 0.45),
    "low": (-0.40, -0.35),
}


def _rng(seed: int, *path: int) -> np.random.Generator:
    sequence = np.random.SeedSequence([int(seed), *[int(part) for part in path]])
    return np.random.default_rng(sequence.spawn(1)[0])


def _render_turn(n_samples: int, sample_rate: int, f0: float, formant: float,
                 gain: float, rng: np.random.Generator) -> np.ndarray:
    """Render one voiced turn as a deterministic source/filter style signal."""
    t = np.arange(n_samples, dtype=np.float64) / sample_rate
    period = sample_rate / f0
    phase = np.mod(np.arange(n_samples), period) / period
    # Glottal-like pulse train (fundamental) plus a second-formant harmonic.
    glottis = np.sin(2.0 * np.pi * phase)
    harmonic = 0.45 * np.sin(2.0 * np.pi * phase * 2.0
                            + 0.35 * np.sin(2.0 * np.pi * t * 3.0))
    formant_wave = 0.30 * np.sin(2.0 * np.pi * formant * t)
    jitter = rng.normal(0.0, 0.01, size=n_samples)
    # Smooth deterministic gain envelope (raised-cosine attack/release).
    env = np.ones(n_samples, dtype=np.float64)
    ramp = max(1, min(n_samples // 8, sample_rate // 200))
    window = 0.5 * (1.0 - np.cos(np.pi * np.arange(ramp) / ramp))
    env[:ramp] = window
    env[-ramp:] = window[::-1]
    wave = (glottis + harmonic + formant_wave + jitter) * env * gain
    return wave


def generate_dialog(dialog_id: str, seed: int, duration_ms: float,
                    turns: list[SpeechTurn], sample_rate: int = SAMPLE_RATE,
                    frame_ms: float = 20.0, hop_ms: float = 10.0
                    ) -> tuple[np.ndarray, list[dict]]:
    """Return int16 PCM and the per-frame truth rows for one dialogue."""
    n_samples = int(round(duration_ms * sample_rate / 1000.0))
    pcm = np.zeros(n_samples, dtype=np.float64)

    frame_len = int(round(sample_rate * frame_ms / 1000.0))
    hop_len = int(round(sample_rate * hop_ms / 1000.0))
    n_frames = 1 + max(0, (n_samples - frame_len) // hop_len)

    truth: list[dict] = [None] * n_frames  # type: ignore[list-item]

    for turn_idx, turn in enumerate(sorted(turns, key=lambda x: (x.start_ms, x.end_ms))):
        if turn.speaker_id not in SPEAKER_BASE:
            raise EngineError(E_INPUT_INVALID,
                              f"unknown speaker_id {turn.speaker_id}",
                              item=dialog_id)
        if not (0 <= turn.start_ms < turn.end_ms <= duration_ms):
            raise EngineError(E_INPUT_INVALID,
                              "turn timeline outside dialogue duration",
                              item=f"{dialog_id}:turn[{turn_idx}]")
        s0 = int(round(turn.start_ms * sample_rate / 1000.0))
        s1 = int(round(turn.end_ms * sample_rate / 1000.0))
        base = SPEAKER_BASE[turn.speaker_id]
        rng = _rng(seed, turn_idx)
        f0 = base[2] * (1.0 + 0.05 * rng.normal())
        formant = base[3] * (1.0 + 0.05 * rng.normal())
        gain = 0.55 + 0.15 * rng.normal()
        pcm[s0:s1] += _render_turn(s1 - s0, sample_rate, f0, formant, gain, rng)

    active_turns = sorted(turns, key=lambda x: (x.start_ms, x.end_ms))
    for frame_idx in range(n_frames):
        center_ms = (frame_idx * hop_len + frame_len / 2.0) * 1000.0 / sample_rate
        owner = None
        for turn in active_turns:
            if turn.start_ms <= center_ms < turn.end_ms:
                owner = turn
                break
        if owner is None:
            truth[frame_idx] = {
                "frame_index": frame_idx,
                "speaker_id": -1,
                "valence": 0.0,
                "arousal": 0.0,
                "is_speech": 0,
            }
            continue
        base = SPEAKER_BASE[owner.speaker_id]
        dv, da = EMOTION_DELTA[owner.emotion_hint]
        drift_rng = _rng(seed, 90000 + frame_idx)
        drift = drift_rng.normal(0.0, 0.02, size=2)
        valence = float(np.clip(base[0] + dv + drift[0], -1.0, 1.0))
        arousal = float(np.clip(base[1] + da + drift[1], -1.0, 1.0))
        truth[frame_idx] = {
            "frame_index": frame_idx,
            "speaker_id": owner.speaker_id,
            "valence": valence,
            "arousal": arousal,
            "is_speech": 1,
        }

    peak = float(np.max(np.abs(pcm))) if pcm.size else 0.0
    if peak > 0:
        pcm = pcm / peak * 0.9
    quantized = np.clip(np.round(pcm * 32767.0), -32768.0, 32767.0).astype(np.int16)
    return quantized, truth


def load_dialogue_spec(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as handle:
        spec = json.load(handle)
    if not isinstance(spec, dict) or "dialogues" not in spec:
        raise EngineError(E_INPUT_INVALID,
                          "spec file must contain a 'dialogues' array", item=path)
    return spec["dialogues"]


def generate_corpus(spec_path: str, out_dir: str,
                    sample_rate: int = SAMPLE_RATE,
                    frame_ms: float = 20.0, hop_ms: float = 10.0) -> dict:
    """Generate the full deterministic corpus and persist it to ``out_dir``."""
    dialogues = load_dialogue_spec(spec_path)
    os.makedirs(os.path.join(out_dir, "pcm"), exist_ok=True)
    os.makedirs(os.path.join(out_dir, "truth"), exist_ok=True)

    seen: set[str] = set()
    manifest_rows: list[dict] = []
    for entry in sorted(dialogues, key=lambda d: d["dialog_id"]):
        dialog_id = entry["dialog_id"]
        if dialog_id in seen:
            raise EngineError(E_DUPLICATE_DIALOG,
                              f"duplicate dialog_id {dialog_id}", item=dialog_id)
        seen.add(dialog_id)
        seed = int(entry["seed"])
        duration_ms = float(entry["duration_ms"])
        scene_tag = entry["scene_tag"]
        turns = [SpeechTurn(**turn) for turn in entry["turns"]]

        pcm, truth = generate_dialog(dialog_id, seed, duration_ms, turns,
                                     sample_rate, frame_ms, hop_ms)
        pcm_path = os.path.join(out_dir, "pcm", f"{dialog_id}.pcm")
        truth_path = os.path.join(out_dir, "truth", f"{dialog_id}.csv")
        pcm.tofile(pcm_path)
        with open(truth_path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["frame_index", "speaker_id", "valence",
                            "arousal", "is_speech"])
            writer.writeheader()
            writer.writerows(truth)
        manifest_rows.append({
            "dialog_id": dialog_id,
            "sample_rate": sample_rate,
            "duration_ms": duration_ms,
            "seed": seed,
            "scene_tag": scene_tag,
            "pcm_bytes": int(pcm.nbytes),
            "frame_count": len(truth),
        })

    manifest_path = os.path.join(out_dir, "manifest.jsonl")
    with open(manifest_path, "w", encoding="utf-8") as handle:
        for row in sorted(manifest_rows, key=lambda r: r["dialog_id"]):
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    return {"manifest": manifest_rows, "manifest_path": manifest_path,
            "out_dir": out_dir}
