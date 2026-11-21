#!/usr/bin/env python3
"""End-to-end reproducible demo: HTTP service, repeat runs, crash, resume."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parent
ARTIFACTS = ROOT / "artifacts" / "demo"
BASE_SCENARIO = ROOT / "scenarios" / "demo.json"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def run_cli(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, "-m", "capbench", *args]
    print("+", " ".join(command), flush=True)
    return subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=check,
    )


def wait_health(port: int, process: subprocess.Popen[bytes], timeout_s: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_s
    url = f"http://127.0.0.1:{port}/healthz"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"test service exited with {process.returncode}")
        try:
            with urlopen(url, timeout=0.5) as response:
                if response.status == 200:
                    return
        except (URLError, OSError):
            time.sleep(0.1)
    raise RuntimeError("test service did not become healthy")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="keep existing demo artifacts")
    args = parser.parse_args()
    if ARTIFACTS.exists() and not args.keep:
        shutil.rmtree(ARTIFACTS)
    ARTIFACTS.mkdir(parents=True, exist_ok=True)

    port = free_port()
    scenario = json.loads(BASE_SCENARIO.read_text(encoding="utf-8"))
    scenario["services"][0]["port"] = port
    scenario["services"][0]["refused_port"] = free_port()
    scenario_path = ARTIFACTS / "scenario.http.json"
    scenario_path.write_text(
        json.dumps(scenario, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    service_log = (ARTIFACTS / "test_service.log").open("wb")
    service = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "capbench.test_service",
            str(scenario_path),
        ],
        cwd=ROOT,
        stdout=service_log,
        stderr=subprocess.STDOUT,
    )
    try:
        wait_health(port, service)
        print(f"HTTP target ready on 127.0.0.1:{port}", flush=True)

        baseline_db = ARTIFACTS / "baseline.sqlite"
        baseline_report = ARTIFACTS / "baseline.report.json"
        first = run_cli(
            "run",
            "--scenario",
            str(scenario_path),
            "--db",
            str(baseline_db),
            "--report",
            str(baseline_report),
            "--workers",
            "4",
        )
        print(first.stdout.strip())

        repeat_db = ARTIFACTS / "repeat.sqlite"
        repeat_report = ARTIFACTS / "repeat.report.json"
        second = run_cli(
            "run",
            "--scenario",
            str(scenario_path),
            "--db",
            str(repeat_db),
            "--report",
            str(repeat_report),
            "--workers",
            "4",
        )
        print(second.stdout.strip())

        interrupted_db = ARTIFACTS / "interrupted.sqlite"
        crashed = run_cli(
            "run",
            "--scenario",
            str(scenario_path),
            "--db",
            str(interrupted_db),
            "--workers",
            "4",
            "--crash-after-claims",
            "10",
            check=False,
        )
        if crashed.returncode != 137:
            raise RuntimeError(
                f"expected simulated hard exit 137, got {crashed.returncode}"
            )
        interrupted_status = run_cli(
            "status",
            "--db",
            str(interrupted_db),
            "--format",
            "json",
        ).stdout
        (ARTIFACTS / "interrupted.status.json").write_text(
            interrupted_status,
            encoding="utf-8",
        )

        resumed_report = ARTIFACTS / "resumed.report.json"
        resumed = run_cli(
            "resume",
            "--db",
            str(interrupted_db),
            "--report",
            str(resumed_report),
            "--workers",
            "4",
        )
        print(resumed.stdout.strip())

        for db, report in (
            (baseline_db, baseline_report),
            (repeat_db, repeat_report),
            (interrupted_db, resumed_report),
        ):
            verified = run_cli(
                "verify",
                "--db",
                str(db),
                "--report",
                str(report),
            )
            payload = json.loads(verified.stdout)
            if not payload["ok"]:
                raise RuntimeError(f"verify failed: {payload}")

        baseline_json = json.loads(baseline_report.read_text(encoding="utf-8"))
        repeat_json = json.loads(repeat_report.read_text(encoding="utf-8"))
        resumed_json = json.loads(resumed_report.read_text(encoding="utf-8"))
        repeat_equal = baseline_json == repeat_json
        resume_equal = baseline_json == resumed_json
        comparison = {
            "ok": repeat_equal and resume_equal,
            "repeat_run_equal": repeat_equal,
            "resume_run_equal": resume_equal,
            "baseline_sha256": sha256_file(baseline_report),
            "repeat_sha256": sha256_file(repeat_report),
            "resumed_sha256": sha256_file(resumed_report),
            "tenants": {
                tenant["tenant"]: {
                    "status": tenant["capacity_conclusion"]["status"],
                    "capacity_concurrency": tenant["capacity_conclusion"][
                        "capacity_concurrency"
                    ],
                    "degraded": tenant["capacity_conclusion"]["degraded_capacity"],
                    "aborted_at": tenant["capacity_conclusion"]["aborted_at"],
                }
                for tenant in baseline_json["tenants"]
            },
            "run": baseline_json["run"],
        }
        (ARTIFACTS / "comparison.json").write_text(
            json.dumps(comparison, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(comparison, ensure_ascii=False, sort_keys=True, indent=2))
        if not comparison["ok"]:
            return 1
        return 0
    finally:
        service.terminate()
        try:
            service.wait(timeout=5)
        except subprocess.TimeoutExpired:
            service.kill()
            service.wait(timeout=5)
        service_log.close()


if __name__ == "__main__":
    raise SystemExit(main())

