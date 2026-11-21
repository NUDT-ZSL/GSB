from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from motion_temporal.generator import generate_manifest, generate_clip
from motion_temporal.pipeline import run_clip

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.name == "nt", reason="POSIX SIGKILL recovery demonstration")
def test_sigkill_recovery_matches_uninterrupted_run(tmp_path):
    from tests.conftest import make_spec

    spec = make_spec(frame_count=16)
    manifest = generate_manifest(tmp_path / "data", specs=[spec])
    baseline = run_clip(generate_clip(spec), tmp_path / "baseline")
    cache = tmp_path / "cache"
    output = tmp_path / "killed.json"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "motion_temporal",
            "run",
            "--manifest",
            str(manifest["manifest_path"]),
            "--output",
            str(output),
            "--cache-dir",
            str(cache),
            "--delay-ms",
            "40",
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.time() + 15
    while time.time() < deadline and process.poll() is None:
        if list(cache.glob("*/checkpoint_v*_encoding.json")):
            break
        time.sleep(0.01)
    assert process.poll() is None, "process ended before forced termination"
    time.sleep(0.05)
    os.kill(process.pid, signal.SIGKILL)
    process.wait(timeout=10)
    assert not output.exists()

    resumed = subprocess.run(
        [
            sys.executable,
            "-m",
            "motion_temporal",
            "run",
            "--manifest",
            str(manifest["manifest_path"]),
            "--output",
            str(output),
            "--cache-dir",
            str(cache),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    report = json.loads(output.read_text())
    assert report["clip_reports"][0]["predictions_checksum"] == baseline["predictions_checksum"]
    assert {"sampling", "encoding"}.issubset(
        set(report["clip_reports"][0]["reuse_recompute"]["stage_reused"])
    )


def test_cli_generate_run_verify(tmp_path):
    data = tmp_path / "data"
    report_path = tmp_path / "report.json"
    generate = subprocess.run(
        [
            sys.executable,
            "-m",
            "motion_temporal",
            "generate",
            "--out",
            str(data),
            "--frame-count",
            "6",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert generate.returncode == 0, generate.stderr
    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "motion_temporal",
            "run",
            "--manifest",
            str(data / "manifest.jsonl"),
            "--output",
            str(report_path),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--clip",
            "clip_uniform",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    verify = subprocess.run(
        [
            sys.executable,
            "-m",
            "motion_temporal",
            "verify",
            "--candidate",
            str(report_path),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert verify.returncode == 0, verify.stdout + verify.stderr

