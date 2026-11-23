"""Deterministic local synthesis of 16 kHz mono meeting audio.

Everything is derived from integer seeds via hashlib, so the same
(dialog_id, seed, template_version) triple always reproduces the exact
same PCM bytes, ground-truth frames and manifest lines.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass

import numpy as np

from .config import SAMPLE_RATE

EVENT_TYPES = ("knock", "cough", "keyboard", "laugh")
EVENT_TYPE_ID = {name: idx for idx, name in enumerate(EVENT_TYPES)}
TRUTH_FRAME_MS = 20


def _seed_int(*parts) -> int:
    h = hashlib.sha256("::".join(str(p) for p in parts).encode()).hexdigest()
    return int(h[:16], 16)


def _rng(*parts) -> np.random.Generator:
    return np.random.default_rng(_seed_int(*parts))


@dataclass(frozen=True)
class Speaker:
    speaker_id: str
    f0: float
    formants: tuple
    formant_bw: tuple
    energy: float


def make_speakers(seed: int, speaker_ids) -> list:
    speakers = []
    for sid in speaker_ids:
        rng = _rng(seed, "speaker", sid)
        speakers.append(Speaker(
            speaker_id=sid,
            f0=float(rng.uniform(85.0, 255.0)),
            formants=tuple(float(x) for x in rng.uniform(300.0, 3200.0, 3)),
            formant_bw=tuple(float(x) for x in rng.uniform(60.0, 220.0, 3)),
            energy=float(rng.uniform(0.10, 0.30)),
        ))
    return speakers


def _formant_envelope(freqs: np.ndarray, spk: Speaker) -> np.ndarray:
    env = np.zeros_like(freqs)
    for f, bw in zip(spk.formants, spk.formant_bw):
        env += np.exp(-0.5 * ((freqs - f) / bw) ** 2)
    return env + 1e-3


def _voiced_segment(spk: Speaker, n_samples: int, rng: np.random.Generator,
                    sample_rate: int) -> np.ndarray:
    t = np.arange(n_samples) / sample_rate
    f0 = spk.f0 * (1.0 + 0.01 * np.sin(2 * np.pi * 5.0 * t))
    phase = 2 * np.pi * np.cumsum(f0) / sample_rate
    n_harm = 12
    harm_freqs = spk.f0 * np.arange(1, n_harm + 1)
    env = _formant_envelope(harm_freqs, spk)
    env /= env.max()
    sig = np.zeros(n_samples)
    for k in range(n_harm):
        sig += env[k] * np.sin((k + 1) * phase)
    a = max(1, int(0.02 * sample_rate))
    amp = np.ones(n_samples)
    amp[:a] = np.linspace(0.0, 1.0, a)
    amp[-a:] = np.linspace(1.0, 0.0, a)
    pauses = rng.random(n_samples) < 0.002
    amp[pauses] *= 0.2
    return spk.energy * amp * sig


def event_waveform(event_type: str, template_version: str, index: int,
                  sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Deterministic waveform template for one event instance."""
    rng = _rng("template", template_version, event_type, index)
    if event_type == "knock":
        n = int(0.12 * sample_rate)
        t = np.arange(n) / sample_rate
        f = float(rng.uniform(180.0, 320.0))
        w = np.sin(2 * np.pi * f * t) * np.exp(-t * 45.0)
    elif event_type == "cough":
        n = int(0.25 * sample_rate)
        t = np.arange(n) / sample_rate
        noise = rng.standard_normal(n)
        w = noise * np.exp(-t * 18.0) * (t < 0.18)
    elif event_type == "keyboard":
        n = int(0.03 * sample_rate)
        t = np.arange(n) / sample_rate
        f = float(rng.uniform(1800.0, 2600.0))
        w = np.sin(2 * np.pi * f * t) * np.exp(-t * 220.0)
    elif event_type == "laugh":
        n = int(0.5 * sample_rate)
        t = np.arange(n) / sample_rate
        f0 = float(rng.uniform(180.0, 300.0))
        pulse = np.sin(2 * np.pi * f0 * t) + 0.5 * np.sin(4 * np.pi * f0 * t)
        gate = (np.sin(2 * np.pi * 6.0 * t) > 0).astype(float)
        w = pulse * gate * np.exp(-t * 3.0)
    else:
        raise ValueError(f"unknown event type {event_type!r}")
    peak = np.max(np.abs(w))
    return (w / peak if peak > 0 else w).astype(np.float64)


