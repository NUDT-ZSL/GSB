"""End-to-end acceptance verification harness.

Runs the workbench across the required matrix (two quantization levels x two
cluster thresholds), verifies incremental-vs-serial field equality, exercises
budget markers, subprocess interruption/resume, and writes a verification
summary JSON.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PYTHON = str(ROOT / ".venv" / "bin" / "python")
ENV = {**os.environ, "PYTHONPATH": str(ROOT / "src")}

CONFIG_MATRIX = [
    ("quant8_threshold_tight", "configs/config.json"),
    ("quant4_threshold_tight", "configs/config_quant4.json"),
    ("quant8_threshold_loose", "configs/config_threshold_loose.json"),
    ("quant4_threshold_loose", None),  # derived below
]


def run_cli(config: str, snapshot_dir: str, report: str, baseline: str,
            extra_env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    cmd = [
        PYTHON, "-m", "voiceworkbench.cli",
        "--config", config,
        "--audio-dir", "data/audio",
        "--commands", "commands/edits.jsonl",
        "--snapshot-dir", snapshot_dir,
        "--report", report,
        "--baseline-report", baseline,
    ]
    env = dict(ENV)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True,
                          text=True, check=check)


def compare(report_path: Path, baseline_path: Path) -> dict:
    report = json.loads(report_path.read_text())
    baseline = json.loads(baseline_path.read_text())
    checks = {
        "stage_keys_equal": report["stage_keys"] == baseline["stage_keys"],
        "clusters_equal": report["clusters"] == baseline["clusters"],
        "assignments_equal": report["assignments"] == baseline["assignments"],
        "segments_equal": report["segments"] == baseline["segments"],
        "rejections_equal": report["rejected_commands"] == baseline["rejected_commands"],
    }
    return {
        "all_equal": all(checks.values()),
        "checks": checks,
        "cluster_count": len(report["clusters"]),
        "segment_count": len(report["segments"]),
    }


def budget_summary(report_path: Path) -> dict:
    report = json.loads(report_path.read_text())
    responses = report["command_responses"]
    pending = [r["seq"] for r in responses if r["pending"]]
    return {
        "total_commands": len(responses),
        "pending_sequences": pending,
        "pending_code": "E_INTERACT_BUDGET_PENDING" if pending else None,
        "all_completed": all(
            len(report["assignments"]) == report["segments"] and report["segments"]
            for _ in [0]
        ),
        "max_elapsed_ms": round(max(r["elapsed_ms"] for r in responses), 3),
    }


def main() -> int:
    artifacts = ROOT / "artifacts" / "verification"
    import shutil
    shutil.rmtree(artifacts, ignore_errors=True)
    artifacts.mkdir(parents=True, exist_ok=True)

    # derive the quant4+loose config deterministically
    q4 = json.loads((ROOT / "configs/config_quant4.json").read_text())
    q4["cluster_distance_threshold"] = 2.5
    derived_config = artifacts / "config_quant4_loose.json"
    derived_config.write_text(json.dumps(q4, indent=2, sort_keys=True) + "\n")

    matrix = [
        ("quant8_threshold_tight", "configs/config.json"),
        ("quant4_threshold_tight", "configs/config_quant4.json"),
        ("quant8_threshold_loose", "configs/config_threshold_loose.json"),
        ("quant4_threshold_loose", str(derived_config.relative_to(ROOT))),
    ]

    summary = {"matrix": [], "generated_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    overall_ok = True

    for name, config in matrix:
        snap_dir = artifacts / name / "snap"
        report_path = artifacts / name / "report.json"
        baseline_path = artifacts / name / "baseline.json"
        proc = run_cli(config, str(snap_dir), str(report_path), str(baseline_path))
        assert proc.returncode == 0, proc.stderr
        comparison = compare(report_path, baseline_path)
        budget = budget_summary(report_path)
        overall_ok &= comparison["all_equal"]
        summary["matrix"].append({
            "case": name,
            "config": config,
            "comparison": comparison,
            "budget": budget,
        })

    # ---- interruption / resume via a real subprocess ------------------
    interrupt_dir = artifacts / "interrupt" / "snap"
    interrupt_report = artifacts / "interrupt" / "partial.json"
    env_exit = {"WORKBENCH_EXIT_AFTER_SEQ": "3"}
    proc = run_cli("configs/config.json", str(interrupt_dir),
                   str(interrupt_report), str(artifacts / "interrupt" / "baseline.json"),
                   extra_env=env_exit)
    assert proc.returncode == 0, proc.stderr
    partial = json.loads(interrupt_report.read_text())
    assert partial["exit"]["code"] == 0
    assert partial["exit"]["stopped_after_seq"] == 3

    # resume in a fresh process and finish the remaining commands
    resumed_report = artifacts / "interrupt" / "resumed.json"
    resumed_baseline = artifacts / "interrupt" / "resumed_baseline.json"
    proc = run_cli("configs/config.json", str(interrupt_dir),
                   str(resumed_report), str(resumed_baseline))
    assert proc.returncode == 0, proc.stderr
    resume_compare = compare(resumed_report, resumed_baseline)
    overall_ok &= resume_compare["all_equal"]
    summary["interrupt_resume"] = {
        "stopped_after_seq": 3,
        "resume_exit_code": proc.returncode,
        "comparison": resume_compare,
    }

    # ---- atomic replacement evidence ----------------------------------
    snap_files = sorted(p.name for p in interrupt_dir.iterdir())
    summary["snapshot_dir_listing"] = snap_files
    summary["overall_ok"] = overall_ok
    out_path = artifacts / "verification_summary.json"
    out_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"overall_ok": overall_ok, "summary": str(out_path)},
                     sort_keys=True))
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
