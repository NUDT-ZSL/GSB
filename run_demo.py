#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DEMO_ROOT = ROOT / "artifacts" / "demo"
SAMPLES = ROOT / "samples"
SAMPLES_V2 = SAMPLES / "v2"
RUN_ID = "demo"
PLAN = "payment-failover"


def canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def stable(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


def reset_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def run_cli(
    state_dir: Path,
    *args: str,
    expect_json: bool = True,
) -> tuple[int, dict[str, Any] | None, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    command = [
        sys.executable,
        "-m",
        "drillbench",
        *args,
        "--db",
        str(state_dir / "state.db"),
    ]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )
    payload: dict[str, Any] | None = None
    if expect_json and completed.stdout.strip():
        payload = json.loads(completed.stdout)
    return completed.returncode, payload, completed.stderr


def load_sample(state_dir: Path, *, sample_dir: Path = SAMPLES) -> dict[str, Any]:
    code, payload, stderr = run_cli(
        state_dir,
        "load",
        "--sample-dir",
        str(sample_dir),
        "--plan",
        PLAN,
    )
    if code != 0 or payload is None or not payload.get("ok"):
        raise RuntimeError(f"load failed: {code} {payload} {stderr}")
    return payload


def run_drill(state_dir: Path, *extra: str) -> dict[str, Any]:
    code, payload, stderr = run_cli(
        state_dir,
        "run",
        "--run-id",
        RUN_ID,
        "--plan",
        PLAN,
        "--skip-step",
        "4",
        *extra,
    )
    if code != 0 or payload is None or not payload.get("ok"):
        raise RuntimeError(f"run failed: {code} {payload} {stderr}")
    return payload


def resume_drill(state_dir: Path) -> dict[str, Any]:
    code, payload, stderr = run_cli(
        state_dir,
        "resume",
        "--run-id",
        RUN_ID,
        "--plan",
        PLAN,
        "--skip-step",
        "4",
    )
    if code != 0 or payload is None or not payload.get("ok"):
        raise RuntimeError(f"resume failed: {code} {payload} {stderr}")
    return payload


def get_status(state_dir: Path) -> dict[str, Any]:
    code, payload, stderr = run_cli(
        state_dir,
        "status",
        "--run-id",
        RUN_ID,
    )
    if code != 0 or payload is None:
        raise RuntimeError(f"status failed: {code} {payload} {stderr}")
    return payload


def get_verify(state_dir: Path) -> dict[str, Any]:
    code, payload, stderr = run_cli(
        state_dir,
        "verify",
        "--run-id",
        RUN_ID,
        "--samples-dir",
        str(SAMPLES),
    )
    if code != 0 or payload is None:
        raise RuntimeError(f"verify failed: {code} {payload} {stderr}")
    return payload


def get_injections(state_dir: Path) -> dict[str, Any]:
    code, payload, stderr = run_cli(
        state_dir,
        "inject",
        "--run-id",
        RUN_ID,
    )
    if code != 0 or payload is None:
        raise RuntimeError(f"inject failed: {code} {payload} {stderr}")
    return payload


def write_dashboard(state_dir: Path, target: Path) -> str:
    code, payload, stderr = run_cli(
        state_dir,
        "status",
        "--run-id",
        RUN_ID,
        "--format",
        "html",
        "--output",
        str(target),
    )
    if code != 0 or payload is None or not payload.get("written"):
        raise RuntimeError(f"dashboard failed: {code} {payload} {stderr}")
    return hashlib.sha256(target.read_bytes()).hexdigest()


def scenario(*, crash: bool, state_dir: Path) -> dict[str, Any]:
    reset_dir(state_dir)
    loaded = load_sample(state_dir)

    crash_exit: int | None = None
    if crash:
        crash_exit, _, crash_stderr = run_cli(
            state_dir,
            "run",
            "--run-id",
            RUN_ID,
            "--plan",
            PLAN,
            "--skip-step",
            "4",
            "--crash-at-step",
            "2",
            "--crash-phase",
            "after-response",
            expect_json=False,
        )
        if crash_exit == 0:
            raise RuntimeError(
                f"crash injection unexpectedly succeeded: {crash_stderr}"
            )
        status_during_crash = get_status(state_dir)
        resumed = resume_drill(state_dir)
    else:
        run_result = run_drill(state_dir)
        status_during_crash = None
        resumed = {
            "recovery": {
                "verdict": "NO_INTERRUPTION",
                "half_committed_steps": [],
            },
            "executed_steps": run_result["executed_steps"],
        }

    injection_state = get_injections(state_dir)
    final_status = get_status(state_dir)
    verification = get_verify(state_dir)
    dashboard_path = state_dir / "dashboard.html"
    dashboard_sha = write_dashboard(state_dir, dashboard_path)
    return {
        "load": loaded,
        "crash_exit_code": crash_exit,
        "status_during_crash": (
            {
                "run_state": status_during_crash["run"]["state"],
                "logical_clock_ms": status_during_crash["run"]["logical_clock_ms"],
                "running_steps": [
                    step["seq"]
                    for step in status_during_crash["steps"]
                    if step["stage"] == "RUNNING"
                ],
                "pending_injections": [
                    injection["id"]
                    for injection in status_during_crash["injections"]
                    if not injection["consumed"]
                ],
            }
            if status_during_crash is not None
            else None
        ),
        "resume": resumed,
        "inject": injection_state,
        "status": final_status,
        "verify": verification,
        "dashboard_sha256": dashboard_sha,
    }


def replacement_demo(state_dir: Path) -> dict[str, Any]:
    replaced = load_sample(state_dir, sample_dir=SAMPLES_V2)
    rollback_code, rollback_payload, _ = run_cli(
        state_dir,
        "load",
        "--sample-dir",
        str(SAMPLES),
        "--plan",
        PLAN,
        "--requested-version",
        "1",
    )
    status = get_status(state_dir)
    return {
        "load_v2": replaced,
        "rollback_attempt": {
            "exit_code": rollback_code,
            "error_code": (
                rollback_payload["error"]["code"]
                if rollback_payload and "error" in rollback_payload
                else None
            ),
        },
        "bound_run": {
            "run_version": status["run"]["version"],
            "current_snapshot_version": status["run"][
                "current_snapshot_version"
            ],
            "bound_version_is_current": status["run"][
                "bound_version_is_current"
            ],
        },
    }


def main() -> int:
    DEMO_ROOT.mkdir(parents=True, exist_ok=True)
    scenario_a = scenario(crash=True, state_dir=DEMO_ROOT / "run-a")
    scenario_b = scenario(crash=True, state_dir=DEMO_ROOT / "run-b")
    clean = scenario(crash=False, state_dir=DEMO_ROOT / "clean")
    shutil.copyfile(
        DEMO_ROOT / "run-a" / "dashboard.html",
        ROOT / "artifacts" / "dashboard.html",
    )

    identical = canonical(scenario_a) == canonical(scenario_b)
    resumed_matches_clean_status = canonical(
        scenario_a["status"]
    ) == canonical(clean["status"])
    resumed_matches_clean_verify = canonical(
        scenario_a["verify"]
    ) == canonical(clean["verify"])
    replacement = replacement_demo(DEMO_ROOT / "run-a")

    scenario_digest = hashlib.sha256(
        canonical(scenario_a).encode("utf-8")
    ).hexdigest()
    result = {
        "ok": True,
        "acceptance": {
            "crash_exit_nonzero": scenario_a["crash_exit_code"] not in {None, 0},
            "half_committed_step_recovered": (
                scenario_a["resume"]["recovery"]["half_committed_steps"] == [2]
            ),
            "resumed_matches_clean_status": resumed_matches_clean_status,
            "resumed_matches_clean_verify": resumed_matches_clean_verify,
            "two_scenarios_identical": identical,
            "version_replacement": replacement["load_v2"]["change_kind"],
            "rollback_rejected_code": replacement["rollback_attempt"]["error_code"],
            "sample_manifest_verified": any(
                check["name"] == "sample_manifest" and check["ok"]
                for check in scenario_a["verify"]["checks"]
            ),
        },
        "determinism": {
            "scenario_sha256": scenario_digest,
            "identical": identical,
        },
        "scenario": scenario_a,
        "replacement": replacement,
    }
    output_path = ROOT / "artifacts" / "demo_output.json"
    output_path.write_text(stable(result) + "\n", encoding="utf-8")
    sys.stdout.write(stable(result) + "\n")
    return 0 if all(
        value is True or value in {
            "FULL_REPLACEMENT",
            "E_VERSION_ROLLBACK",
        }
        for value in result["acceptance"].values()
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())

