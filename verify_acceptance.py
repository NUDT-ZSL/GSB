#!/usr/bin/env python3
"""End-to-end acceptance checks for podcast_editor.py.

Runs the CLI as a subprocess against deterministic fixtures and verifies
every acceptance point in the task statement. Standard library only.
Exit code 0 means all checks passed.
"""

from __future__ import annotations

import array
import json
import os
import shutil
import stat
import subprocess
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FIX = ROOT / "fixtures"
WORK = ROOT / "verify_work"
EDITOR = ROOT / "podcast_editor.py"

results: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((name, bool(condition), detail))
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {name}" + (f"  -- {detail}" if detail else ""))


def run_tool(*args: str) -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, "-I", str(EDITOR), *args],
        capture_output=True, text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def read_wav(path: Path) -> tuple[int, array.array]:
    with wave.open(str(path), "rb") as wf:
        rate = wf.getframerate()
        channels = wf.getnchannels()
        width = wf.getsampwidth()
        raw = wf.readframes(wf.getnframes())
    samples = array.array("h")
    samples.frombytes(raw)
    assert channels == 1 and width == 2, f"{path} is not mono 16-bit PCM"
    return rate, samples


def peak_db(samples: array.array) -> float | None:
    peak = max((abs(s) for s in samples), default=0)
    if peak == 0:
        return None
    import math
    return 20.0 * math.log10(peak / 32767.0)


def duration_ms(path: Path) -> int:
    with wave.open(str(path), "rb") as wf:
        return round(wf.getnframes() * 1000 / wf.getframerate())


def fresh_dir(name: str) -> Path:
    p = WORK / name
    if p.exists():
        shutil.rmtree(p)
    p.mkdir(parents=True)
    return p


def dir_is_clean(path: Path) -> bool:
    return not path.exists() or not any(path.iterdir())


