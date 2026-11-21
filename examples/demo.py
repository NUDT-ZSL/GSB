"""End-to-end demonstration: normal, injection and SIGKILL recovery."""

from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "demo_output"
PYTHON = sys.executable


def run_cli(arguments: list[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
    process = subprocess.run(
        [PYTHON, "-m", "motion_temporal", *arguments],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    if check and process.returncode != 0:
        raise RuntimeError(f"command failed: {arguments}\nstdout={process.stdout}\nstderr={process.stderr}")
    return process


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def clip_report(report: dict[str, Any], clip_id: str) -> dict[str, Any]:
    for item in report.get("clip_reports", [report]):
        if item.get("clip_id") == clip_id:
            return item
    raise KeyError(clip_id)


def main() -> int:
    shutil.rmtree(DEMO, ignore_errors=True)
    DEMO.mkdir(parents=True, exist_ok=True)
    data = DEMO / "data"
    generate = run_cli(
        [
            "generate",
            "--out",
            str(data),
            "--frame-count",
            "24",
            "--seed",
            "424242",
        ],
        check=True,
    )
    print("[1/6] generated deterministic samples")

    baseline_cache = DEMO / "cache_baseline"
    baseline = run_cli(
        ["run", "--manifest", str(data / "manifest.jsonl"), "--output", str(DEMO / "baseline.json"), "--cache-dir", str(baseline_cache), "--clip", "clip_uniform"],
        check=True,
    )
    print("[2/6] normal run")

    injection_error_path = DEMO / "injection_time_order.json"
    injection_error = run_cli(
        [
            "run",
            "--manifest",
            str(data / "manifest.jsonl"),
            "--output",
            str(injection_error_path),
            "--cache-dir",
            str(DEMO / "cache_injection_order"),
            "--clip",
            "clip_uniform",
            "--inject",
            "sampling:time_order:7",
        ]
    )
    if injection_error.returncode != 2:
        raise RuntimeError(f"expected injected failure, got {injection_error.returncode}: {injection_error.stdout}")
    print("[3/6] injected timestamp inversion -> E_TIME_ORDER")

    attribution_path = DEMO / "injection_attribution.json"
    attribution_run = run_cli(
        [
            "run",
            "--manifest",
            str(data / "manifest.jsonl"),
            "--output",
            str(attribution_path),
            "--cache-dir",
            str(DEMO / "cache_injection_attribution"),
            "--clip",
            "clip_uniform",
            "--inject",
            "sampling:missing_observation:6",
            "--inject",
            "encoding:motion_jump:12",
        ]
    )
    if attribution_run.returncode != 0:
        raise RuntimeError(f"attribution run failed: {attribution_run.stdout} {attribution_run.stderr}")
    print("[4/6] injected missing observation + motion jump -> located attribution")

    kill_cache = DEMO / "cache_kill"
    kill_output = DEMO / "killed_run_report.json"
    command = [
        PYTHON,
        "-m",
        "motion_temporal",
        "run",
        "--manifest",
        str(data / "manifest.jsonl"),
        "--output",
        str(kill_output),
        "--cache-dir",
        str(kill_cache),
        "--clip",
        "clip_uniform",
        "--delay-ms",
        "60",
    ]
    existing_encoding = set(kill_cache.glob("*/checkpoint_v*_encoding.json")) if kill_cache.exists() else set()
    process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    new_encoding: list[Path] = []
    deadline = time.time() + 20.0
    while time.time() < deadline and not new_encoding and process.poll() is None:
        time.sleep(0.01)
        new_encoding = [
            path
            for path in kill_cache.glob("*/checkpoint_v*_encoding.json")
            if path not in existing_encoding
        ]
    if process.poll() is not None:
        raise RuntimeError("kill demo process finished before forced termination")
    time.sleep(0.08)
    if os.name == "nt":
        process.terminate()
    else:
        os.kill(process.pid, signal.SIGKILL)
    process.wait(timeout=10)
    if kill_output.exists():
        raise RuntimeError("SIGKILL unexpectedly produced an exit report")
    print("[5/6] force-killed process during aggregation (no exit report)")

    resume_path = DEMO / "resumed_report.json"
    resumed = run_cli(
        [
            "run",
            "--manifest",
            str(data / "manifest.jsonl"),
            "--output",
            str(resume_path),
            "--cache-dir",
            str(kill_cache),
            "--clip",
            "clip_uniform",
        ],
        check=True,
    )
    baseline_report = load(DEMO / "baseline.json")
    resumed_report = load(resume_path)
    base_clip = clip_report(baseline_report, "clip_uniform")
    resumed_clip = clip_report(resumed_report, "clip_uniform")
    if base_clip["predictions_checksum"] != resumed_clip["predictions_checksum"]:
        raise RuntimeError("resumed predictions differ from uninterrupted baseline")
    if not {"sampling", "encoding"}.issubset(set(resumed_clip["reuse_recompute"]["stage_reused"])):
        raise RuntimeError("resume did not reuse committed sampling/encoding stages")
    verify = run_cli(
        [
            "verify",
            "--candidate",
            str(resume_path),
            "--baseline",
            str(DEMO / "baseline.json"),
        ]
    )
    if verify.returncode != 0:
        raise RuntimeError(f"verification failed: {verify.stdout} {verify.stderr}")
    print("[6/6] resumed from committed prefix; predictions byte-identical")

    graceful_path = DEMO / "graceful_pause.json"
    graceful = run_cli(
        [
            "run",
            "--manifest",
            str(data / "manifest.jsonl"),
            "--output",
            str(graceful_path),
            "--cache-dir",
            str(DEMO / "cache_graceful"),
            "--clip",
            "clip_uniform",
            "--stop-at",
            "aggregation:8",
            "--delay-ms",
            "1",
        ],
        check=True,
    )
    graceful_report = load(graceful_path)
    graceful_clip = clip_report(graceful_report, "clip_uniform")
    if graceful_clip["status"] != "paused":
        raise RuntimeError("graceful stop did not pause at frame boundary")
    resumed_graceful_path = DEMO / "graceful_resumed.json"
    run_cli(
        [
            "run",
            "--manifest",
            str(data / "manifest.jsonl"),
            "--output",
            str(resumed_graceful_path),
            "--cache-dir",
            str(DEMO / "cache_graceful"),
            "--clip",
            "clip_uniform",
        ],
        check=True,
    )
    resumed_graceful = clip_report(load(resumed_graceful_path), "clip_uniform")
    if resumed_graceful["predictions_checksum"] != base_clip["predictions_checksum"]:
        raise RuntimeError("graceful resume predictions differ")

    summary = {
        "status": "ok",
        "normal_predictions_checksum": base_clip["predictions_checksum"],
        "injection_error_code": clip_report(load(injection_error_path), "clip_uniform")["business_code"],
        "attribution_categories": sorted(
            {item["category"] for item in clip_report(load(attribution_path), "clip_uniform")["attribution"]}
        ),
        "resumed_predictions_checksum": resumed_clip["predictions_checksum"],
        "resume_stage_reused": resumed_clip["reuse_recompute"]["stage_reused"],
        "graceful_status": graceful_clip["status"],
        "graceful_resumed_checksum": resumed_graceful["predictions_checksum"],
    }
    (DEMO / "demo_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

