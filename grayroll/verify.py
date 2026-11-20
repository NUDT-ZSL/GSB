from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .errors import GrayrollError
from .store import Store
from .util import canonical_bytes, sha256_bytes

UNTIL_MS = 1700000012000


def _run_cli(
    project_root: Path,
    state: Path,
    arguments: list[str],
    *,
    crash_at: str | None = None,
) -> subprocess.CompletedProcess[bytes]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(project_root)
    env.pop("GRAYROLL_CRASH_AT", None)
    if crash_at:
        env["GRAYROLL_CRASH_AT"] = crash_at
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "grayroll",
            "--state",
            str(state),
            *arguments,
        ],
        cwd=project_root,
        env=env,
        capture_output=True,
        check=False,
    )


def _spawn_cli(
    project_root: Path,
    state: Path,
    arguments: list[str],
) -> subprocess.Popen[bytes]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(project_root)
    env.pop("GRAYROLL_CRASH_AT", None)
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "grayroll",
            "--state",
            str(state),
            *arguments,
        ],
        cwd=project_root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _require_success(
    result: subprocess.CompletedProcess[bytes], operation: str
) -> dict[str, Any]:
    if result.returncode != 0:
        raise GrayrollError(
            "E_VERIFY_FAILED",
            f"{operation} failed with exit code {result.returncode}",
            {
                "stderr": result.stderr.decode("utf-8", errors="replace"),
                "stdout": result.stdout.decode("utf-8", errors="replace"),
            },
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise GrayrollError(
            "E_VERIFY_FAILED",
            f"{operation} did not return JSON",
            {"stdout": result.stdout.decode("utf-8", errors="replace")},
        ) from exc


def _prepare(project_root: Path, samples: Path, state: Path) -> str:
    load = _run_cli(
        project_root,
        state,
        [
            "load",
            "--manifest",
            str(samples / "manifest.json"),
            "--defaults",
            str(samples / "defaults.json"),
        ],
    )
    loaded = _require_success(load, "load")
    package_id = loaded["package_id"]
    merge = _run_cli(project_root, state, ["merge", "--package-id", package_id])
    merged = _require_success(merge, "merge")
    if merged.get("status") != "MERGED":
        raise GrayrollError("E_VERIFY_FAILED", "merge did not activate package")
    return package_id


def _execute_run(
    project_root: Path,
    samples: Path,
    state: Path,
    *,
    crash: bool,
) -> tuple[bytes, bytes]:
    _prepare(project_root, samples, state)
    events = str(samples / "events.jsonl")
    if crash:
        crashed = _run_cli(
            project_root,
            state,
            ["ramp", "--until", str(UNTIL_MS), "--events", events],
            crash_at="rollback_intent",
        )
        if crashed.returncode != 75:
            raise GrayrollError(
                "E_VERIFY_FAILED",
                "forced-termination ramp did not exit at the injected crash point",
                {
                    "exit_code": crashed.returncode,
                    "stderr": crashed.stderr.decode("utf-8", errors="replace"),
                },
            )
        processes = [
            _spawn_cli(
                project_root,
                state,
                ["resume", "--until", str(UNTIL_MS), "--events", events],
            )
            for _ in range(2)
        ]
        for process_index, process in enumerate(processes, 1):
            stdout, stderr = process.communicate()
            result = subprocess.CompletedProcess(
                process.args, process.returncode, stdout, stderr
            )
            _require_success(result, f"parallel resume {process_index}")
    else:
        ramped = _run_cli(
            project_root,
            state,
            ["ramp", "--until", str(UNTIL_MS), "--events", events],
        )
        _require_success(ramped, "ramp")

    status_result = _run_cli(project_root, state, ["status"])
    _require_success(status_result, "status")
    store = Store(state)
    try:
        ledger_bytes = canonical_bytes(store.export_ledger()) + b"\n"
    finally:
        store.close()
    return status_result.stdout, ledger_bytes


def _assert_expected(
    state: Path,
    status_bytes: bytes,
) -> None:
    status = json.loads(status_bytes)
    batches = {batch["batch_id"]: batch for batch in status["batches"]}
    expected_stages = {
        "1": "ROLLED_BACK",
        "2": "STABLE",
        "3": "STABLE",
        "4": "ROLLED_BACK",
    }
    actual_stages = {
        batch_id: batches.get(batch_id, {}).get("stage") for batch_id in expected_stages
    }
    if actual_stages != expected_stages:
        raise GrayrollError(
            "E_VERIFY_FAILED",
            "final batch stages do not match the expected deterministic scenario",
            {"actual": actual_stages, "expected": expected_stages},
        )
    if status["clock"]["logical_watermark_ms"] != UNTIL_MS:
        raise GrayrollError(
            "E_VERIFY_FAILED", "logical clock watermark was not persisted"
        )
    if status["rhythm_version"] != 1:
        raise GrayrollError("E_VERIFY_FAILED", "unexpected rhythm version")
    if batches["3"]["source_version"] != "v1":
        raise GrayrollError("E_VERIFY_FAILED", "v1-only batch was not migrated")
    if batches["1"]["source_version"] != "v2":
        raise GrayrollError("E_VERIFY_FAILED", "cross-version winner was not v2")
    if batches["1"]["rollback"]["status"] != "SUCCEEDED":
        raise GrayrollError("E_VERIFY_FAILED", "manual-abort rollback did not finish")
    if batches["4"]["rollback"]["status"] != "SUCCEEDED":
        raise GrayrollError(
            "E_VERIFY_FAILED", "observation-failure rollback did not finish"
        )
    if batches["4"]["rollback"]["completed_targets"] != [0.15, 0.0]:
        raise GrayrollError(
            "E_VERIFY_FAILED", "rollback did not execute in descending tier order"
        )
    store = Store(state)
    try:
        pointer = store.get_pointer()
        assert pointer is not None
        v1_batch = store.get_package_batch(
            pointer["current_package_id"], "checkout.svc", "3"
        )
        v2_batch = store.get_package_batch(
            pointer["current_package_id"], "checkout.svc", "1"
        )
        assert v1_batch is not None and v2_batch is not None
        earliest = v1_batch["migration"]["earliest_rollout"]["mode"]
        if earliest != "relative_seconds_to_absolute_ms":
            raise GrayrollError(
                "E_VERIFY_FAILED", "v1 clock migration evidence missing"
            )
        if v2_batch["decision"]["decision"] != "higher_logical_clock":
            raise GrayrollError(
                "E_VERIFY_FAILED", "merge arbitration evidence mismatch"
            )
    finally:
        store.close()


def run_verification(
    project_root: Path, samples: Path, workdir: Path
) -> dict[str, Any]:
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    statuses: list[bytes] = []
    ledgers: list[bytes] = []
    for index in (1, 2):
        state = workdir / f"repeat-{index}" / "state.db"
        status_bytes, ledger_bytes = _execute_run(
            project_root, samples, state, crash=True
        )
        _assert_expected(state, status_bytes)
        statuses.append(status_bytes)
        ledgers.append(ledger_bytes)

    status_identical = statuses[0] == statuses[1]
    ledger_identical = ledgers[0] == ledgers[1]
    if not status_identical or not ledger_identical:
        raise GrayrollError(
            "E_VERIFY_FAILED",
            "two fixed-sample runs were not byte-identical",
            {
                "ledger_identical": ledger_identical,
                "status_identical": status_identical,
            },
        )

    clean_state = workdir / "clean" / "state.db"
    clean_status, clean_ledger = _execute_run(
        project_root, samples, clean_state, crash=False
    )
    _assert_expected(clean_state, clean_status)
    clean_status_matches = clean_status == statuses[0]
    clean_ledger_matches = clean_ledger == ledgers[0]
    if not clean_status_matches or not clean_ledger_matches:
        raise GrayrollError(
            "E_VERIFY_FAILED",
            "crash-recovery result differs from uninterrupted execution",
            {
                "clean_ledger_matches": clean_ledger_matches,
                "clean_status_matches": clean_status_matches,
            },
        )

    return {
        "checks": {
            "clean_equals_crash_resume": True,
            "clock_watermark_persisted": True,
            "cross_version_merge": True,
            "descending_rollback": True,
            "forced_termination_recovered": True,
            "parallel_resume_idempotent": True,
            "v1_migration": True,
        },
        "ledger_sha256": sha256_bytes(ledgers[0]),
        "runs": 2,
        "status": "VERIFIED",
        "status_sha256": sha256_bytes(statuses[0]),
        "two_run_byte_identical": True,
    }
