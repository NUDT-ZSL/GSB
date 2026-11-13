#!/usr/bin/env python3
"""Deterministic demo: build a sample podcast WAV + edit plan, splice it, verify.

Usage:
    python demo.py                 # happy path + checks, writes into demo_out/
    python demo.py --force         # same, replacing files from a previous run

Everything is generated locally; no network and no third-party packages.
"""

from __future__ import annotations

import argparse
import json
import shutil
import struct
import subprocess
import sys
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPLICER = HERE / "wav_splicer.py"
OUT_DIR = HERE / "demo_out"
SAMPLE_RATE = 48000
DURATION_MS = 10_000


def sample_value(frame_index: int) -> int:
    """Deterministic saw-like waveform; every 16-bit value is reproducible."""
    return ((frame_index * 7919 + 12345) % 65536) - 32768


def write_source(path: Path, frames: int, rate: int) -> list[int]:
    samples = [sample_value(index) for index in range(frames)]
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return samples


def read_wav_frames(path: Path) -> tuple[wave._wave_params, bytes]:
    with wave.open(str(path), "rb") as handle:
        params = handle.getparams()
        payload = handle.readframes(handle.getnframes())
    return params, payload


def ms_to_frame(ms: int, rate: int) -> int:
    return ms * rate // 1000


def run_splicer(input_wav: Path, plan: Path, output: Path, report: Path, *extra: str):
    command = [
        sys.executable,
        str(SPLICER),
        "--input", str(input_wav),
        "--plan", str(plan),
        "--output", str(output),
        "--report", str(report),
        *extra,
    ]
    print("$ " + " ".join(f'"{part}"' if " " in part else part for part in command))
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="replace existing demo outputs")
    args = parser.parse_args()

    if OUT_DIR.exists():
        if not args.force:
            print(
                f"{OUT_DIR} already exists; re-run with --force to regenerate it.",
                file=sys.stderr,
            )
            return 1
        shutil.rmtree(OUT_DIR)
    OUT_DIR.mkdir(parents=True)

    source = OUT_DIR / "episode_raw.wav"
    plan = OUT_DIR / "plan.json"
    output = OUT_DIR / "episode_cut.wav"
    report = OUT_DIR / "report.json"

    # --- fixtures -------------------------------------------------------
    write_source(source, SAMPLE_RATE * DURATION_MS // 1000, SAMPLE_RATE)
    segments = [
        {"start_ms": 500, "end_ms": 2500},    # intro
        {"start_ms": 2300, "end_ms": 4000},   # overlaps the intro by 200 ms
        {"start_ms": 7000, "end_ms": 8250},   # later excerpt
    ]
    plan.write_text(json.dumps(segments, indent=2), encoding="utf-8")
    print(f"source : {source}  ({DURATION_MS} ms, {SAMPLE_RATE} Hz, 16-bit mono)")
    print(f"plan   : {plan}  ({len(segments)} segments)")

    # --- run ------------------------------------------------------------
    result = run_splicer(source, plan, output, report)
    print(result.stdout, end="")
    if result.returncode != 0:
        print(result.stderr, end="", file=sys.stderr)
        return result.returncode

    # --- verify ---------------------------------------------------------
    _, source_bytes = read_wav_frames(source)
    params, out_bytes = read_wav_frames(output)

    expected = b""
    expected_frames = 0
    for segment in segments:
        first = ms_to_frame(segment["start_ms"], params.framerate)
        last = ms_to_frame(segment["end_ms"], params.framerate)
        expected += source_bytes[first * 2 : last * 2]
        expected_frames += last - first

    print()
    print(f"output channels={params.nchannels} sampwidth={params.sampwidth} "
          f"rate={params.framerate}")
    print(f"output frames={params.nframes} expected={expected_frames} "
          f"-> {'OK' if params.nframes == expected_frames else 'MISMATCH'}")
    print(f"output duration={params.nframes * 1000 / params.framerate:.3f} ms")
    print(f"byte-identical to source slices: {'OK' if out_bytes == expected else 'MISMATCH'}")

    data = json.loads(report.read_text(encoding="utf-8"))
    print("report:", json.dumps(data, ensure_ascii=False))
    return 0 if params.nframes == expected_frames and out_bytes == expected else 1


if __name__ == "__main__":
    sys.exit(main())
