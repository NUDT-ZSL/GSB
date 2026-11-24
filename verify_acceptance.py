from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import wave
from pathlib import Path
from typing import Any

import numpy as np

from generate_samples import generate


ROOT = Path(__file__).resolve().parent
WORK = ROOT / "artifacts" / "acceptance_data"


def run_cli(task: Path, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "audiofp.cli", "run", "--task", str(task)],
        cwd=str(ROOT),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def task_with_paths(
    source: dict[str, Any],
    target: Path,
    *,
    state: str,
    report: str,
    segments: str,
    **updates: Any,
) -> Path:
    task = json.loads(json.dumps(source))
    task["state_dir"] = state
    task["report_path"] = report
    task["segments_path"] = segments
    for key, value in updates.items():
        task[key] = value
    target.write_text(
        json.dumps(task, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return target


def load_report(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def segment_keys(report: dict[str, Any]) -> list[tuple[Any, ...]]:
    return sorted(
        (
            item["query_file"],
            item["reference_file"],
            item["query_start_sample"],
            item["query_end_sample"],
            item["reference_start_sample"],
            item["reference_end_sample"],
            item["integer_frame_offset"],
            item["fixed_score_units"],
            item["reference_float_score"],
            item["match_count"],
        )
        for item in report["segments"]
    )


def ranking_keys(report: dict[str, Any]) -> list[tuple[Any, ...]]:
    return [
        (
            item["rank"],
            item["candidate_id"],
            item["fixed_score_units"],
            item["query_start_sample"],
            item["reference_start_sample"],
        )
        for item in report["ranking"]
    ]


def reasons(report: dict[str, Any]) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in report["incremental"]["affected_intervals"]:
        result[item["reason"]] = result.get(item["reason"], 0) + 1
    return result


def append_noise(path: Path, sample_rate: int, seconds: float, seed: int) -> None:
    with wave.open(str(path), "rb") as handle:
        params = handle.getparams()
        frames = handle.readframes(handle.getnframes())
    rng = np.random.default_rng(seed)
    extra = np.rint(
        rng.normal(0.0, 0.01, size=int(sample_rate * seconds)) * 32767.0
    ).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(params.nchannels)
        handle.setsampwidth(params.sampwidth)
        handle.setframerate(params.framerate)
        handle.writeframes(frames + extra.tobytes())


def wait_for_chunks(state_dir: Path, timeout: float = 10.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        chunks = list((state_dir / "chunks" / "fp").rglob("*.json"))
        if chunks:
            return len(chunks)
        time.sleep(0.02)
    return 0


def validate_chunk_checksums(state_dir: Path) -> tuple[int, int]:
    canonical = lambda value: json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    checked = 0
    bad = 0
    for path in (state_dir / "chunks").rglob("*.json"):
        wrapper = json.loads(path.read_text(encoding="utf-8"))
        checked += 1
        if hashlib.sha256(canonical(wrapper["payload"])).hexdigest() != wrapper["payload_sha256"]:
            bad += 1
    return checked, bad


def main() -> int:
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)
    checks: dict[str, Any] = {}
    timings: dict[str, float] = {}

    started = time.perf_counter()
    manifest = generate(WORK / "dataset", seed=20261125)
    timings["generate_seconds"] = round(time.perf_counter() - started, 3)
    data = WORK / "dataset"
    root_task = json.loads((data / "task.json").read_text(encoding="utf-8"))

    baseline_task = task_with_paths(
        root_task,
        data / "baseline_task.json",
        state="baseline_state",
        report="baseline_report.json",
        segments="baseline_segments.json",
    )
    started = time.perf_counter()
    baseline_process = run_cli(baseline_task)
    timings["baseline_seconds"] = round(time.perf_counter() - started, 3)
    checks["baseline_exit_0"] = baseline_process.returncode == 0
    baseline = load_report(data / "baseline_report.json")
    checks["baseline_segments"] = len(baseline["segments"])
    checks["baseline_unmatched"] = [
        item["query_file"] for item in baseline["unmatched_queries"]
    ]
    checks["baseline_fixed_only"] = baseline["judgement"]["fixed_point_only"]
    checks["baseline_float_rank_mismatches"] = len(
        baseline["fixed_float_rank_mismatches"]
    )
    checks["baseline_first_tolerance_exceeded"] = {
        segment["query_file"]: segment["first_tolerance_exceeded_at"]
        for segment in baseline["segments"]
        if segment["first_tolerance_exceeded_at"] is not None
    }

    concurrency_task = task_with_paths(
        root_task,
        data / "concurrency1_task.json",
        state="concurrency1_state",
        report="concurrency1_report.json",
        segments="concurrency1_segments.json",
        concurrency_limit=1,
    )
    started = time.perf_counter()
    concurrency_process = run_cli(concurrency_task)
    timings["concurrency1_seconds"] = round(time.perf_counter() - started, 3)
    concurrency1 = load_report(data / "concurrency1_report.json")
    checks["concurrency1_equal"] = (
        concurrency_process.returncode == 0
        and segment_keys(concurrency1) == segment_keys(baseline)
        and ranking_keys(concurrency1) == ranking_keys(baseline)
    )

    low_memory_task = task_with_paths(
        root_task,
        data / "low_memory_task.json",
        state="low_memory_state",
        report="low_memory_report.json",
        segments="low_memory_segments.json",
        memory_budget_bytes=700_000,
    )
    started = time.perf_counter()
    low_memory_process = run_cli(low_memory_task)
    timings["low_memory_seconds"] = round(time.perf_counter() - started, 3)
    low_memory = load_report(data / "low_memory_report.json")
    checks["low_memory_equal"] = (
        low_memory_process.returncode == 0
        and segment_keys(low_memory) == segment_keys(baseline)
        and ranking_keys(low_memory) == ranking_keys(baseline)
    )

    partial_task_data = json.loads(json.dumps(root_task))
    partial_task_data["query_files"] = [root_task["query_files"][0]]
    partial_task = task_with_paths(
        partial_task_data,
        data / "partial_task.json",
        state="incremental_state",
        report="partial_report.json",
        segments="partial_segments.json",
    )
    incremental_task = task_with_paths(
        root_task,
        data / "incremental_task.json",
        state="incremental_state",
        report="incremental_report.json",
        segments="incremental_segments.json",
    )
    started = time.perf_counter()
    partial_process = run_cli(partial_task)
    full_incremental_process = run_cli(incremental_task)
    timings["incremental_seconds"] = round(time.perf_counter() - started, 3)
    incremental = load_report(data / "incremental_report.json")
    checks["incremental_equal"] = (
        partial_process.returncode == 0
        and full_incremental_process.returncode == 0
        and segment_keys(incremental) == segment_keys(baseline)
    )
    checks["incremental_reuse"] = {
        "fingerprint_reused": incremental["incremental"][
            "fingerprint_chunks_reused"
        ],
        "fingerprint_written": incremental["incremental"][
            "fingerprint_chunks_written"
        ],
        "candidate_reused": incremental["incremental"]["candidate_chunks_reused"],
        "candidate_written": incremental["incremental"]["candidate_chunks_written"],
        "reasons": reasons(incremental),
    }

    append_root = WORK / "append_env"
    shutil.copytree(data / "audio", append_root / "audio")
    append_task_data = json.loads(json.dumps(root_task))
    append_task = task_with_paths(
        append_task_data,
        append_root / "append_task.json",
        state="state",
        report="report.json",
        segments="segments.json",
    )
    before_append = run_cli(append_task)
    append_noise(append_root / "audio" / "query_a_insert_noise.wav", 48000, 1.0, 777)
    started = time.perf_counter()
    after_append = run_cli(append_task)
    timings["append_incremental_seconds"] = round(time.perf_counter() - started, 3)
    append_report = load_report(append_root / "report.json")
    fresh_append_task = task_with_paths(
        append_task_data,
        append_root / "append_fresh_task.json",
        state="fresh_state",
        report="fresh_report.json",
        segments="fresh_segments.json",
    )
    fresh_append = run_cli(fresh_append_task)
    fresh_append_report = load_report(append_root / "fresh_report.json")
    checks["append_tail_equal"] = (
        before_append.returncode == 0
        and after_append.returncode == 0
        and fresh_append.returncode == 0
        and segment_keys(append_report) == segment_keys(fresh_append_report)
    )
    checks["append_reuse"] = {
        "fingerprint_reused": append_report["incremental"][
            "fingerprint_chunks_reused"
        ],
        "fingerprint_written": append_report["incremental"][
            "fingerprint_chunks_written"
        ],
        "reasons": reasons(append_report),
    }

    interrupt_root = WORK / "interrupt_env"
    shutil.copytree(data / "audio", interrupt_root / "audio")
    interrupt_task = task_with_paths(
        json.loads(json.dumps(root_task)),
        interrupt_root / "interrupt_task.json",
        state="state",
        report="report.json",
        segments="segments.json",
    )
    interrupt_env = dict(os.environ)
    interrupt_env["AUDIOFP_INTERRUPT_AFTER_CHUNKS"] = "10"
    interrupted = run_cli(interrupt_task, env=interrupt_env)
    marker = json.loads(
        (interrupt_root / "state" / "interruption.json").read_text(encoding="utf-8")
    )
    interrupt_no_report = not (interrupt_root / "report.json").exists()
    resumed = run_cli(interrupt_task)
    resumed_report = load_report(interrupt_root / "report.json")
    checks["graceful_interrupt"] = {
        "exit_code": interrupted.returncode,
        "no_report": interrupt_no_report,
        "marker_exit": marker["exit_code"],
        "resume_exit": resumed.returncode,
        "equal": segment_keys(resumed_report) == segment_keys(baseline),
        "resumed_signal": resumed_report["incremental"][
            "resumed_from_interruption"
        ]["signal"],
    }

    sigterm_root = WORK / "sigterm_env"
    shutil.copytree(data / "audio", sigterm_root / "audio")
    sigterm_task = task_with_paths(
        json.loads(json.dumps(root_task)),
        sigterm_root / "sigterm_task.json",
        state="state",
        report="report.json",
        segments="segments.json",
    )
    sigterm_env = dict(os.environ)
    sigterm_env["AUDIOFP_TEST_CHUNK_DELAY_MS"] = "50"
    process = subprocess.Popen(
        [sys.executable, "-m", "audiofp.cli", "run", "--task", str(sigterm_task)],
        cwd=str(ROOT),
        env=sigterm_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    wait_for_chunks(sigterm_root / "state")
    time.sleep(0.1)
    process.send_signal(signal.SIGTERM)
    sigterm_rc = process.wait(timeout=60)
    sigterm_marker = json.loads(
        (sigterm_root / "state" / "interruption.json").read_text(encoding="utf-8")
    )
    sigterm_no_report = not (sigterm_root / "report.json").exists()
    sigterm_resume = run_cli(sigterm_task)
    sigterm_report = load_report(sigterm_root / "report.json")
    checks["sigterm"] = {
        "exit_code": sigterm_rc,
        "marker_signal": sigterm_marker["signal"],
        "no_report_before_resume": sigterm_no_report,
        "resume_exit": sigterm_resume.returncode,
        "equal": segment_keys(sigterm_report) == segment_keys(baseline),
    }

    kill_root = WORK / "sigkill_env"
    shutil.copytree(data / "audio", kill_root / "audio")
    kill_task = task_with_paths(
        json.loads(json.dumps(root_task)),
        kill_root / "kill_task.json",
        state="state",
        report="report.json",
        segments="segments.json",
    )
    kill_env = dict(os.environ)
    kill_env["AUDIOFP_TEST_CHUNK_DELAY_MS"] = "60"
    process = subprocess.Popen(
        [sys.executable, "-m", "audiofp.cli", "run", "--task", str(kill_task)],
        cwd=str(ROOT),
        env=kill_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    wait_for_chunks(kill_root / "state")
    time.sleep(0.2)
    process.kill()
    kill_rc = process.wait(timeout=10)
    checked, bad = validate_chunk_checksums(kill_root / "state")
    kill_resume = run_cli(kill_task)
    kill_report = load_report(kill_root / "report.json")
    checks["sigkill"] = {
        "exit_code": kill_rc,
        "persisted_chunks_checked": checked,
        "bad_checksums": bad,
        "resume_exit": kill_resume.returncode,
        "equal": segment_keys(kill_report) == segment_keys(baseline),
    }

    param_root = WORK / "param_env"
    shutil.copytree(data / "audio", param_root / "audio")
    param_task_data = json.loads(json.dumps(root_task))
    param_task_data["query_files"] = [root_task["query_files"][0]]
    param_before = task_with_paths(
        param_task_data,
        param_root / "param_task.json",
        state="state",
        report="report.json",
        segments="segments.json",
    )
    run_cli(param_before)
    changed = json.loads(json.dumps(param_task_data))
    changed["fingerprint"]["peak_neighborhood"] = 4
    param_after = task_with_paths(
        changed,
        param_root / "param_task.json",
        state="state",
        report="report_changed.json",
        segments="segments_changed.json",
    )
    changed_run = run_cli(param_after)
    changed_report = load_report(param_root / "report_changed.json")
    param_fresh = task_with_paths(
        changed,
        param_root / "param_fresh_task.json",
        state="fresh_state",
        report="fresh_report.json",
        segments="fresh_segments.json",
    )
    fresh_run = run_cli(param_fresh)
    fresh_changed = load_report(param_root / "fresh_report.json")
    checks["parameter_change"] = {
        "exit": changed_run.returncode,
        "equal_fresh": segment_keys(changed_report) == segment_keys(fresh_changed),
        "fresh_exit": fresh_run.returncode,
        "reasons": reasons(changed_report),
    }

    invalid_root = WORK / "invalid_env"
    shutil.copytree(data / "audio", invalid_root / "audio")
    invalid_data = json.loads(json.dumps(root_task))
    invalid_data["fingerprint"]["band_boundaries_hz"][-1] = 30000
    invalid_task = task_with_paths(
        invalid_data,
        invalid_root / "invalid_task.json",
        state="state",
        report="report.json",
        segments="segments.json",
    )
    invalid = run_cli(invalid_task)
    payload = json.loads(invalid.stdout)
    checks["invalid_input"] = {
        "exit_code": invalid.returncode,
        "code": payload["error"]["code"],
        "no_state": not (invalid_root / "state").exists(),
        "no_report": not (invalid_root / "report.json").exists(),
    }

    required_checks = [
        checks["baseline_exit_0"],
        checks["concurrency1_equal"],
        checks["low_memory_equal"],
        checks["incremental_equal"],
        checks["append_tail_equal"],
        checks["graceful_interrupt"]["equal"],
        checks["sigterm"]["equal"],
        checks["sigkill"]["equal"],
        checks["parameter_change"]["equal_fresh"],
        checks["invalid_input"]["no_state"],
        checks["invalid_input"]["no_report"],
    ]
    result = {
        "status": "passed" if all(required_checks) else "failed",
        "python": sys.version.split()[0],
        "numpy": np.__version__,
        "seed": manifest["seed"],
        "sample_files": 7,
        "timings_seconds": timings,
        "checks": checks,
    }
    output = WORK / "acceptance_results.json"
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
