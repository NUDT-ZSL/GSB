#!/usr/bin/env python3
"""Generate deterministic sample WAV segments and manifests for the demo.

Everything is produced with the standard library and constant waveforms, so
re-running this script yields byte-identical inputs.  Run it from the project
root:

    python generate_samples.py            # writes ./demo
    python generate_samples.py --out DIR  # custom location

Then assemble the demos:

    python podcast_assembler.py demo/clean_manifest.json \
        -o demo/out/clean_episode.wav -r demo/out/clean_report.json

    python podcast_assembler.py demo/mixed_manifest.json \
        -o demo/out/mixed_episode.wav -r demo/out/mixed_report.json

    python podcast_assembler.py demo/precision_manifest.json \
        -o demo/out/precision_episode.wav -r demo/out/precision_report.json
"""

from __future__ import annotations

import argparse
import array
import json
import math
import os
import wave
from typing import List, Optional, Sequence

SAMPLE_RATE = 44100


def tone(
    duration_seconds: float, frequency: float, amplitude: int = 250, offset: int = 200
) -> array.array:
    """Deterministic mono tone; the DC offset keeps sample 0 non-zero."""
    frame_count = int(round(duration_seconds * SAMPLE_RATE))
    samples = array.array("h", bytes(frame_count * 2))
    for index in range(frame_count):
        value = offset + round(
            amplitude * math.sin(2.0 * math.pi * frequency * index / SAMPLE_RATE)
        )
        samples[index] = max(-32768, min(32767, value))
    return samples


def write_mono_wav(path: str, samples: array.array) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with wave.open(path, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(SAMPLE_RATE)
        writer.writeframes(samples.tobytes())


def write_stereo_wav(path: str, duration_seconds: float) -> None:
    """A deliberately invalid segment (stereo instead of mono)."""
    frame_count = int(round(duration_seconds * SAMPLE_RATE))
    payload = array.array("h", bytes(frame_count * 4))
    for index in range(frame_count):
        left = 200 + round(250 * math.sin(2.0 * math.pi * 441 * index / SAMPLE_RATE))
        right = 100 + round(120 * math.sin(2.0 * math.pi * 550 * index / SAMPLE_RATE))
        payload[2 * index] = left
        payload[2 * index + 1] = right
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with wave.open(path, "wb") as writer:
        writer.setnchannels(2)
        writer.setsampwidth(2)
        writer.setframerate(SAMPLE_RATE)
        writer.writeframes(payload.tobytes())


def write_manifest(path: str, segments: List[dict]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump({"segments": segments}, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def build_demo(out_dir: Optional[str]) -> str:
    out_dir = os.path.abspath(out_dir or "demo")
    segments_dir = os.path.join(out_dir, "segments")

    clips = {
        "clip_a.wav": tone(1.0, 440),
        "clip_b.wav": tone(1.0, 660),
        "clip_c.wav": tone(1.0, 880),
        "clip_d.wav": tone(1.0, 990),
        "中文片段.wav": tone(0.4, 523),
        "fade_short.wav": tone(0.1, 300, amplitude=120, offset=150),
    }
    for name, samples in clips.items():
        write_mono_wav(os.path.join(segments_dir, name), samples)
    write_stereo_wav(os.path.join(segments_dir, "stereo_clip.wav"), 1.0)

    clean = [
        # Deliberately out of order: the tool must sort by start second.
        {"path": "segments/clip_c.wav", "start": 4.0},
        {"path": "segments/clip_a.wav", "start": 0.0},
        {"path": "segments/clip_b.wav", "start": 2.0},
    ]
    write_manifest(os.path.join(out_dir, "clean_manifest.json"), clean)

    mixed = [
        {"path": "segments/clip_a.wav", "start": 0.0},
        {"path": "segments/clip_a.wav", "start": 0.0},          # duplicate
        {"path": "segments/missing_take.wav", "start": 1.0},    # missing file
        {"path": "segments/stereo_clip.wav", "start": 2.0},     # wrong format
        {                                                        # fade too long
            "path": "segments/fade_short.wav",
            "start": 3.0,
            "fade_in_ms": 60,
            "fade_out_ms": 60,
        },
        {                                                        # valid fades
            "path": "segments/clip_b.wav",
            "start": 4.0,
            "fade_in_ms": 100,
            "fade_out_ms": 100,
        },
        {"path": "segments/clip_d.wav", "start": 4.4},           # overlaps clip_b
        {"path": "segments/中文片段.wav", "start": 5.5},          # UTF-8 path
    ]
    write_manifest(os.path.join(out_dir, "mixed_manifest.json"), mixed)

    precision = [
        {"path": "segments/中文片段.wav", "start": 0.001},
    ]
    write_manifest(os.path.join(out_dir, "precision_manifest.json"), precision)

    # A manifest that must be refused as a whole (negative start).
    invalid = [
        {"path": "segments/clip_a.wav", "start": -1.0},
    ]
    write_manifest(os.path.join(out_dir, "invalid_manifest.json"), invalid)

    return out_dir


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", default="demo", help="output directory (default: demo)"
    )
    args = parser.parse_args(argv)
    out_dir = build_demo(args.out)
    print(f"generated demo inputs in {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
