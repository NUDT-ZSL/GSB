"""Runnable demo for the wavmixer CLI.

Builds a deterministic set of 16-bit PCM WAV clips, writes a manifest, then
runs four scenarios end to end and prints what each produced:

1. complete   - four clips mixed, one of them clipping on gain
2. partial    - one referenced source deleted; the rest still mix
3. invalid    - offset -1 and gain 3.0; no WAV produced
4. no usable  - every source missing; no WAV produced

Every run overwrites ``demo/out`` so results are reproducible.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import wave
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tools.samples import constant_wav, sine_wav, write_manifest  # noqa: E402

DEMO_ROOT = REPO_ROOT / "demo"
OUT_ROOT = DEMO_ROOT / "out"


def build_samples(samples_dir: Path) -> dict[str, Path]:
    samples_dir.mkdir(parents=True, exist_ok=True)
    return {
        "footstep": sine_wav(
            samples_dir / "footstep.wav",
            freq_hz=180, duration_ms=120, amplitude=0.6,
        ),
        "sword_hit": sine_wav(
            samples_dir / "sword_hit.wav",
            freq_hz=900, duration_ms=80, amplitude=0.45,
        ),
        "door_creak": sine_wav(
            samples_dir / "door_creak.wav",
            freq_hz=320, duration_ms=200, amplitude=0.5,
        ),
        "loud_impact": constant_wav(
            samples_dir / "loud_impact.wav",
            value=30000, duration_ms=60,
        ),
        "ambience": sine_wav(
            samples_dir / "ambience.wav",
            freq_hz=110, duration_ms=300, amplitude=0.25,
        ),
    }


def run_cli(manifest: Path, output: Path) -> tuple[int, dict]:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "wavmixer",
            "--manifest",
            str(manifest),
            "--out",
            str(output),
            "--quiet",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    report_path = Path(str(output) + ".report.json")
    report = (
        json.loads(report_path.read_text(encoding="utf-8"))
        if report_path.exists()
        else {}
    )
    if completed.returncode not in (0, 2, 3):
        print(completed.stdout)
        print(completed.stderr)
    return completed.returncode, report


def describe(name: str, exit_code: int, report: dict, wav_path: Path) -> None:
    print("=" * 72)
    print(f"SCENARIO: {name}")
    print(f"  exit code ............ {exit_code}")
    print(f"  report status ........ {report.get('status')}")
    print(f"  reason ............... {report.get('reason')}")
    if report.get("status") in ("complete", "partial"):
        fmt = report["format"]
        print(
            f"  output format ........ {fmt['sample_rate']} Hz, "
            f"{fmt['bits_per_sample']}-bit, {fmt['channels']} ch, "
            f"{fmt['frames']} frames"
        )
        print(f"  total duration ....... {report['total_duration_ms']} ms")
        print(f"  overall clipped ...... {report['overall_clipped']}")
        print(
            f"  clips mixed/excluded . "
            f"{report['summary']['mixed']} / {report['summary']['excluded']}"
        )
        for clip in report["clips"]:
            state = (
                f"clipped={clip['clipped']}"
                if clip["status"] == "mixed"
                else f"excluded ({clip.get('excluded_reason')})"
            )
            print(
                f"    {clip['offset_ms']:>4} ms  gain {clip['gain']:<4} "
                f"{clip['id']:<12} {state}"
            )
        if report["excluded_sources"]:
            print("  missing/unreadable:")
            for item in report["excluded_sources"]:
                print(f"    - [{item['reason']}] {item['detail']}")
        if wav_path.exists():
            with wave.open(str(wav_path), "rb") as reader:
                frames = reader.getnframes()
                rate = reader.getframerate()
                print(
                    f"  wave re-read ......... OK "
                    f"({frames} frames @ {rate} Hz = "
                    f"{frames * 1000 / rate:.0f} ms)"
                )
    else:
        for error in report.get("errors", [])[:5]:
            print(f"  error: {error.get('field')}: {error.get('message')}")
    print(f"  wav written .......... {wav_path.exists()}")
    print(f"  report ............... {wav_path}.report.json")
    print()


def main() -> int:
    if OUT_ROOT.exists():
        shutil.rmtree(OUT_ROOT)
    samples_dir = DEMO_ROOT / "samples"
    if samples_dir.exists():
        shutil.rmtree(samples_dir)
    clips = build_samples(samples_dir)

    # 1. complete mix --------------------------------------------------------
    complete_manifest = write_manifest(
        DEMO_ROOT / "manifests" / "complete.json",
        [
            {"id": "ambience", "source": "../samples/ambience.wav", "offset_ms": 0, "gain": 0.8},
            {"id": "footstep", "source": "../samples/footstep.wav", "offset_ms": 40, "gain": 1.0},
            {"id": "door_creak", "source": "../samples/door_creak.wav", "offset_ms": 120, "gain": 1.2},
            {"id": "sword_hit", "source": "../samples/sword_hit.wav", "offset_ms": 260, "gain": 1.5},
            {"id": "loud_impact", "source": "../samples/loud_impact.wav", "offset_ms": 320, "gain": 2.0},
        ],
    )
    complete_out = OUT_ROOT / "complete" / "mix.wav"
    code, report = run_cli(complete_manifest, complete_out)
    describe("complete mix (one clip clips on gain)", code, report, complete_out)

    # 2. partial mix ---------------------------------------------------------
    partial_manifest = write_manifest(
        DEMO_ROOT / "manifests" / "partial.json",
        [
            {"id": "ambience", "source": "../samples/ambience.wav", "offset_ms": 0, "gain": 0.8},
            {"id": "footstep", "source": "../samples/footstep.wav", "offset_ms": 40, "gain": 1.0},
            {"id": "sword_hit", "source": "../samples/removed_sword_hit.wav", "offset_ms": 150, "gain": 1.0},
        ],
    )
    partial_out = OUT_ROOT / "partial" / "mix.wav"
    code, report = run_cli(partial_manifest, partial_out)
    describe("partial mix (one source missing)", code, report, partial_out)

    # 3. invalid manifest ----------------------------------------------------
    invalid_manifest = write_manifest(
        DEMO_ROOT / "manifests" / "invalid.json",
        [
            {"id": "footstep", "source": "../samples/footstep.wav", "offset_ms": -1, "gain": 1.0},
            {"id": "sword_hit", "source": "../samples/sword_hit.wav", "offset_ms": 10, "gain": 3.0},
        ],
    )
    invalid_out = OUT_ROOT / "invalid" / "mix.wav"
    code, report = run_cli(invalid_manifest, invalid_out)
    describe("invalid manifest (offset -1, gain 3.0)", code, report, invalid_out)

    # 4. no usable clips -----------------------------------------------------
    unusable_manifest = write_manifest(
        DEMO_ROOT / "manifests" / "unusable.json",
        [
            {"id": "missing_a", "source": "../samples/nope_a.wav", "offset_ms": 0, "gain": 1.0},
            {"id": "missing_b", "source": "../samples/nope_b.wav", "offset_ms": 100, "gain": 1.0},
        ],
    )
    unusable_out = OUT_ROOT / "unusable" / "mix.wav"
    code, report = run_cli(unusable_manifest, unusable_out)
    describe("all sources unavailable", code, report, unusable_out)

    print("=" * 72)
    print(f"Artifacts under {OUT_ROOT}")
    print("  complete/mix.wav, partial/mix.wav + '<wav>.report.json'")
    print("  invalid/mix.wav            -> only report, no WAV")
    print("  unusable/mix.wav           -> only report, no WAV")
    return 0


if __name__ == "__main__":
    sys.exit(main())
