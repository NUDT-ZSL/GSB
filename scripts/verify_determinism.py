#!/usr/bin/env python3
"""Run serial/concurrent adjudications and compare sync markers/results."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


def run(workers: int, state: Path, output: Path) -> dict:
    cmd = [
        PYTHON,
        "-m",
        "offline_authz",
        "adjudicate",
        "--input-dir",
        str(ROOT / "samples" / "generated"),
        "--state-dir",
        str(state),
        "--watermark",
        "1",
        "--workers",
        str(workers),
        "--output",
        str(output),
    ]
    completed = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True)
    payload = json.loads(output.read_text(encoding="utf-8"))
    payload["_exit_code"] = completed.returncode
    return payload


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="authz-determinism-") as tmp:
        tmp_path = Path(tmp)
        serial = run(1, tmp_path / "state-serial", tmp_path / "serial.json")
        concurrent = run(4, tmp_path / "state-concurrent", tmp_path / "concurrent.json")
        serial_results = json.dumps(serial["results"], sort_keys=True, ensure_ascii=False)
        concurrent_results = json.dumps(concurrent["results"], sort_keys=True, ensure_ascii=False)
        same = serial_results == concurrent_results
        marker_same = serial["sync_marker"] == concurrent["sync_marker"]
    report = {
        "status": "success" if same and marker_same else "failed",
        "serial_marker": serial["sync_marker"],
        "concurrent_marker": concurrent["sync_marker"],
        "results_identical": same,
        "markers_identical": marker_same,
        "note": "exit code 1 is expected for the intentionally partial seeded sample",
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if same and marker_same else 1


if __name__ == "__main__":
    raise SystemExit(main())
