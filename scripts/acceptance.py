#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from imgdiag.checkpoint import CheckpointStore  # noqa: E402
from imgdiag.tasks import load_jsonl, load_params  # noqa: E402


def run_cmd(args: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "imgdiag.cli", *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def wait_for_file(path: Path, timeout: float = 40.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return
        time.sleep(0.1)
    raise TimeoutError(f"timed out waiting for {path}")


def main() -> int:
    started = time.time()
    artifacts = ROOT / "artifacts"
    runtime_root = ROOT / "runtime"
    artifacts.mkdir(exist_ok=True)
    runtime_root.mkdir(exist_ok=True)
    report: dict = {"ok": False, "steps": []}

    gen = subprocess.run(
        [sys.executable, "gen_samples.py", "--seed", "0", "--case_range", "0", "50"],
        cwd=ROOT, text=True, capture_output=True, timeout=60, check=False,
    )
    report["steps"].append({"name": "generate_samples", "returncode": gen.returncode, "stdout": gen.stdout.strip()})
    if gen.returncode != 0:
        report["gen_stderr"] = gen.stderr
        raise SystemExit("sample generation failed")

    baseline_path = artifacts / "acceptance_baseline.json"
    baseline = run_cmd(["baseline", "--out", str(baseline_path)], timeout=120)
    baseline_report = json.loads(baseline_path.read_text(encoding="utf-8"))
    report["baseline"] = {
        "returncode": baseline.returncode,
        "byte_identical": baseline_report["byte_identical_block_full_count"],
        "task_count": baseline_report["task_count"],
        "all_match": baseline_report["all_blocks_match_float64"],
    }
    if baseline.returncode != 0 or not baseline_report["all_blocks_match_float64"]:
        report["baseline_stderr"] = baseline.stderr
        raise SystemExit("baseline failed")

    parallel_path = artifacts / "acceptance_parallel.json"
    parallel = run_cmd([
        "parallel", "--workers", "4", "--batch-size", "10", "--out", str(parallel_path)
    ], timeout=120)
    parallel_report = json.loads(parallel_path.read_text(encoding="utf-8"))
    report["parallel"] = {
        "returncode": parallel.returncode,
        "all_ok": parallel_report["all_ok"],
        "scheduling_reproducible": parallel_report["scheduling_reproducible"],
        "result_count": parallel_report["result_count"],
    }
    if parallel.returncode != 0 or not parallel_report["all_ok"]:
        report["parallel_stderr"] = parallel.stderr
        raise SystemExit("parallel failed")

    workdir = Path(tempfile.mkdtemp(prefix="acceptance-kill-", dir=runtime_root))
    marker = workdir / "ready.json"
    log_path = workdir / "first_run.log"
    with log_path.open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            [
                sys.executable, "-m", "imgdiag.cli", "run",
                "--workdir", str(workdir), "--batch-size", "5", "--workers", "2",
                "--ready-after-version", "4", "--ready-marker", str(marker),
                "--pause-seconds", "60",
            ],
            cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, text=True,
            start_new_session=True,
        )
        try:
            wait_for_file(marker)
            marker_payload = json.loads(marker.read_text(encoding="utf-8"))
            killed_version = marker_payload["checkpoint_version"]
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=10)
        except Exception:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
            raise

    killed_store = CheckpointStore(str(workdir / "checkpoints"))
    killed_latest = killed_store.load_latest()
    report["force_kill"] = {
        "workdir": str(workdir),
        "marker_version": killed_version,
        "latest_version_before_restart": killed_latest["version"],
        "returncode": proc.returncode,
        "signal": -signal.SIGKILL,
    }
    if killed_latest["version"] != 4:
        raise AssertionError(f"expected kill after checkpoint 4, got {killed_latest['version']}")

    resume = run_cmd(["run", "--workdir", str(workdir), "--batch-size", "5", "--workers", "2"], timeout=180)
    if resume.returncode != 0:
        report["resume_stdout"] = resume.stdout
        report["resume_stderr"] = resume.stderr
        raise SystemExit("resume failed")

    verify_path = workdir / "self_check.json"
    verify = run_cmd([
        "verify", "--workdir", str(workdir), "--replay-all", "--out", str(verify_path)
    ], timeout=180)
    verify_report = json.loads(verify_path.read_text(encoding="utf-8"))
    if verify.returncode != 0 or not verify_report["ok"]:
        report["verify"] = verify_report
        report["verify_stderr"] = verify.stderr
        raise AssertionError("self-check failed after recovery")

    ledger_path = workdir / "ledger.json"
    ledger_after_resume = json.loads(ledger_path.read_text(encoding="utf-8"))
    first_entry = ledger_after_resume["tasks"]["edit-0000"]
    hash_map_after_resume = {
        edit_id: entry["summary"]["hashes"]["float64_output"]
        for edit_id, entry in ledger_after_resume["tasks"].items()
        if entry.get("status") == "committed"
    }
    recovery_sets = {
        "recomputed": first_entry["recomputed_stages"],
        "reused": first_entry["reused_stages"],
    }
    if first_entry["recomputed_stages"] != ["S5_metrics"]:
        raise AssertionError(f"unexpected recovery recompute set: {recovery_sets}")
    if first_entry["reused_stages"] != [
        "S1_mask_boundary", "S2_completion", "S3_style", "S4_fusion_blocks"
    ]:
        raise AssertionError(f"unexpected recovery reuse set: {recovery_sets}")

    rerun = run_cmd(["run", "--workdir", str(workdir), "--batch-size", "5", "--workers", "2"], timeout=180)
    if rerun.returncode != 0:
        report["rerun_stderr"] = rerun.stderr
        raise AssertionError("idempotent rerun failed")
    ledger_after_rerun = json.loads(ledger_path.read_text(encoding="utf-8"))
    hash_map_after_rerun = {
        edit_id: entry["summary"]["hashes"]["float64_output"]
        for edit_id, entry in ledger_after_rerun["tasks"].items()
        if entry.get("status") == "committed"
    }
    all_reused = all(
        not entry["recomputed_stages"] and len(entry["reused_stages"]) == 5
        for entry in ledger_after_rerun["tasks"].values()
        if entry.get("status") == "committed"
    )
    report["recovery"] = {
        "self_check_ok": verify_report["ok"],
        "first_edit_sets_after_resume": recovery_sets,
        "idempotent_rerun_all_stages_reused": all_reused,
        "hash_map_reproducible": hash_map_after_resume == hash_map_after_rerun,
        "committed_edits": len(hash_map_after_rerun),
        "checkpoint_chain": verify_report["checkpoint"],
    }
    if not all_reused or hash_map_after_resume != hash_map_after_rerun:
        raise AssertionError("rerun state was not reproducible")

    query = run_cmd([
        "query", "edit-0007", "--workdir", str(workdir),
        "--out", str(workdir / "query-edit-0007.json"),
    ], timeout=120)
    query_report = json.loads((workdir / "query-edit-0007.json").read_text(encoding="utf-8"))
    report["query_replay"] = {"returncode": query.returncode, "replay_match": query_report.get("replay_match")}
    if query.returncode != 0 or not query_report.get("replay_match"):
        raise AssertionError("query replay failed")

    report["ok"] = True
    report["elapsed_seconds"] = round(time.time() - started, 3)
    report["workdir"] = str(workdir)
    out_path = artifacts / "acceptance_report.json"
    out_path.write_text(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