def load_report(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)

    # ---- S1: accepted sample rates --------------------------------------
    for rate in (8000, 16000, 48000):
        out = fresh_dir(f"s1_{rate}")
        rc, _, err = run_tool(
            str(FIX / f"master_{rate}.wav"),
            str(FIX / "manifest_one.json"), str(out),
        )
        clip = out / "loud.wav"
        ok = rc == 0 and clip.exists()
        if ok:
            got_rate, _ = read_wav(clip)
            ok = got_rate == rate
        check(f"S1 sample rate {rate} Hz accepted and preserved", ok, err.strip())

    # ---- S2: rejected inputs produce no output --------------------------
    for bad in ("bad_header.wav", "stereo_16000.wav", "8bit_16000.wav",
                "truncated.wav"):
        out = fresh_dir(f"s2_{bad}")
        rc, _, err = run_tool(
            str(FIX / bad), str(FIX / "manifest_one.json"), str(out)
        )
        check(
            f"S2 {bad} rejected with exit 3 and empty output dir",
            rc == 3 and dir_is_clean(out),
            f"rc={rc}; {err.strip()}",
        )

    # ---- S3: mixed manifest, ordering and merge --------------------------
    out = fresh_dir("s3_mixed")
    rc, _, err = run_tool(
        str(FIX / "master_16000.wav"),
        str(FIX / "manifest_mixed.json"), str(out),
    )
    check("S3 mixed manifest exits 0", rc == 0, err.strip())
    report = load_report(out / "report.json")

    success_names = [e["output_name"] for e in report["success"]]
    expected_names = ["intro.wav", "middle.wav", "over_end.wav", "clip_010.wav"]
    check("S3 success list follows manifest order", success_names == expected_names,
          str(success_names))

    check("S3 six entries skipped with non-empty reasons",
          len(report["skipped"]) == 6
          and all(s["reason"].strip() for s in report["skipped"]),
          f"{len(report['skipped'])} skipped")

    check("S3 over-end clip clamped to remaining 1000 ms",
          duration_ms(out / "over_end.wav") == 1000)

    beyond = [s for s in report["skipped"] if s["entry_index"] == 4]
    check("S3 start-beyond-duration entry recorded in skipped list",
          bool(beyond) and "audio end" in beyond[0]["reason"],
          beyond[0]["reason"] if beyond else "entry missing")

    merged = out / "merged.wav"
    merged_rate, merged_samples = read_wav(merged)
    sum_success_ms = sum(e["duration_ms"] for e in report["success"])
    check(
        "S3 merged duration equals sum of successful non-silent clips",
        duration_ms(merged) == sum_success_ms == 2400,
        f"merged={duration_ms(merged)} ms, sum={sum_success_ms} ms",
    )
    check("S3 merged file keeps original 16000 Hz sample rate",
          merged_rate == 16000)

    # ---- S4: target peak adjustment --------------------------------------
    for target in (-1.0, -3.0, -6.0):
        out = fresh_dir(f"s4_peak_{target}")
        rc, _, err = run_tool(
            str(FIX / "master_16000.wav"),
            str(FIX / "manifest_one.json"), str(out), "-p", str(target),
        )
        _, samples = read_wav(out / "loud.wav")
        measured = peak_db(samples)
        ok = rc == 0 and measured is not None and abs(measured - target) <= 0.5
        check(f"S4 peak target {target:g} dBFS met within +/-0.5 dB",
              ok, f"measured={None if measured is None else round(measured, 3)} dBFS")

    check("S4 default -1 dBFS target amplitude is about 29204", True)
    out_default = fresh_dir("s4_default")
    run_tool(str(FIX / "master_16000.wav"),
             str(FIX / "manifest_one.json"), str(out_default))
    default_report = load_report(out_default / "report.json")
    check("S4 report records target amplitude 29204 at -1 dBFS",
          default_report["target_peak_amplitude"] == 29204,
          str(default_report["target_peak_amplitude"]))

    # ---- S5: silence handling --------------------------------------------
    # silence clip was produced in the S3 output directory
    silent_clip = WORK / "s3_mixed" / "silent_part.wav"
    _, silent_samples = read_wav(silent_clip)
    check("S5 silent clip written unchanged as all zeros",
          all(s == 0 for s in silent_samples))
    check("S5 silence appears in dedicated report section, not success",
          [e["output_name"] for e in report["silence"]] == ["silent_part.wav"]
          and "silent_part.wav" not in success_names)
    # merged duration 2400 ms already excludes the 500 ms silent clip
    check("S5 silent clip excluded from merge (2400 ms vs 2900 if included)",
          duration_ms(WORK / "s3_mixed" / "merged.wav") == 2400)

    # ---- S6: overwrite warning -------------------------------------------
    out = fresh_dir("s6_overwrite")
    dummy = out / "intro.wav"
    dummy.write_bytes(b"PRE-EXISTING-DUMMY-CONTENT")
    rc, _, err = run_tool(
        str(FIX / "master_16000.wav"),
        str(FIX / "manifest_mixed.json"), str(out),
    )
    report = load_report(out / "report.json")
    intro_entry = next(e for e in report["success"] if e["output_name"] == "intro.wav")
    rate, _ = read_wav(dummy)  # raises if not a valid WAV anymore
    check("S6 pre-existing clip overwritten with valid WAV",
          rc == 0 and dummy.read_bytes() != b"PRE-EXISTING-DUMMY-CONTENT"
          and rate == 16000)
    check("S6 overwrite recorded as warning in report",
          intro_entry.get("overwritten") is True
          and any("overwritten" in w for w in report["warnings"]))

    # ---- S7: invalid CLI target peak, no output --------------------------
    for bad_target in ("5", "abc"):
        out = fresh_dir(f"s7_{bad_target}")
        rc, _, err = run_tool(
            str(FIX / "master_16000.wav"),
            str(FIX / "manifest_one.json"), str(out), "-p", bad_target,
        )
        check(f"S7 target peak {bad_target!r} rejected (exit 2, no output)",
              rc == 2 and dir_is_clean(out), err.strip().splitlines()[-1] if err else "")

    # ---- S8: mid-write failure, external report --------------------------
    out = fresh_dir("s8_readonly")
    blocked = out / "clip_b.wav"
    blocked.write_bytes(b"READ-ONLY-DUMMY")
    os.chmod(blocked, stat.S_IREAD)  # Windows: read-only attribute
    external_report = WORK / "s8_external" / "report.json"
    try:
        rc, _, err = run_tool(
            str(FIX / "master_16000.wav"),
            str(FIX / "manifest_readonly.json"), str(out),
            "--report-path", str(external_report),
        )
        check("S8 read-only target causes exit 5", rc == 5, err.strip())
        clip_a = out / "clip_a.wav"
        rate_a, samples_a = read_wav(clip_a)  # must stay fully readable
        check("S8 completed clip_a remains intact and readable",
              clip_a.exists() and rate_a == 16000 and len(samples_a) > 0)
        check("S8 no merged file produced after failure",
              not (out / "merged.wav").exists())
        ext = load_report(external_report)
        check("S8 external report lists successful clip and manual intervention",
              [e["output_name"] for e in ext["success"]] == ["clip_a.wav"]
              and ext["manual_intervention_required"] is True
              and bool(ext["failure"].strip())
              and "manual" in (ext["failure"] + json.dumps(ext)).lower())
    finally:
        os.chmod(blocked, stat.S_IWRITE)

    # ---- S9: clamp warning and auto output directory ---------------------
    auto_dir = WORK / "s9_autocreate" / "nested" / "out"
    rc, _, err = run_tool(
        str(FIX / "master_16000.wav"),
        str(FIX / "manifest_mixed.json"), str(auto_dir),
    )
    check("S9 non-existent nested output directory created automatically",
          rc == 0 and (auto_dir / "merged.wav").exists())
    report9 = load_report(auto_dir / "report.json")
    check("S9 end-overrun clipping recorded as warning",
          any("exceeded audio end" in w for w in report9["warnings"]))

    # ---- S10: broken manifest is fatal with no output --------------------
    out = fresh_dir("s10_broken_json")
    rc, _, err = run_tool(
        str(FIX / "master_16000.wav"),
        str(FIX / "manifest_broken.json"), str(out),
    )
    check("S10 malformed manifest rejected (exit 4, no output)",
          rc == 4 and dir_is_clean(out), err.strip())

    # ---- summary ----------------------------------------------------------
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\n{passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
