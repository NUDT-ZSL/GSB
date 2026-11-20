#!/usr/bin/env python3
"""Run deterministic acceptance checks and write a machine-readable report."""

from __future__ import annotations

import json
import platform
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deploy_convergence.engine import ConvergenceEngine
from deploy_convergence.errors import EngineError
from deploy_convergence.store import Store


SAMPLES = ROOT / "samples"
ARTIFACTS = ROOT / "artifacts"
WORK = ARTIFACTS / "acceptance"
CONFIG = json.loads((SAMPLES / "config.json").read_text(encoding="utf-8"))
CLOCK = json.loads(
    (SAMPLES / "clock_steps.json").read_text(encoding="utf-8")
)["initial_clock_ms"]
PATHS = CONFIG["paths"]


def engine(
    *,
    state: Path,
    v2_meta: Path | None = None,
    v2_shards: Path | None = None,
    injection: Path | None = None,
    rollback_policy: str | None = None,
) -> ConvergenceEngine:
    return ConvergenceEngine(
        v1_path=SAMPLES / PATHS["v1"],
        v2_meta_path=v2_meta or SAMPLES / PATHS["v2_meta"],
        v2_shards_dir=v2_shards or SAMPLES / PATHS["v2_shards"],
        state_path=state,
        environment_order=CONFIG["environment_order"],
        tolerances=CONFIG["tolerances"],
        clock_tolerance_ms=CONFIG["clock_tolerance_ms"],
        rollback_policy=rollback_policy or CONFIG["rollback_policy"],
        rollback_window_ms=CONFIG["rollback_window_ms"],
        injection_path=injection or SAMPLES / PATHS["injections"],
    )


def enable_injection(stage: str, kind: str, target: Path) -> Path:
    payload = json.loads((SAMPLES / PATHS["injections"]).read_text(encoding="utf-8"))
    for item in payload["injections"]:
        item["enabled"] = item["stage"] == stage and item["kind"] == kind
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return target


def baseline_check() -> tuple[dict[str, Any], EngineError | None]:
    state = WORK / "baseline.sqlite"
    instance = engine(state=state)
    first = instance.run(clock_ms=CLOCK)
    second = instance.run(clock_ms=CLOCK)
    view = first.view
    connection = sqlite3.connect(state)
    try:
        version_count = connection.execute(
            "SELECT COUNT(*) FROM view_versions"
        ).fetchone()[0]
    finally:
        connection.close()
    payload = {
        "steps_executed": first.steps_executed,
        "second_submission_idempotent": second.idempotent,
        "view_id": view["persisted"]["view_id"],
        "commit_seq": view["persisted"]["commit_seq"],
        "view_version_count": version_count,
        "conflict_count": len(view["conflicts"]),
        "migration_count": len(view["migrations"]),
        "degradation_count": len(view["compatibility_degradations"]),
        "first_divergence": view["consistency"]["first_divergence"],
        "gateway_derivation": view["derivations"]["development"]["gateway"],
        "orders_timeout_conflict": next(
            conflict
            for conflict in view["conflicts"]
            if conflict["environment"] == "development"
            and conflict["service"] == "orders-api"
            and conflict["parameter"] == "timeout_ms"
        ),
        "auth_decision_basis": view["environments"]["development"][
            "auth-service"
        ]["decision"]["basis"],
        "tombstoned_service": view["environments"]["development"][
            "legacy-worker"
        ]["tombstoned"],
        "expired_service": view["environments"]["development"][
            "retired-cache"
        ]["expired_at_clock"],
    }

    rollback_error: EngineError | None = None
    try:
        instance.run(clock_ms=CLOCK - 5000)
    except EngineError as error:
        rollback_error = error
    payload["clock_rollback"] = {
        "code": None if rollback_error is None else rollback_error.code,
        "view_unchanged": Store(state).get_current_view()["persisted"]["view_id"]
        == payload["view_id"],
    }
    return payload, rollback_error


def injection_checks() -> list[dict[str, Any]]:
    cases = [
        ("parse", "malformed_yaml_field", "E_BAD_MANIFEST"),
        ("parse", "shard_digest_mismatch", "E_DIGEST_MISMATCH"),
        ("migrate", "migration_failure", "E_MIGRATION"),
        ("merge", "generation_conflict", "E_GENERATION_CONFLICT"),
        ("evaluate", "dependency_cycle", "E_DEPENDENCY_CYCLE"),
        ("evaluate", "clock_rollback", "E_CLOCK_ROLLBACK"),
    ]
    state = WORK / "injections.sqlite"
    baseline = engine(state=state).run(clock_ms=CLOCK)
    view_id = baseline.view["persisted"]["view_id"]
    watermark = Store(state).status()["clock_watermark_ms"]
    output = []
    for stage, kind, expected_code in cases:
        injection_path = enable_injection(
            stage,
            kind,
            WORK / f"injection-{kind}.json",
        )
        observed_code = None
        try:
            engine(state=state, injection=injection_path).run(clock_ms=CLOCK)
        except EngineError as error:
            observed_code = error.code
        current = Store(state).get_current_view()
        status = Store(state).status()
        output.append(
            {
                "stage": stage,
                "kind": kind,
                "expected_code": expected_code,
                "observed_code": observed_code,
                "view_unchanged": current["persisted"]["view_id"] == view_id,
                "watermark_unchanged": status["clock_watermark_ms"] == watermark,
                "passed": (
                    observed_code == expected_code
                    and current["persisted"]["view_id"] == view_id
                    and status["clock_watermark_ms"] == watermark
                ),
            }
        )
    return output


