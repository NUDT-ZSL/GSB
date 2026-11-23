"""End-to-end forced-termination recovery demonstration."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

from .canonical import atomic_write_bytes, canonical_bytes
from .pipeline import load_runner
from .snapshots import STAGE_FILE_NAMES


def run_recovery_demo(root: Path) -> Dict[str, Any]:
    root = root.resolve()
    workspace = root / "artifacts" / "recovery-demo"
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    checkpoint_dir = workspace / "checkpoint"
    cache_dir = workspace / "cache"
    baseline_cache = workspace / "baseline-cache"

    sessions = root / "data" / "sessions.jsonl"
    vocabulary = root / "data" / "vocabulary_v1.json"
    config = root / "data" / "params_v1.json"

    baseline = load_runner(
        sessions, vocabulary, config, baseline_cache
    ).run()
    baseline_bytes = baseline.summary_bytes
    baseline_summary_path = workspace / "baseline-summaries.json"
    atomic_write_bytes(baseline_summary_path, baseline_bytes)

    common = [
        sys.executable,
        "-m",
        "support_summary",
        "run",
        "--root",
        str(root),
        "--checkpoint-dir",
        str(checkpoint_dir),
        "--cache-dir",
        str(cache_dir),
    ]
    crash_command = common + [
        "--fresh",
        "--crash-at",
        "rewrite:partial",
    ]
    crashed = subprocess.run(
        crash_command,
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=120,
        env=_subprocess_env(root),
    )
    snapshots_after_crash = sorted(
        path.name
        for path in checkpoint_dir.glob("*.json")
        if path.name in STAGE_FILE_NAMES.values()
    )

    resumed_output = workspace / "resumed-report.json"
    resume_command = common + [
        "--resume",
        "--output",
        str(resumed_output),
    ]
    resumed_process = subprocess.run(
        resume_command,
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=120,
        env=_subprocess_env(root),
    )
    if resumed_process.returncode != 0:
        raise RuntimeError(
            "resume subprocess failed: "
            + (resumed_process.stderr or resumed_process.stdout)
        )
    resumed_report = json.loads(resumed_output.read_text(encoding="utf-8"))
    resumed_bytes = canonical_bytes(resumed_report["summaries"])
    byte_identical = resumed_bytes == baseline_bytes

    return {
        "ok": (
            crashed.returncode == 97
            and snapshots_after_crash
            == [
                STAGE_FILE_NAMES["chunk_tokenize"],
                STAGE_FILE_NAMES["unit_score"],
            ]
            and byte_identical
        ),
        "forced_exit_code": crashed.returncode,
        "snapshots_before_resume": snapshots_after_crash,
        "resumed_stages": resumed_report.get("resumed_stages", []),
        "recomputed_stages": resumed_report.get("recomputed_stages", []),
        "snapshot_versions": {
            stage: value.get("snapshot_version")
            for stage, value in resumed_report.get("stages", {}).items()
        },
        "baseline_digest": baseline.summary_digest,
        "resumed_digest": resumed_report.get("summary_digest"),
        "byte_identical": byte_identical,
        "baseline_summary_file": str(baseline_summary_path),
        "resumed_report_file": str(resumed_output),
        "workspace": str(workspace),
    }


def _subprocess_env(root: Path) -> Dict[str, str]:
    env = os.environ.copy()
    package_root = str(Path(__file__).resolve().parents[1])
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        package_root + (os.pathsep + existing if existing else "")
    )
    env["PYTHONHASHSEED"] = "0"
    return env
