#!/usr/bin/env python3
"""Independent verification of the demo outputs (standard library only).

Run after the demo steps in README.md / generate_samples.py:

    python verify_demo.py

It re-reads the produced WAV with :mod:`wave` and compares it sample by
sample with the source segments, so it does not trust the assembler itself.
Prints a PASS/FAIL line per criterion and exits non-zero on any failure.
"""

from __future__ import annotations

import array
import json
import os
import sys
import wave
from typing import Dict, List, Tuple

SAMPLE_RATE = 44100
ROOT = os.path.dirname(os.path.abspath(__file__))
DEMO = os.path.join(ROOT, "demo")

failures: List[str] = []


def check(condition: bool, label: str, detail: str = "") -> None:
    if condition:
        print(f"PASS  {label}")
    else:
        print(f"FAIL  {label}" + (f"  ({detail})" if detail else ""))
        failures.append(label)


def read_wav(path: str) -> Tuple[Tuple[int, int, int, int], array.array]:
    with wave.open(path, "rb") as reader:
        params = (
            reader.getnchannels(),
            reader.getsampwidth(),
            reader.getframerate(),
            reader.getnframes(),
        )
        raw = reader.readframes(reader.getnframes())
    samples = array.array("h")
    samples.frombytes(raw)
    if sys.byteorder != "little":
        samples.byteswap()
    return params, samples


def samples_for(seconds: float, rate: int = SAMPLE_RATE) -> int:
    # Mirrors the spec: round(seconds * 44100), half away from zero.
    scaled = seconds * rate
    return int(scaled + 0.5) if scaled >= 0 else -int(-scaled + 0.5)


def verify_clean() -> None:
    print("\n=== clean_manifest.json ===")
    params, out = read_wav(os.path.join(DEMO, "out", "clean_episode.wav"))
    with open(os.path.join(DEMO, "out", "clean_report.json"), encoding="utf-8") as fh:
        report = json.load(fh)
    sources = {
        name: read_wav(os.path.join(DEMO, "segments", name))[1]
        for name in ("clip_a.wav", "clip_b.wav", "clip_c.wav")
    }

    check(params == (1, 2, 44100, 5 * SAMPLE_RATE), "output is 16-bit mono 44.1 kHz, 5.0 s", repr(params))
    order = [entry["path"] for entry in report["successful_segments"]]
    check(
        order == ["segments/clip_a.wav", "segments/clip_b.wav", "segments/clip_c.wav"],
        "report sorted by start even though manifest was shuffled",
        repr(order),
    )
    expected = (
        list(sources["clip_a.wav"])
        + [0] * SAMPLE_RATE                 # gap 1-2 s
        + list(sources["clip_b.wav"])
        + [0] * SAMPLE_RATE                 # gap 3-4 s
        + list(sources["clip_c.wav"])
    )
    check(list(out) == expected, "every sample matches its source clip; gaps are zero")
    check(
        report["silence_intervals"]
        == [
            {"start_seconds": 1.0, "end_seconds": 2.0},
            {"start_seconds": 3.0, "end_seconds": 4.0},
        ],
        "silence intervals are [1,2] and [3,4] for clips at 0/2/4 s",
        repr(report["silence_intervals"]),
    )