def crash_recovery_check() -> dict[str, Any]:
    state = WORK / "recover-merge.sqlite"
    crashed = subprocess.run(
        [
            sys.executable,
            "-m",
            "deploy_convergence",
            "verify",
            "--state",
            str(state),
            "--crash-after",
            "merge",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    before = Store(state).status()
    recovered = engine(state=state).run(clock_ms=CLOCK, recover=True)
    return {
        "crash_exit": crashed.returncode,
        "before_current_view": before["current_view"],
        "before_watermark_ms": before["clock_watermark_ms"],
        "before_steps": [step["step_name"] for step in before["steps"]],
        "steps_executed": recovered.steps_executed,
        "steps_replayed": recovered.steps_replayed,
        "after_commit_seq": recovered.view["persisted"]["commit_seq"],
        "passed": (
            crashed.returncode == 90
            and before["current_view"] is None
            and before["clock_watermark_ms"] == 0
            and recovered.steps_executed == ["evaluate", "stage", "switch"]
            and recovered.steps_replayed == ["parse", "migrate", "merge"]
        ),
    }


def concurrency_check() -> dict[str, Any]:
    state = WORK / "concurrent.sqlite"
    processes = []
    outputs = []
    for suffix in ("a", "b"):
        output = WORK / f"concurrent-{suffix}.json"
        outputs.append(output)
        processes.append(
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "deploy_convergence",
                    "verify",
                    "--state",
                    str(state),
                    "--json-out",
                    str(output),
                ],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        )
    results = []
    return_codes = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=60)
        return_codes.append(process.returncode)
        if process.returncode != 0:
            results.append({"error": stderr or stdout})
        else:
            results.append(
                json.loads(outputs[len(results)].read_text(encoding="utf-8"))
            )
    connection = sqlite3.connect(state)
    try:
        version_count = connection.execute(
            "SELECT COUNT(*) FROM view_versions"
        ).fetchone()[0]
        max_seq = connection.execute(
            "SELECT MAX(commit_seq) FROM view_versions"
        ).fetchone()[0]
        complete_operations = connection.execute(
            "SELECT COUNT(*) FROM operations WHERE status='complete'"
        ).fetchone()[0]
    finally:
        connection.close()
    return {
        "return_codes": return_codes,
        "idempotent_flags": [result["idempotent"] for result in results],
        "view_ids": [
            result["view"]["persisted"]["view_id"] for result in results
        ],
        "view_version_count": version_count,
        "max_commit_seq": max_seq,
        "complete_operations": complete_operations,
        "passed": (
            return_codes == [0, 0]
            and sorted(result["idempotent"] for result in results) == [False, True]
            and version_count == 1
            and max_seq == 1
            and complete_operations == 1
        ),
    }


def replacement_check() -> dict[str, Any]:
    state = WORK / "replacement.sqlite"
    base = engine(state=state).run(clock_ms=CLOCK)
    next_result = engine(
        state=state,
        v2_meta=SAMPLES / PATHS["v2_next_meta"],
        v2_shards=SAMPLES / PATHS["v2_next_shards"],
    ).run(clock_ms=CLOCK + 1000)
    rollback_code = None
    try:
        engine(state=state).run(clock_ms=CLOCK + 2000)
    except EngineError as error:
        rollback_code = error.code
    current_after_reject = Store(state).get_current_view()
    degraded = engine(
        state=state,
        rollback_policy="degrade",
    ).run(clock_ms=CLOCK + 3000)
    connection = sqlite3.connect(state)
    try:
        staging_count = sum(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "staging_versions",
                "staging_services",
                "staging_blobs",
            )
        )
        version_count = connection.execute(
            "SELECT COUNT(*) FROM view_versions"
        ).fetchone()[0]
    finally:
        connection.close()
    return {
        "initial_change": base.view["change"]["change_kind"],
        "next_change": next_result.view["change"]["change_kind"],
        "next_consistency_converged": next_result.view["consistency"]["consistent"],
        "next_commit_seq": next_result.view["persisted"]["commit_seq"],
        "rollback_code": rollback_code,
        "view_preserved_after_reject": (
            current_after_reject["persisted"]["view_id"]
            == next_result.view["persisted"]["view_id"]
        ),
        "degraded_change": degraded.view["change"]["change_kind"],
        "degraded_commit_seq": degraded.view["persisted"]["commit_seq"],
        "staging_rows_after_switch": staging_count,
        "view_version_count": version_count,
        "passed": (
            base.view["change"]["change_kind"] == "initial"
            and next_result.view["change"]["change_kind"] == "full_replace"
            and next_result.view["consistency"]["consistent"] is True
            and rollback_code == "E_VERSION_ROLLBACK"
            and current_after_reject["persisted"]["view_id"]
            == next_result.view["persisted"]["view_id"]
            and degraded.view["change"]["change_kind"] == "rollback_degraded"
            and staging_count == 0
        ),
    }


def main() -> int:
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)
    baseline, _ = baseline_check()
    injections = injection_checks()
    recovery = crash_recovery_check()
    concurrency = concurrency_check()
    replacement = replacement_check()
    checks = {
        "baseline": True,
        "clock_rollback": baseline["clock_rollback"]["code"] == "E_CLOCK_ROLLBACK"
        and baseline["clock_rollback"]["view_unchanged"],
        "injections": all(item["passed"] for item in injections),
        "recovery": recovery["passed"],
        "concurrency": concurrency["passed"],
        "replacement": replacement["passed"],
    }
    report = {
        "ok": all(checks.values()),
        "python": platform.python_version(),
        "clock_ms": CLOCK,
        "checks": checks,
        "baseline": baseline,
        "injections": injections,
        "recovery": recovery,
        "concurrency": concurrency,
        "replacement": replacement,
    }
    report_path = ARTIFACTS / "acceptance-report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
