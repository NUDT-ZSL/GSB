from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .canonical import atomic_write_json, read_jsonl
from .probes import run_diagnostic_probes
from .runtime import Workbench
from .sampling import generate_sample_tree
from .selfcheck import run_selfcheck


def _compare_result_bytes(left: Path, right: Path) -> dict[str, Any]:
    left_files = sorted(path.name for path in (left / "results").glob("*.json"))
    right_files = sorted(path.name for path in (right / "results").glob("*.json"))
    mismatches: list[str] = []
    for name in left_files:
        if name not in right_files or (left / "results" / name).read_bytes() != (right / "results" / name).read_bytes():
            mismatches.append(name)
    return {
        "identical": left_files == right_files and not mismatches,
        "result_file_count": len(left_files),
        "file_sets_equal": left_files == right_files,
        "mismatches": mismatches,
        "compared_extension": "results/*.json raw bytes",
    }


def build_demo(*, base_dir: str | Path = ".", state_dir: str | Path = "runs/demo") -> dict[str, Any]:
    base = Path(base_dir).resolve()
    state = Path(state_dir)
    if not state.is_absolute():
        state = Path.cwd() / state
    full_state = Path(str(state) + "-full-from-zero")
    shutil.rmtree(state, ignore_errors=True)
    shutil.rmtree(full_state, ignore_errors=True)

    sample_root = base / "samples"
    sample_manifest = generate_sample_tree(sample_root)
    matrices = sample_root / "matrices.npz"

    bench = Workbench(state)
    batch_one = read_jsonl(sample_root / sample_manifest["batches"][0])
    batch_two = read_jsonl(sample_root / sample_manifest["batches"][1])
    first = bench.submit_records(batch_one, npz_path=matrices)
    incremental = bench.submit_records(batch_two, npz_path=matrices)

    full_bench = Workbench(full_state)
    full = full_bench.submit_records(batch_two, npz_path=matrices)
    equivalence = _compare_result_bytes(state, full_state)
    atomic_write_json(state / "byte-equivalence.json", equivalence)

    probes = run_diagnostic_probes(bench)

    enrichment = {
        job_id: {"input_hash": job.get("input_hash", ""), "result_hash": job.get("result_hash", "")}
        for job_id, job in bench.load_committed_state().get("jobs", {}).items()
    }
    migration_up = bench.ledger.migrate_v1_to_v2(enrichment)
    migration_down = bench.ledger.migrate_v2_to_v1(allow_version_regression=True)

    selfcheck = run_selfcheck(state)
    atomic_write_json(state / "selfcheck.json", selfcheck)

    return {
        "ok": selfcheck["ok"] and equivalence["identical"] and probes["ok"],
        "sample_manifest": sample_manifest,
        "state_dir": str(state),
        "full_from_zero_state_dir": str(full_state),
        "first_run": {"job_count": first["job_count"], "snapshot_version": first["snapshot_version"]},
        "incremental_run": {"job_count": incremental["job_count"], "snapshot_version": incremental["snapshot_version"]},
        "byte_equivalence": equivalence,
        "probes": {"ok": probes["ok"], "count": probes["probe_count"]},
        "migration": {
            "v1_to_v2": migration_up["migrated_count"],
            "v2_to_v1": migration_down["migrated_count"],
        },
        "selfcheck": {"ok": selfcheck["ok"], "check_count": selfcheck["check_count"]},
    }


def _subprocess_env(base: Path) -> dict[str, str]:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(base) + (os.pathsep + existing if existing else "")
    return env


def recovery_demo(*, base_dir: str | Path = ".", state_dir: str | Path = "runs/recovery-demo") -> dict[str, Any]:
    base = Path(base_dir).resolve()
    state = Path(state_dir)
    if not state.is_absolute():
        state = Path.cwd() / state
    control_state = Path(str(state) + "-control")
    shutil.rmtree(state, ignore_errors=True)
    shutil.rmtree(control_state, ignore_errors=True)

    sample_root = base / "samples"
    generate_sample_tree(sample_root)
    jobs_path = sample_root / "batches" / "batch-001.jsonl"
    python = sys.executable
    env = _subprocess_env(base)

    kill_command = [
        python,
        "-m",
        "gemm_bmm_workbench.cli",
        "run",
        "--jobs",
        str(jobs_path),
        "--state-dir",
        str(state),
        "--kill-after-chunks",
        "2",
    ]
    killed = subprocess.run(kill_command, cwd=base, env=env, text=True, capture_output=True)
    staging_chunks_before = sorted((state / "staging").rglob("*.npy")) if (state / "staging").exists() else []

    resume_command = [
        python,
        "-m",
        "gemm_bmm_workbench.cli",
        "resume",
        "--state-dir",
        str(state),
    ]
    resumed = subprocess.run(resume_command, cwd=base, env=env, text=True, capture_output=True)
    if resumed.returncode != 0:
        raise RuntimeError(
            f"recovery resume failed code={resumed.returncode}\nSTDOUT:{resumed.stdout}\nSTDERR:{resumed.stderr}"
        )

    control = Workbench(control_state)
    records = read_jsonl(jobs_path)
    control.submit_records(records, npz_path=sample_root / "matrices.npz")
    equivalence = _compare_result_bytes(state, control_state)

    snapshot_versions = [
        snapshot["version"]
        for snapshot in control.snapshots.stage_snapshots_for_run(
            json_load(state / "overview.json")["run_id"] if (state / "overview.json").exists() else ""
        )
    ]
    report = {
        "ok": killed.returncode != 0 and equivalence["identical"] and resumed.returncode == 0,
        "kill_returncode": killed.returncode,
        "kill_signal": -killed.returncode if killed.returncode < 0 else None,
        "staging_chunks_written_before_kill": len(staging_chunks_before),
        "staging_chunk_sample": [str(path.relative_to(state)) for path in staging_chunks_before[:4]],
        "resume_returncode": resumed.returncode,
        "byte_equivalence_to_uninterrupted_control": equivalence,
        "control_state_dir": str(control_state),
        "snapshot_versions": snapshot_versions,
        "resume_stdout_tail": resumed.stdout[-1000:],
    }
    atomic_write_json(state / "recovery-report.json", report)
    selfcheck = run_selfcheck(state)
    atomic_write_json(state / "selfcheck.json", selfcheck)
    report["selfcheck_ok"] = selfcheck["ok"]
    return report


def json_load(path: Path) -> dict[str, Any]:
    import json

    return json.loads(path.read_text(encoding="utf-8"))