def verify_mixed() -> None:
    print("\n=== mixed_manifest.json ===")
    params, out = read_wav(os.path.join(DEMO, "out", "mixed_episode.wav"))
    with open(os.path.join(DEMO, "out", "mixed_report.json"), encoding="utf-8") as fh:
        report = json.load(fh)

    check(params[:3] == (1, 2, 44100), "output keeps 16-bit mono 44.1 kHz", repr(params))
    check(
        params[3] == samples_for(5.5) + samples_for(0.4),
        "duration reaches latest end (5.5 s + 0.4 s)",
        f"{params[3]} samples",
    )

    codes = [issue["code"] for issue in report["issues"]]
    check(
        codes == ["duplicate", "missing", "invalid_format", "fade_too_long", "overlap"],
        "all five problem types recorded in manifest order",
        repr(codes),
    )
    # Criterion 8 is about one manifest *entry* being either accepted or
    # rejected; criterion 13 deliberately has two entries sharing a path, where
    # the second one is rejected.  Compare entries, and for every issue path
    # that also succeeds, verify it is flagged as a duplicate entry.
    success_entries = {(e["manifest_index"], e["path"]) for e in report["successful_segments"]}
    issue_entries = {(i["manifest_index"], i["path"]) for i in report["issues"]}
    check(
        not (success_entries & issue_entries),
        "no manifest entry is both successful and an issue",
        repr(success_entries & issue_entries),
    )
    for issue in report["issues"]:
        if issue["code"] == "duplicate":
            check(
                any(s["path"] == issue["path"] for s in report["successful_segments"]),
                "duplicate issue points at the path of the retained entry",
            )
        else:
            check(
                all(s["path"] != issue["path"] for s in report["successful_segments"]),
                f"{issue['code']} path is absent from successful list",
                issue["path"],
            )
    check(
        report["silence_intervals"]
        == [
            {"start_seconds": 1.0, "end_seconds": 4.0},
            {"start_seconds": 5.0, "end_seconds": 5.5},
        ],
        "silence intervals are [1,4] and [5,5.5]",
        repr(report["silence_intervals"]),
    )

    clip_a = read_wav(os.path.join(DEMO, "segments", "clip_a.wav"))[1]
    clip_b = read_wav(os.path.join(DEMO, "segments", "clip_b.wav"))[1]
    chinese = read_wav(os.path.join(DEMO, "segments", "中文片段.wav"))[1]

    check(list(out[: len(clip_a)]) == list(clip_a), "clip_a occupies 0-1 s exactly once")
    check(
        list(out[SAMPLE_RATE: 4 * SAMPLE_RATE]) == [0] * (3 * SAMPLE_RATE),
        "skipped-segment span 1-4 s is pure silence",
    )
    fade = samples_for(0.1)
    fb = out[4 * SAMPLE_RATE: 4 * SAMPLE_RATE + len(clip_b)]
    check(fb[0] == 0 and fb[-1] == 0, "clip_b starts and ends at 0 (fades applied)")
    check(
        list(fb[fade: len(clip_b) - fade]) == list(clip_b[fade: len(clip_b) - fade]),
        "clip_b samples untouched outside the fade regions",
    )
    check(
        list(out[5 * SAMPLE_RATE + samples_for(0.5):]) == list(chinese),
        "Chinese-named clip assembled at 5.5 s",
    )
    check(
        "中文片段.wav" in open(
            os.path.join(DEMO, "out", "mixed_report.json"), encoding="utf-8"
        ).read(),
        "report is UTF-8 and contains readable Chinese",
    )


def verify_precision() -> None:
    print("\n=== precision_manifest.json (start=0.001) ===")
    params, out = read_wav(os.path.join(DEMO, "out", "precision_episode.wav"))
    source = read_wav(os.path.join(DEMO, "segments", "中文片段.wav"))[1]
    offset = samples_for(0.001)
    check(offset == 44, "0.001 s converts to 44 samples", str(offset))
    check(list(out[:offset]) == [0] * offset, f"first {offset} output samples are silent padding")
    check(out[offset] == source[0], "first valid output sample equals source sample 0")
    check(list(out[offset: offset + len(source)]) == list(source), "the whole clip sits at offset 44")
    check(params[3] == offset + len(source), "total length = offset + clip", f"{params[3]} samples")


def main() -> int:
    for name in ("clean", "mixed", "precision"):
        if not os.path.exists(os.path.join(DEMO, "out", f"{name}_episode.wav")):
            print(f"missing demo output for {name}; run the demo commands first")
            return 2
    verify_clean()
    verify_mixed()
    verify_precision()
    print()
    if failures:
        print(f"{len(failures)} check(s) failed")
        return 1
    print("all demo checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
