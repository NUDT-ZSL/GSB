"""End-to-end demonstration of checkpoint resume and bitwise reproducibility.

Run with the project virtualenv::

    .venv/bin/python tests/manual_resume_demo.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, ".venv", "bin", "python")
SAMPLE = os.path.join(ROOT, "samples", "sample.json")
OUT = os.path.join(ROOT, "out")


def run_cli(config_path, extra=None):
    cmd = [PY, "-m", "diff_render.cli", config_path] + (extra or [])
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)


def main() -> int:
    shutil.rmtree(OUT, ignore_errors=True)
    subprocess.run(
        [PY, os.path.join(ROOT, "scripts", "make_sample.py"), SAMPLE],
        cwd=ROOT, check=True, capture_output=True,
    )

    # 1. Full reference run.
    reference = run_cli(SAMPLE)
    assert reference.returncode == 0, reference.stderr
    full_report = json.loads(reference.stdout)
    full_result = json.load(open(os.path.join(OUT, "run", "fit_result.json")))

    # 2. Independent fresh run must be bitwise identical.
    shutil.rmtree(OUT, ignore_errors=True)
    again = run_cli(SAMPLE)
    assert again.returncode == 0, again.stderr
    again_result = json.load(open(os.path.join(OUT, "run", "fit_result.json")))
    assert again_result["losses"] == full_result["losses"]
    assert again_result["final_params"] == full_result["final_params"]
    print("[1] independent fresh runs are bitwise identical")

    # 3. Partial run (smaller budget, same scenario digest) then resume.
    shutil.rmtree(OUT, ignore_errors=True)
    cfg = json.load(open(SAMPLE))
    interrupted = dict(cfg)
    interrupted["optimization"] = dict(cfg["optimization"], iterations=20)
    interrupted_path = os.path.join(ROOT, "samples", "sample_20.json")
    with open(interrupted_path, "w", encoding="utf-8") as handle:
        json.dump(interrupted, handle, indent=2, sort_keys=True)

    first = run_cli(interrupted_path)
    # Budget exhausted without convergence -> E_CONVERGE, state still persists.
    assert first.returncode == 3, first.stderr
    state = json.load(open(os.path.join(OUT, "state", "state.json")))
    assert state["payload"]["iteration"] == 20, state["payload"]["iteration"]
    print("[2] partial run persisted a valid checkpoint at iteration 20")

    resumed = run_cli(SAMPLE)
    assert resumed.returncode == 0, resumed.stderr
    resumed_report = json.loads(resumed.stdout)
    resumed_result = json.load(open(os.path.join(OUT, "run", "fit_result.json")))
    assert resumed_result["losses"] == full_result["losses"], "resume changed trajectory"
    assert resumed_result["final_params"] == full_result["final_params"]
    assert resumed_report["iterations_completed"] == full_report["iterations_completed"]
    print("[3] resume reproduces the full-run result exactly")

    os.unlink(interrupted_path)
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
