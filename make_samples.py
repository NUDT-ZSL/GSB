#!/usr/bin/env python3
"""Deterministically generate the local demo manifest and WAV fixtures.

Running this script rebuilds ``samples/audio/*.wav`` and
``samples/manifest.json`` from scratch. No external assets are required.

    python make_samples.py

The generated manifest intentionally mixes exported, incomplete and invalid
entries so the tool's three-way reporting can be inspected locally.
"""

from __future__ import annotations

import json
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
SAMPLES_DIR = HERE / "samples"
AUDIO_DIR = SAMPLES_DIR / "audio"
SEAM_WINDOW = 64
SAMPLE_RATE = 44100


def write_mono_wav(path: Path, samples: "np.ndarray") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        out.writeframes(samples.astype("<i2", copy=False).tobytes())


def write_stereo_wav(path: Path, frames: int) -> None:
    rng = np.random.default_rng(99)
    mono = (rng.standard_normal(frames) * 4000).astype("<i2")
    interleaved = np.column_stack([mono, mono]).reshape(-1)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        out.writeframes(interleaved.tobytes())


def seam_audio(
    seed: int,
    duration_ms: int,
    matching_ms: int | None,
    mismatching_ms: tuple[int, ...] = (),
) -> "np.ndarray":
    """Build audio whose head/tail match exactly at ``matching_ms``.

    The tail window at the matching seam is copied from the head window, and
    the head's first/last samples are made equal, so both the waveform shape
    and the instantaneous boundary value line up. Every mismatching candidate
    gets its tail window negated, so its seam difference is large and
    unambiguous. Only the first/last window participates in scoring, so the
    rest is deterministic broadband noise.
    """
    frames = round(duration_ms * SAMPLE_RATE / 1000)
    rng = np.random.default_rng(seed)
    samples = (rng.standard_normal(frames) * 5000).clip(-32768, 32767).astype("<i2")

    head = samples[:SEAM_WINDOW].copy()
    head[0] = head[-1]  # make the head window circular
    samples[:SEAM_WINDOW] = head
    for ms in mismatching_ms:
        index = round(ms * SAMPLE_RATE / 1000)
        samples[index - SEAM_WINDOW : index] = -head
    if matching_ms is not None:
        index = round(matching_ms * SAMPLE_RATE / 1000)
        samples[index - SEAM_WINDOW : index] = head
    return samples


def build() -> dict:
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Clear, perfectly matching seam at 750 ms; 500/880 ms are hostile.
    write_mono_wav(
        AUDIO_DIR / "loop_perfect.wav",
        seam_audio(1, 1000, matching_ms=750, mismatching_ms=(500, 880)),
    )

    # 2. Shorter than its expected 900 ms loop length.
    write_mono_wav(
        AUDIO_DIR / "loop_short.wav",
        seam_audio(2, 600, matching_ms=200, mismatching_ms=(400,)),
    )

    # 3. missing.wav is deliberately never created.

    # 4. Digital silence.
    write_mono_wav(
        AUDIO_DIR / "silent.wav",
        np.zeros(round(1000 * SAMPLE_RATE / 1000), dtype="<i2"),
    )

    # 5. Not WAV data at all, despite the extension.
    (AUDIO_DIR / "broken.wav").write_text("this is not a RIFF/WAVE file\n", encoding="utf-8")

    # 6. Stereo, which violates the mono contract.
    write_stereo_wav(AUDIO_DIR / "stereo.wav", 800 * SAMPLE_RATE // 1000)

    # 7. Valid audio but a candidate seam beyond its duration.
    write_mono_wav(
        AUDIO_DIR / "out_of_range.wav",
        seam_audio(7, 1000, matching_ms=300, mismatching_ms=(200,)),
    )

    # 8/9. One source shared by two entries with different loop lengths.
    write_mono_wav(
        AUDIO_DIR / "shared.wav",
        seam_audio(8, 1200, matching_ms=300, mismatching_ms=(700,)),
    )

    # 10/11. Same candidate lists, different sources, different best seams.
    write_mono_wav(
        AUDIO_DIR / "seam_a.wav",
        seam_audio(10, 600, matching_ms=300, mismatching_ms=(420,)),
    )
    write_mono_wav(
        AUDIO_DIR / "seam_b.wav",
        seam_audio(11, 600, matching_ms=420, mismatching_ms=(300,)),
    )

    manifest = {
        "description": "无缝循环接缝评估本地样例（确定性生成）",
        "output_dir": "out",
        "audio_dir": "audio",
        "entries": [
            {
                "filename": "loop_perfect.wav",
                "expected_loop_ms": 800,
                "candidate_seams_ms": [500, 750, 880],
            },
            {
                "filename": "loop_short.wav",
                "expected_loop_ms": 900,
                "candidate_seams_ms": [200, 400],
            },
            {
                "filename": "missing.wav",
                "expected_loop_ms": 500,
                "candidate_seams_ms": [250],
            },
            {
                "filename": "silent.wav",
                "expected_loop_ms": 500,
                "candidate_seams_ms": [250, 400],
            },
            {
                "filename": "broken.wav",
                "expected_loop_ms": 500,
                "candidate_seams_ms": [200],
            },
            {
                "filename": "stereo.wav",
                "expected_loop_ms": 500,
                "candidate_seams_ms": [300],
            },
            {
                "filename": "out_of_range.wav",
                "expected_loop_ms": 500,
                "candidate_seams_ms": [200, 2000],
            },
            {
                "filename": "shared.wav",
                "expected_loop_ms": 400,
                "candidate_seams_ms": [300],
            },
            {
                "filename": "shared.wav",
                "expected_loop_ms": 800,
                "candidate_seams_ms": [700],
            },
            {
                "filename": "seam_a.wav",
                "expected_loop_ms": 500,
                "candidate_seams_ms": [300, 420],
            },
            {
                "filename": "seam_b.wav",
                "expected_loop_ms": 500,
                "candidate_seams_ms": [300, 420],
            },
            # ---- invalid manifest entries -----------------------------
            {
                "filename": "loop_perfect.wav",
                "candidate_seams_ms": [500],
            },
            {
                "filename": "loop_perfect.wav",
                "expected_loop_ms": "800",
                "candidate_seams_ms": [500],
            },
            {
                "filename": "loop_perfect.wav",
                "expected_loop_ms": 800,
                "candidate_seams_ms": [300, -20],
            },
            {
                "filename": "loop_perfect.wav",
                "expected_loop_ms": 800,
                "candidate_seams_ms": [300.5],
            },
        ],
    }
    (SAMPLES_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> None:
    manifest = build()
    print(f"已生成样例清单: {SAMPLES_DIR / 'manifest.json'}")
    print(f"条目数: {len(manifest['entries'])}")
    print(f"音频目录: {AUDIO_DIR}")


if __name__ == "__main__":
    main()
