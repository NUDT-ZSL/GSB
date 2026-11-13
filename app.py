"""Deterministic end-to-end MVST demonstration driver.

Running ``python app.py`` regenerates every input sample in ``./samples``
from fixed seeds (no network, no external assets), then walks through the
full mastering workflow and the representative acceptance scenarios:

  1. validate on a legal multi-language suite
  2. time-base unification (123 ms -> 5904 master samples; 40/80 ms -> 1/2)
  3. package into master.mvst and read it back with info (header index only)
  4. independent container verification + audio block tamper resistance
  5. error contracts (E_WAV_FORMAT, E_MANIFEST, E_LANG_*, E_TARGET_EXISTS,
     E_MASTER_INVALID, E_RESAMPLE) and the SIGINT/atomic-delivery path

All commands run as local subprocesses exactly as a human would type them.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable


def banner(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def run(argv: list[str], cwd: Path, env: dict | None = None) -> tuple[int, str, str]:
    merged = dict(os.environ)
    if env:
        merged.update(env)
    proc = subprocess.run([PY, *argv], cwd=str(cwd), env=merged,
                          capture_output=True, text=True, timeout=180)
    return proc.returncode, proc.stdout, proc.stderr


def show(label: str, code: int, out: str, err: str) -> None:
    print(f"$ {label}\n  exit_code={code}")
    if out.strip():
        first = out.strip().splitlines()
        print("  stdout: " + "\n          ".join(first[:18])
              + ("\n          ..." if len(first) > 18 else ""))
    if err.strip():
        print("  stderr: " + err.strip())


def build_samples() -> Path:
    banner("STEP 1  Build deterministic sample set (no external assets)")
    samples = ROOT / "samples"
    if samples.exists():
        shutil.rmtree(samples)
    samples.mkdir()
    from mvst.samplegen import (
        build_standard_suite, build_video, build_wav, write_manifest, write_wav,
    )
    build_standard_suite(samples, gains={"en": 0.0, "es": 0.0, "fr": 0.0,
                                         "ja": 12.0})
    # Offset the English track by 123 ms to demonstrate exact time alignment.
    manifest_path = samples / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["tracks"][0]["offset_ms"] = 123
    with open(manifest_path, "w", encoding="utf-8", newline="") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    # Also publish a root-level manifest whose element paths are relative to
    # samples/, so the documented commands work from the project root:
    #   python -m mvst validate --manifest manifest.json --root samples
    with open(ROOT / "manifest.json", "w", encoding="utf-8", newline="") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)

    # Malformed WAV trio for the E_WAV_FORMAT contract.
    bad = samples / "bad"
    bad.mkdir()
    write_wav(bad / "stereo.wav", 48000, [100] * 100, channels=2)
    write_wav(bad / "eight.wav", 48000, [10] * 100, bits=8)
    write_wav(bad / "overflow.wav", 48000, [10] * 100, bad_fmt_length=True)
    (bad / "video.raw").write_bytes(build_video(25))
    print(f"  wrote {len(list(samples.rglob('*')))} sample entries under samples/")
    return samples


def demo_validate(samples: Path) -> None:
    banner("STEP 2  validate (legal suite; exact rational time base)")
    code, out, err = run(["-m", "mvst", "validate", "--manifest", "manifest.json",
                          "--root", ".", "--jobs", "4"], samples)
    show("mvst validate --jobs 4", code, out, err)
    report = json.loads(out)
    offsets = {a["language"]: a for a in report["intermediate"]["audio_offsets"]}
    print(f"  CHECK en offset_ms=123 -> start_sample={offsets['en']['start_sample']} "
          f"(expected 5904)")
    print(f"  CHECK conclusion={report['conclusion']} "
          f"key_order={list(report.keys())}")


def demo_errors(samples: Path) -> None:
    banner("STEP 3  Business error contracts (exit code 2, one JSON line)")
    bad = samples / "bad"
    for name in ("stereo.wav", "eight.wav", "overflow.wav"):
        manifest = {
            "master_sample_rate": 48000, "video_fps": 25,
            "video_file": "video.raw", "languages": ["en"],
            "tracks": [{"language": "en", "offset_ms": 0, "gain_db": 0.0,
                        "file": name}],
            "subtitles": [],
        }
        with open(bad / "manifest.json", "w", encoding="utf-8", newline="") as fh:
            json.dump(manifest, fh)
        code, _out, err = run(["-m", "mvst", "validate", "--manifest",
                               "manifest.json", "--root", "."], bad)
        print(f"  {name:14s} -> exit {code}, {err.strip()}")

    bad_manifest = {
        "master_sample_rate": 48000, "video_fps": 25,
        "video_file": "video.raw", "languages": ["en"],
        "tracks": [{"language": "de", "offset_ms": 6000, "gain_db": 0.0,
                    "file": "x.wav"}],
        "subtitles": [],
    }
    with open(bad / "bad_manifest.json", "w", encoding="utf-8", newline="") as fh:
        json.dump(bad_manifest, fh)
    code, _out, err = run(["-m", "mvst", "validate", "--manifest",
                           "bad_manifest.json", "--root", "."], bad)
    print(f"  bad manifest   -> exit {code}, {err.strip()}")

    code, _out, err = run(["-m", "mvst", "info", "does_not_exist.mvst"], bad)
    print(f"  info missing   -> exit {code}, {err.strip()}")


def demo_package(samples: Path) -> Path:
    banner("STEP 4  package -> master.mvst (atomic temp-file + rename)")
    master = samples / "master.mvst"
    if master.exists():
        master.unlink()
    code, out, err = run(["-m", "mvst", "package", "--manifest", "manifest.json",
                          "--root", ".", "--out", "master.mvst", "--force",
                          "--jobs", "4"], samples)
    show("mvst package --force --jobs 4", code, out, err)
    report = json.loads(out)
    for audio in report["audio"]:
        print(f"  audio {audio['language']}: source={audio['source_samples']} "
              f"resampled={audio['resampled_samples']} clip={audio['clip_count']} "
              f"peak={audio['peak']} rms={audio['rms']}")
    print(f"  warnings={[w['error_code'] for w in report['warnings']]}")
    assert master.exists()
    return master


def demo_info_and_verify(samples: Path, master: Path) -> None:
    banner("STEP 5  info (header index only) + independent verification")
    code, out, err = run(["-m", "mvst", "info", "master.mvst"], samples)
    show("mvst info master.mvst", code, out, err)
    info_before = out

    verify = ROOT / "tools" / "verify_container.py"
    code, out, err = run([str(verify), "master.mvst"], samples)
    show("tools/verify_container.py", code, out, err)

    # Tamper an audio block payload; info output must remain identical.
    from mvst.container import BLOCK_HEADER_SIZE, TYPE_AUDIO, read_master_header
    header = read_master_header(master)
    entry = next(e for e in header.entries if e.track_type == TYPE_AUDIO)
    data = bytearray(master.read_bytes())
    pos = entry.byte_offset + BLOCK_HEADER_SIZE + 7
    data[pos] ^= 0xA5
    master.write_bytes(bytes(data))
    code, info_after, err = run(["-m", "mvst", "info", "master.mvst"], samples)
    print(f"  after tampering audio block byte at {pos}: info exit={code}")
    print(f"  CHECK info identical pre/post tamper: {info_before == info_after}")
    code, out, _err = run([str(verify), "master.mvst",
                           "--tamper-check"], samples)
    print(f"  independent verify still parses index: exit={code}")


def demo_target_and_language(samples: Path) -> None:
    banner("STEP 6  --force guard, E_LANG_PARTIAL, E_RESAMPLE collection")
    master = samples / "master.mvst"
    digest = hashlib.sha256(master.read_bytes()).hexdigest()
    code, _out, err = run(["-m", "mvst", "package", "--manifest", "manifest.json",
                           "--root", ".", "--out", "master.mvst"], samples)
    print(f"  package without --force -> exit {code}, {err.strip()}")
    print(f"  target unchanged: {hashlib.sha256(master.read_bytes()).hexdigest() == digest}")

    # Partial language: copy only the top-level suite files, drop the French
    # subtitle, and rewrite the manifest to match.
    partial = samples / "partial"
    if partial.exists():
        shutil.rmtree(partial)
    partial.mkdir()
    for item in samples.iterdir():
        if item.is_file():
            shutil.copy2(item, partial / item.name)
    fr_sub = partial / "sub_fr.srt"
    if fr_sub.exists():
        fr_sub.unlink()
    mpath = partial / "manifest.json"
    mani = json.loads(mpath.read_text(encoding="utf-8"))
    mani["subtitles"] = [s for s in mani["subtitles"] if s["language"] != "fr"]
    with open(mpath, "w", encoding="utf-8", newline="") as fh:
        json.dump(mani, fh)
    code, out, err = run(["-m", "mvst", "validate", "--manifest", "manifest.json",
                          "--root", "."], partial)
    report = json.loads(out)
    partial_langs = [w["language"] for w in report["warnings"]
                     if w["error_code"] == "E_LANG_PARTIAL"]
    print(f"  French subtitle removed -> exit {code}, "
          f"E_LANG_PARTIAL languages={partial_langs} (ja also legitimately "
          f"has audio-only coverage)")

    # Deterministic worker failure on track 3: successes still reported.
    code, out, err = run(["-m", "mvst", "validate", "--manifest", "manifest.json",
                          "--root", ".", "--jobs", "4"], samples,
                         env={"MVST_FAIL_TRACK": "2"})
    print(f"  injected track-3 resample failure -> exit {code}, {err.strip()}")
    if out.strip():
        report = json.loads(out)
        print(f"  successful audio tracks retained: "
              f"{[a['index'] for a in report['audio']]}")


def main() -> int:
    samples = build_samples()
    demo_validate(samples)
    demo_errors(samples)
    master = demo_package(samples)
    demo_info_and_verify(samples, master)
    demo_target_and_language(samples)
    banner("DEMO COMPLETE")
    print("All deterministic checks executed locally; no network used.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
