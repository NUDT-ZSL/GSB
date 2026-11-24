#!/usr/bin/env python3
"""Local deterministic interrupt/recovery acceptance driver.

The driver uses the workbench's test-only fault hooks to stop after a persisted
event boundary (handled SIGINT) or immediately after a persisted sim snapshot
(SIGKILL). It then restarts without hooks and compares the completed ledger and
frame metadata with an uninterrupted baseline.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def _python() -> str:
    venv_python = ROOT / ".venv" / "bin" / "python"
    return str(venv_python if venv_python.exists() else sys.executable)


def _base_args(state_dir: Path, output_dir: Path, *, fault: int | None, signal_name: str) -> list[str]:
    args = [
        _python(),
        "-m",
        "narrative_workbench",
        "run",
        "--state",
        str(SAMPLES / "state.json"),
        "--story",
        str(SAMPLES / "story.json"),
        "--storyboard",
        str(SAMPLES / "storyboard.json"),
        "--run",
        str(SAMPLES / "run.json"),
        "--state-dir",
        str(state_dir),
        "--output-dir",
        str(output_dir),
        "--workers",
        "2",
        "--quiet",
    ]
    if fault is not None:
        args.extend(
            [
                "--fault-after-boundary",
                str(fault),
                "--fault-signal",
                signal_name,
            ]
        )
    return args


def _execute(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=ROOT, capture_output=True, text=True)


def _result_path(output_dir: Path) -> Path:
    matches = sorted(output_dir.glob("runs/*/*/result.json"))
    if len(matches) != 1:
        raise AssertionError(f"expected one result under {output_dir}, found {matches}")
    return matches[0]


def _read_result(output_dir: Path) -> dict[str, Any]:
    return json.loads(_result_path(output_dir).read_text(encoding="utf-8"))


def _read_frames(output_dir: Path) -> dict[str, Any]:
    result_path = _result_path(output_dir)
    frames_path = result_path.parent / "frames.json"
    return json.loads(frames_path.read_text(encoding="utf-8"))


def _run_baseline(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    result = _execute(_base_args(root / "baseline-state", root / "baseline-output", fault=None, signal_name="sigint"))
    if result.returncode != 0:
        raise AssertionError(f"baseline failed: {result.stderr}")
    return _read_result(root / "baseline-output"), _read_frames(root / "baseline-output")


def _run_sigint(
    root: Path,
    baseline_result: dict[str, Any],
    baseline_frames: dict[str, Any],
) -> dict[str, Any]:
    state_dir = root / "sigint-state"
    output_dir = root / "sigint-output"
    first = _execute(_base_args(state_dir, output_dir, fault=5, signal_name="sigint"))
    if first.returncode != 130:
        raise AssertionError(
            f"expected SIGINT exit 130, got {first.returncode}: {first.stderr}"
        )
    marker_path = state_dir / "interrupt.json"
    if not marker_path.exists():
        raise AssertionError("SIGINT interruption marker is missing")
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    if marker.get("code") != "E_INTERRUPTED" or marker.get("boundary") != "event":
        raise AssertionError(f"unexpected SIGINT marker: {marker}")
    if list(output_dir.glob("runs/*/*/result.json")):
        raise AssertionError("interrupted run left a completed result")
    resumed = _execute(_base_args(state_dir, output_dir, fault=None, signal_name="sigint"))
    if resumed.returncode != 0:
        raise AssertionError(f"SIGINT resume failed: {resumed.stderr}")
    result = _read_result(output_dir)
    frames = _read_frames(output_dir)
    if result != baseline_result or frames != baseline_frames:
        raise AssertionError("SIGINT-resumed result is not field/pixel identical")
    if marker_path.exists():
        raise AssertionError("successful resume did not clear the interruption marker")
    return {"exit_code": first.returncode, "marker": marker, "resumed": True}


def _run_sigkill(
    root: Path,
    baseline_result: dict[str, Any],
    baseline_frames: dict[str, Any],
) -> dict[str, Any]:
    state_dir = root / "sigkill-state"
    output_dir = root / "sigkill-output"
    first = _execute(_base_args(state_dir, output_dir, fault=7, signal_name="sigkill"))
    if first.returncode == 0:
        raise AssertionError("SIGKILL test unexpectedly completed")
    if (state_dir / "interrupt.json").exists():
        raise AssertionError("SIGKILL test unexpectedly wrote an interruption marker")
    if list(output_dir.glob("runs/*/*/result.json")):
        raise AssertionError("SIGKILL run left a completed result")
    resumed = _execute(_base_args(state_dir, output_dir, fault=None, signal_name="sigint"))
    if resumed.returncode != 0:
        raise AssertionError(f"SIGKILL resume failed: {resumed.stderr}")
    result = _read_result(output_dir)
    frames = _read_frames(output_dir)
    if result != baseline_result or frames != baseline_frames:
        raise AssertionError("SIGKILL-resumed result is not field/pixel identical")
    return {"exit_code": first.returncode, "interrupt_marker": False, "resumed": True}


def _run_sigint_frame(
    root: Path,
    baseline_result: dict[str, Any],
    baseline_frames: dict[str, Any],
) -> dict[str, Any]:
    state_dir = root / "sigint-frame-state"
    output_dir = root / "sigint-frame-output"
    first = _execute(_base_args(state_dir, output_dir, fault=18, signal_name="sigint"))
    if first.returncode != 130:
        raise AssertionError(
            f"expected frame-boundary SIGINT exit 130, got {first.returncode}: {first.stderr}"
        )
    marker_path = state_dir / "interrupt.json"
    if not marker_path.exists():
        raise AssertionError("frame-boundary interruption marker is missing")
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    if marker.get("boundary") != "frame_block":
        raise AssertionError(f"unexpected frame-boundary marker: {marker}")
    if list(output_dir.glob("runs/*/*/result.json")):
        raise AssertionError("frame-boundary interrupted run left a completed result")
    resumed = _execute(_base_args(state_dir, output_dir, fault=None, signal_name="sigint"))
    if resumed.returncode != 0:
        raise AssertionError(f"frame-boundary SIGINT resume failed: {resumed.stderr}")
    if (
        _read_result(output_dir) != baseline_result
        or _read_frames(output_dir) != baseline_frames
    ):
        raise AssertionError(
            "frame-boundary SIGINT resume is not field/pixel identical"
        )
    return {"exit_code": first.returncode, "boundary": "frame_block", "resumed": True}


def _run_sigkill_frame(
    root: Path,
    baseline_result: dict[str, Any],
    baseline_frames: dict[str, Any],
) -> dict[str, Any]:
    state_dir = root / "sigkill-frame-state"
    output_dir = root / "sigkill-frame-output"
    first = _execute(_base_args(state_dir, output_dir, fault=19, signal_name="sigkill"))
    if first.returncode == 0:
        raise AssertionError("frame-boundary SIGKILL test unexpectedly completed")
    if (state_dir / "interrupt.json").exists():
        raise AssertionError("frame-boundary SIGKILL wrote an interruption marker")
    if list(output_dir.glob("runs/*/*/result.json")):
        raise AssertionError("frame-boundary SIGKILL left a completed result")
    resumed = _execute(_base_args(state_dir, output_dir, fault=None, signal_name="sigint"))
    if resumed.returncode != 0:
        raise AssertionError(f"frame-boundary SIGKILL resume failed: {resumed.stderr}")
    if (
        _read_result(output_dir) != baseline_result
        or _read_frames(output_dir) != baseline_frames
    ):
        raise AssertionError(
            "frame-boundary SIGKILL resume is not field/pixel identical"
        )
    return {"exit_code": first.returncode, "boundary": "frame_block", "resumed": True}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--work-root",
        help="directory for driver artifacts; a new timestamped directory is used by default",
    )
    args = parser.parse_args(argv)
    if args.work_root:
        root = Path(args.work_root).resolve()
        root.mkdir(parents=True, exist_ok=True)
    else:
        root = Path(tempfile.mkdtemp(prefix="narrative-driver-", dir=str(ROOT / "runtime")))
    baseline_result, baseline_frames = _run_baseline(root)
    summary = {
        "ok": True,
        "work_root": str(root),
        "baseline": {
            "frame_count": baseline_result["frame_count"],
            "ledger_records": baseline_result["ledger_record_count"],
            "replay_ok": baseline_result["replay_ok"],
        },
        "sigint": _run_sigint(root, baseline_result, baseline_frames),
        "sigkill": _run_sigkill(root, baseline_result, baseline_frames),
        "sigint_frame_boundary": _run_sigint_frame(
            root, baseline_result, baseline_frames
        ),
        "sigkill_frame_boundary": _run_sigkill_frame(
            root, baseline_result, baseline_frames
        ),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

