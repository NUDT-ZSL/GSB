#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def _run_cli(*args: str, env: dict[str, str] | None = None) -> dict[str, Any]:
    command = [sys.executable, "-m", "gateway.cli", *args]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"CLI command failed ({completed.returncode}): {' '.join(command)}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return json.loads(completed.stdout)


def _spawn_cli(
    *args: str,
    env: dict[str, str],
) -> subprocess.Popen[str]:
    command = [sys.executable, "-m", "gateway.cli", *args]
    return subprocess.Popen(
        command,
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _load(
    db: Path,
    *,
    with_events: bool,
) -> dict[str, Any]:
    args = [
        "--db",
        str(db),
        "load",
        "--topics",
        str(ROOT / "samples" / "topics.json"),
        "--subscriptions",
        str(ROOT / "samples" / "subscriptions.json"),
        "--config",
        str(ROOT / "samples" / "config.json"),
    ]
    if with_events:
        args.extend(["--events", str(ROOT / "samples" / "events.json")])
    return _run_cli(*args)


def _status(db: Path) -> dict[str, Any]:
    return _run_cli("--db", str(db), "status")


def _verify(db: Path) -> dict[str, Any]:
    return _run_cli("--db", str(db), "verify")


def _kill_when_marker(
    process: subprocess.Popen[str],
    marker: Path,
    *,
    timeout_seconds: float,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if marker.exists():
            process.kill()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=10)
            return True
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=2)
            raise RuntimeError(
                "child exited before the fault marker was observed\n"
                f"returncode={process.returncode}\n"
                f"stdout:\n{stdout}\nstderr:\n{stderr}"
            )
        time.sleep(0.02)
    process.kill()
    process.wait(timeout=10)
    raise RuntimeError("timed out waiting for the child fault marker")


def _fault_env(
    fault_point: str,
    marker: Path,
) -> dict[str, str]:
    env = os.environ.copy()
    env["GATEWAY_FAULT_POINT"] = fault_point
    env["GATEWAY_FAULT_MARKER"] = str(marker)
    env["GATEWAY_FAULT_DELAY_SECONDS"] = "60"
    return env


def _deliver_scenario(
    fault_point: str,
    output: Path,
    *,
    subscriber: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    baseline_db = output / "baseline.db"
    crash_db = output / "crash.db"
    _load(baseline_db, with_events=True)
    _load(crash_db, with_events=True)

    round_id = "kill-demo-round"
    baseline_delivery = _run_cli(
        "--db",
        str(baseline_db),
        "deliver",
        "--subscriber",
        subscriber,
        "--round-id",
        round_id,
    )
    baseline_status = _status(baseline_db)

    marker = output / "fault.marker"
    if marker.exists():
        marker.unlink()
    child = _spawn_cli(
        "--db",
        str(crash_db),
        "deliver",
        "--subscriber",
        subscriber,
        "--round-id",
        round_id,
        env=_fault_env(fault_point, marker),
    )
    killed = _kill_when_marker(
        child,
        marker,
        timeout_seconds=timeout_seconds,
    )
    resume = _run_cli(
        "--db",
        str(crash_db),
        "resume",
        "--subscriber",
        subscriber,
        "--round-id",
        round_id,
    )
    recovered_status = _status(crash_db)
    verification = _verify(crash_db)
    equal = (
        baseline_status["ledger_fingerprint"]
        == recovered_status["ledger_fingerprint"]
    )
    return {
        "scenario": "deliver",
        "fault_point": fault_point,
        "process_killed": killed,
        "baseline_summary": baseline_delivery["summary"],
        "resume_recovered": resume["recovered"],
        "resume_summary": resume["delivery"]["summary"],
        "baseline_fingerprint": baseline_status["ledger_fingerprint"],
        "recovered_fingerprint": recovered_status["ledger_fingerprint"],
        "fingerprints_equal": equal,
        "verify_ok": verification["ok"],
        "verify_failures": verification["failures"],
    }


def _append_scenario(
    fault_point: str,
    output: Path,
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    baseline_db = output / "baseline-append.db"
    crash_db = output / "crash-append.db"
    _load(baseline_db, with_events=False)
    _load(crash_db, with_events=False)

    events = str(ROOT / "samples" / "events.json")
    baseline_produce = _run_cli(
        "--db",
        str(baseline_db),
        "produce",
        "--events",
        events,
    )
    baseline_status = _status(baseline_db)

    marker = output / "append-fault.marker"
    if marker.exists():
        marker.unlink()
    child = _spawn_cli(
        "--db",
        str(crash_db),
        "produce",
        "--events",
        events,
        env=_fault_env(fault_point, marker),
    )
    killed = _kill_when_marker(
        child,
        marker,
        timeout_seconds=timeout_seconds,
    )
    replay = _run_cli(
        "--db",
        str(crash_db),
        "produce",
        "--events",
        events,
    )
    recovered_status = _status(crash_db)
    verification = _verify(crash_db)
    equal = (
        baseline_status["ledger_fingerprint"]
        == recovered_status["ledger_fingerprint"]
    )
    return {
        "scenario": "append",
        "fault_point": fault_point,
        "process_killed": killed,
        "baseline_appended": baseline_produce["appended"],
        "replay_appended": replay["appended"],
        "baseline_fingerprint": baseline_status["ledger_fingerprint"],
        "recovered_fingerprint": recovered_status["ledger_fingerprint"],
        "fingerprints_equal": equal,
        "verify_ok": verification["ok"],
        "verify_failures": verification["failures"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Kill a gateway child process at a deterministic fault point, "
            "recover it, and compare the ledger with an uninterrupted baseline."
        )
    )
    parser.add_argument(
        "--scenario",
        choices=["append", "deliver", "all"],
        default="deliver",
    )
    parser.add_argument(
        "--fault-point",
        default="deliver_after_reserve",
        choices=[
            "append_before_commit",
            "deliver_after_reserve",
            "deliver_before_commit",
            "deliver_after_commit",
        ],
    )
    parser.add_argument("--subscriber", default="sub-orders-a")
    parser.add_argument(
        "--output",
        default=str(ROOT / "artifacts" / "recovery-demo"),
    )
    parser.add_argument("--keep-existing", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    args = parser.parse_args(argv)

    output = Path(args.output).resolve()
    if output.exists() and not args.keep_existing:
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)

    scenarios: list[dict[str, Any]] = []
    if args.scenario in {"deliver", "all"}:
        scenarios.append(
            _deliver_scenario(
                args.fault_point
                if args.fault_point.startswith("deliver_")
                else "deliver_after_reserve",
                output,
                subscriber=args.subscriber,
                timeout_seconds=args.timeout_seconds,
            )
        )
    if args.scenario in {"append", "all"}:
        scenarios.append(
            _append_scenario(
                "append_before_commit",
                output,
                timeout_seconds=args.timeout_seconds,
            )
        )

    ok = all(
        item["process_killed"]
        and item["fingerprints_equal"]
        and item["verify_ok"]
        for item in scenarios
    )
    result = {
        "ok": ok,
        "output": str(output),
        "scenarios": scenarios,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