def synth_dialog(dialog_id: str, seed: int, duration_ms: int,
                 speaker_ids, template_version: str,
                 sample_rate: int = SAMPLE_RATE):
    """Return (pcm_int16, events, truth_frames, manifest_entry)."""
    n_samples = duration_ms * sample_rate // 1000
    rng = _rng(seed, "timeline", dialog_id)
    speakers = make_speakers(seed, speaker_ids)
    sig = np.zeros(n_samples)

    pos = 0
    turn = 0
    while pos < n_samples:
        spk = speakers[turn % len(speakers)]
        seg_len = int(rng.uniform(0.6, 2.0) * sample_rate)
        seg_len = min(seg_len, n_samples - pos)
        gain = float(rng.uniform(0.7, 1.0))
        sig[pos:pos + seg_len] += gain * _voiced_segment(
            spk, seg_len, _rng(seed, "seg", dialog_id, turn), sample_rate)
        pos += seg_len + int(rng.uniform(0.05, 0.3) * sample_rate)
        turn += 1

    events = []
    n_events = int(_rng(seed, "nevents", dialog_id).integers(4, 8))
    cursor = int(0.5 * sample_rate)
    for i in range(n_events):
        etype = EVENT_TYPES[i % len(EVENT_TYPES)]
        wave = event_waveform(etype, template_version, i, sample_rate)
        start = cursor + int(_rng(seed, "evpos", dialog_id, i).uniform(
            0.2, 0.8) * sample_rate)
        if start + len(wave) >= n_samples:
            break
        gain = float(_rng(seed, "evgain", dialog_id, i).uniform(0.5, 0.9))
        sig[start:start + len(wave)] += gain * wave
        events.append({
            "event_index": i,
            "event_type": etype,
            "start_ms": start * 1000 // sample_rate,
            "end_ms": (start + len(wave)) * 1000 // sample_rate,
            "gain": round(gain, 6),
        })
        cursor = start + len(wave) + int(0.4 * sample_rate)

    sig += 0.002 * _rng(seed, "floor", dialog_id).standard_normal(n_samples)
    sig = np.clip(sig, -1.0, 1.0)
    pcm = (sig * 32767.0).astype("<i2")

    flen = TRUTH_FRAME_MS * sample_rate // 1000
    truth = []
    for fi, s0 in enumerate(range(0, n_samples - flen + 1, flen)):
        s1 = s0 + flen
        label, is_event = "", 0
        for ev in events:
            e0 = ev["start_ms"] * sample_rate // 1000
            e1 = ev["end_ms"] * sample_rate // 1000
            if s0 < e1 and s1 > e0:
                label, is_event = ev["event_type"], 1
                break
        truth.append({
            "frame_index": fi,
            "event_type": label,
            "is_event": is_event,
            "sample_start": s0,
            "sample_end": s1,
        })

    manifest = {
        "dialog_id": dialog_id,
        "seed": seed,
        "sample_rate": sample_rate,
        "duration_ms": duration_ms,
        "speaker_ids": list(speaker_ids),
        "template_version": template_version,
    }
    return pcm, events, truth, manifest


def write_dataset(out_dir: str, specs) -> str:
    """Synthesize dialogs and write pcm/truth/manifest files."""
    os.makedirs(out_dir, exist_ok=True)
    manifest_path = os.path.join(out_dir, "manifest.jsonl")
    with open(manifest_path, "w", encoding="utf-8") as mf:
        for dialog_id, seed, duration_ms, speaker_ids, tver in specs:
            pcm, events, truth, manifest = synth_dialog(
                dialog_id, seed, duration_ms, speaker_ids, tver)
            pcm.tofile(os.path.join(out_dir, f"{dialog_id}.pcm"))
            with open(os.path.join(out_dir, f"{dialog_id}.events.json"),
                      "w", encoding="utf-8") as fh:
                json.dump(events, fh, indent=1)
            with open(os.path.join(out_dir, f"{dialog_id}.truth.jsonl"),
                      "w", encoding="utf-8") as fh:
                for row in truth:
                    fh.write(json.dumps(row, sort_keys=True) + "\n")
            mf.write(json.dumps(manifest, sort_keys=True) + "\n")
    return manifest_path
