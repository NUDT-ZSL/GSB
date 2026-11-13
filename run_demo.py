#!/usr/bin/env python3
"""One-command reproducible demo: generate inputs, assemble, verify.

Usage (from the project root, Python 3.10+):

    python run_demo.py

Steps:
1. ``generate_samples.py`` writes deterministic WAVs and manifests to demo/.
2. The assembler CLI is invoked for the clean, mixed and precision manifests
   (each as a subprocess, exactly as a user would run it).
3. The invalid manifest is invoked to show the non-zero "invalid manifest" exit.
4. ``verify_demo.py`` independently re-reads the outputs and checks them.

Exit code 0 means every step behaved as documented.
"""

from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEMO = os.path.join(HERE, "demo")
OUT = os.path.join(DEMO, "out")
PY = sys.executable


def run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PY, "-I", *args],
        cwd=HERE,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def report_step(title: str, proc: subprocess.CompletedProcess, expected: int) -> bool:
    ok = proc.returncode == expected
    print(f"[{'OK' if ok else 'FAIL'}] {title} (exit {proc.returncode}, expected {expected})")
    for stream_name, text in (("stdout", proc.stdout), ("stderr", proc.stderr)):
        for line in text.strip().splitlines():
            print(f"       {stream_name}: {line}")
    return ok


def main() -> int:
    ok = True

    proc = run([os.path.join(HERE, "generate_samples.py"), "--out", DEMO])
    ok &= report_step("generate deterministic samples", proc, 0)

    demos = [
        ("clean_manifest.json", "clean_episode.wav", "clean_report.json"),
        ("mixed_manifest.json", "mixed_episode.wav", "mixed_report.json"),
        ("precision_manifest.json", "precision_episode.wav", "precision_report.json"),
    ]
    for manifest, wav_name, report_name in demos:
        proc = run(
            [
                os.path.join(HERE, "podcast_assembler.py"),
                os.path.join(DEMO, manifest),
                "-o",
                os.path.join(OUT, wav_name),
                "-r",
                os.path.join(OUT, report_name),
            ]
        )
        ok &= report_step(f"assemble {manifest}", proc, 0)

    proc = run(
        [
            os.path.join(HERE, "podcast_assembler.py"),
            os.path.join(DEMO, "invalid_manifest.json"),
            "-o",
            os.path.join(OUT, "invalid_episode.wav"),
            "-r",
            os.path.join(OUT, "invalid_report.json"),
        ]
    )
    ok &= report_step("reject invalid_manifest.json", proc, 2)
    if os.path.exists(os.path.join(OUT, "invalid_episode.wav")):
        print("[FAIL] invalid manifest must not produce an output WAV")
        ok = False

    proc = run([os.path.join(HERE, "verify_demo.py")])
    print(proc.stdout, end="")
    if proc.stderr.strip():
        print(proc.stderr, end="")
    ok &= proc.returncode == 0

    print("\nDEMO RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
