#!/usr/bin/env python3
"""Run the fixed local sample through determinism, recovery, and concurrency checks."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lbdiag.model import parse_sample_file  # noqa: E402
from lbdiag.storage import Store  # noqa: E402

SAMPLE = ROOT / "samples" / "diagnostic_sample.json"
REPORT = ROOT / "artifacts" / "validation_report.json"


def run_cli(
    *args: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        [sys.executable, "-m", "lbdiag", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=merged,
        check=False,
    )


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def start_service() -> subprocess.Popen[str]:
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "lbdiag",
            "serve",
            "--sample",
            str(SAMPLE),
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    line = process.stdout.readline()
    if not line:
        stderr = "" if process.stderr is None else process.stderr.read()
        process.kill()
        raise RuntimeError(f"test service did not start: {stderr}")
    ready = json.loads(line)
    if not ready.get("ready"):
        process.kill()
        raise RuntimeError(f"test service readiness failed: {ready}")
    return process


def stop_service(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def parse_json_output(completed: subprocess.CompletedProcess[str], label: str) -> dict[str, Any]:
    if not completed.stdout.strip():
        raise RuntimeError(f"{label} produced no stdout; stderr={completed.stderr}")
    return json.loads(completed.stdout)


def main() -> int:
    config = parse_sample_file(SAMPLE)
    work_root = ROOT / "artifacts" / "validation_work"
    if work_root.exists():
        shutil.rmtree(work_root)
    work_root.mkdir(parents=True)
    service = start_service()
    report: dict[str, Any] = {
        "sample_sha256": config.sample_sha256,
        "python_version": sys.version.split()[0],
        "checks": {},
    }
    try:
        run_a = work_root / "run_a.db"
        run_b = work_root / "run_b.db"
        first = run_cli(
            "run",
            "--sample",
            str(SAMPLE),
            "--db",
            str(run_a),
            "--log-dir",
            str(work_root / "logs_a"),
        )
        second = run_cli(
            "run",
            "--sample",
            str(SAMPLE),
            "--db",
            str(run_b),
            "--log-dir",
            str(work_root / "logs_b"),
        )
        if first.returncode != 0:
            raise RuntimeError(f"first run failed: {first.stdout} {first.stderr}")
        if second.returncode != 0:
            raise RuntimeError(f"second run failed: {second.stdout} {second.stderr}")
        first_json = parse_json_output(first, "run A")
        second_json = parse_json_output(second, "run B")
        repeat = run_cli(
            "run",
            "--sample",
            str(SAMPLE),
            "--db",
            str(run_a),
            "--log-dir",
            str(work_root / "logs_a"),
        )
        status_a = run_cli(
            "status",
            "--sample",
            str(SAMPLE),
            "--db",
            str(run_a),
            "--log-dir",
            str(work_root / "logs_status"),
        )
        status_b = run_cli(
            "status",
            "--sample",
            str(SAMPLE),
            "--db",
            str(run_a),
            "--log-dir",
            str(work_root / "logs_status"),
        )
        explain = run_cli(
            "explain",
            "--sample",
            str(SAMPLE),
            "--db",
            str(run_a),
            "--log-dir",
            str(work_root / "logs_explain"),
            "--instance",
            "beta",
            "--window",
            "0",
        )
        verify = run_cli(
            "verify",
            "--sample",
            str(SAMPLE),
            "--db",
            str(run_a),
            "--log-dir",
            str(work_root / "logs_verify"),
        )
        if repeat.returncode != 0:
            raise RuntimeError(f"idempotent rerun failed: {repeat.stdout} {repeat.stderr}")
        if status_a.returncode != 0 or status_b.returncode != 0:
            raise RuntimeError(f"status failed: {status_a.stdout} {status_a.stderr}")
        if explain.returncode != 0:
            raise RuntimeError(f"explain failed: {explain.stdout} {explain.stderr}")
        if verify.returncode != 0:
            raise RuntimeError(f"verify failed: {verify.stdout} {verify.stderr}")

        status_json = parse_json_output(status_a, "status")
        explain_json = parse_json_output(explain, "explain")
        verify_json = parse_json_output(verify, "verify")
        snapshot = status_json["result"]
        states = {item["instance_id"]: item for item in snapshot["instances"]}
        latest_codes = {
            item["instance_id"]: item["latest_check"]["error_code"]
            for item in snapshot["instances"]
        }
        sensitive_ids = sorted(
            item["pair_id"]
            for item in snapshot["sensitivity"]
            if item["precision_sensitive"]
        )
        report["checks"]["deterministic_runs"] = {
            "ok": (
                first.stdout == second.stdout
                and first.stdout == repeat.stdout
                and first_json == second_json
            ),
            "run_output_sha256": sha256_bytes(first.stdout.encode("utf-8")),
            "repeat_run_byte_identical": first.stdout == repeat.stdout,
        }
        report["checks"]["stable_status"] = {
            "ok": status_a.stdout == status_b.stdout,
            "status_output_sha256": sha256_bytes(status_a.stdout.encode("utf-8")),
        }
        report["checks"]["verify"] = {
            "ok": bool(verify_json["result"]["ok"]),
            "error_count": len(verify_json["result"]["errors"]),
        }
        report["checks"]["precision_sensitivity"] = {
            "ok": len(sensitive_ids) == 3
            and "alpha|beta|w0" in sensitive_ids,
            "sensitive_pair_ids": sensitive_ids,
            "beta_window_0": {
                "precision_sensitive": explain_json["result"]["records"][0][
                    "precision_sensitive"
                ],
                "score_difference": explain_json["result"]["records"][0][
                    "score_difference"
                ]["raw_float"],
                "tolerance": explain_json["result"]["records"][0]["tolerance"][
                    "raw_float"
                ],
                "ordering_margin": explain_json["result"]["records"][0][
                    "ordering_margin"
                ]["raw_float"],
                "allocation_error_upper_bound": explain_json["result"]["records"][0][
                    "cumulative_error_upper_bound"
                ]["effective_weight_units"]["raw_float"],
                "chain_steps": [
                    item["step"]
                    for item in explain_json["result"]["records"][0][
                        "error_source_chain"
                    ]
                ],
            },
        }
        report["checks"]["classification_and_health"] = {
            "ok": (
                latest_codes["alpha"] == "E_OK"
                and latest_codes["beta"] == "E_STATUS_MISMATCH"
                and latest_codes["gamma"] == "E_STATUS_MISMATCH"
                and latest_codes["delta"] == "E_TIMEOUT"
                and latest_codes["epsilon"] == "E_CONN_REFUSED"
                and latest_codes["zeta"] == "E_MALFORMED_RESPONSE"
                and latest_codes["eta"] == "E_SAMPLE_MISMATCH"
                and states["alpha"]["state"] == "HEALTHY"
                and states["gamma"]["state"] == "UNHEALTHY"
            ),
            "latest_error_codes": latest_codes,
            "states": {
                instance_id: {
                    "state": item["state"],
                    "consecutive_failures": item["consecutive_failures"],
                    "consecutive_successes": item["consecutive_successes"],
                    "effective_weight": item["effective_weight"]["raw_float"],
                    "committed_windows": item["committed_windows"],
                }
                for instance_id, item in sorted(states.items())
            },
        }

        crash_db = work_root / "crash.db"
        crash = run_cli(
            "run",
            "--sample",
            str(SAMPLE),
            "--db",
            str(crash_db),
            "--log-dir",
            str(work_root / "logs_crash"),
            env={"LBDIAG_TEST_CRASH_AFTER_PROBES": "12"},
        )
        if crash.returncode == 0:
            raise RuntimeError("crash injection did not terminate the process")
        pending_verify = run_cli(
            "verify",
            "--sample",
            str(SAMPLE),
            "--db",
            str(crash_db),
            "--log-dir",
            str(work_root / "logs_crash"),
        )
        store = Store(crash_db, create=False)
        try:
            crash_checks = len(store.get_checks("alpha"))
            crash_windows = store.committed_window_count("alpha")
            crash_state = store.get_state("alpha")
        finally:
            store.close()
        resume = run_cli(
            "resume",
            "--sample",
            str(SAMPLE),
            "--db",
            str(crash_db),
            "--log-dir",
            str(work_root / "logs_resume"),
        )
        resume_verify = run_cli(
            "verify",
            "--sample",
            str(SAMPLE),
            "--db",
            str(crash_db),
            "--log-dir",
            str(work_root / "logs_resume"),
        )
        if pending_verify.returncode != 0:
            raise RuntimeError(f"pending verify failed: {pending_verify.stdout}")
        if resume.returncode != 0:
            raise RuntimeError(f"resume failed: {resume.stdout} {resume.stderr}")
        if resume_verify.returncode != 0:
            raise RuntimeError(f"post-resume verify failed: {resume_verify.stdout}")
        store = Store(crash_db, create=False)
        try:
            resumed_checks = len(store.get_checks("alpha"))
            resumed_windows = store.committed_window_count("alpha")
            resumed_state = store.get_state("alpha")
        finally:
            store.close()
        report["checks"]["crash_recovery"] = {
            "ok": (
                crash.returncode != 0
                and crash_checks == 12
                and crash_windows == 1
                and int(crash_state["consecutive_successes"]) == 8
                and resumed_checks == 16
                and resumed_windows == 3
                and int(resumed_state["consecutive_successes"]) == 16
            ),
            "crash_exit_code": crash.returncode,
            "before_resume": {
                "checks": crash_checks,
                "committed_windows": crash_windows,
                "consecutive_successes": int(crash_state["consecutive_successes"]),
                "pending_verify_ok": bool(
                    json.loads(pending_verify.stdout)["result"]["ok"]
                ),
            },
            "after_resume": {
                "checks": resumed_checks,
                "committed_windows": resumed_windows,
                "consecutive_successes": int(
                    resumed_state["consecutive_successes"]
                ),
                "verify_ok": bool(json.loads(resume_verify.stdout)["result"]["ok"]),
            },
        }

        parallel_db = work_root / "parallel.db"
        common = [
            "--sample",
            str(SAMPLE),
            "--db",
            str(parallel_db),
            "--log-dir",
            str(work_root / "logs_parallel"),
        ]
        process_a = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "lbdiag",
                "run",
                *common,
                "--instance",
                "alpha",
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        process_b = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "lbdiag",
                "run",
                *common,
                "--instance",
                "beta",
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        out_a, err_a = process_a.communicate(timeout=30)
        out_b, err_b = process_b.communicate(timeout=30)
        if process_a.returncode != 0 or process_b.returncode != 0:
            raise RuntimeError(f"parallel run failed: {out_a} {err_a} {out_b} {err_b}")
        parallel_verify = run_cli(
            "verify",
            "--sample",
            str(SAMPLE),
            "--db",
            str(parallel_db),
            "--log-dir",
            str(work_root / "logs_parallel"),
        )
        store = Store(parallel_db, create=False)
        try:
            parallel_alpha = store.get_state("alpha")
            parallel_beta = store.get_state("beta")
            parallel_windows = store.committed_window_count("alpha")
            parallel_snapshot = store.snapshot(config)
        finally:
            store.close()
        parallel_sensitive = [
            item
            for item in parallel_snapshot["sensitivity"]
            if item["precision_sensitive"]
        ]
        report["checks"]["parallel_idempotency"] = {
            "ok": (
                parallel_verify.returncode == 0
                and int(parallel_alpha["consecutive_successes"]) == 16
                and int(parallel_beta["consecutive_failures"]) == 1
                and parallel_windows == 3
                and len(parallel_sensitive) == 3
            ),
            "alpha_consecutive_successes": int(
                parallel_alpha["consecutive_successes"]
            ),
            "beta_consecutive_failures": int(
                parallel_beta["consecutive_failures"]
            ),
            "alpha_windows": parallel_windows,
            "sensitive_pair_count": len(parallel_sensitive),
            "verify_ok": bool(json.loads(parallel_verify.stdout)["result"]["ok"]),
        }
        report["ok"] = all(
            bool(item.get("ok")) for item in report["checks"].values()
        )
    finally:
        stop_service(service)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(
        json.dumps(report, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, sort_keys=True, indent=2, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

