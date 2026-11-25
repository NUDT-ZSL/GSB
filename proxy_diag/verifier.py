"""Repeatable local end-to-end verification and diagnostic report."""

from __future__ import annotations

import copy
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from .errors import WorkbenchError
from .jsonio import read_json, write_json_atomic
from .paths import Paths
from .samplegen import generate as generate_samples
from .server import start_loopback_server
from .validation import validate_v1

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _parse_stdout(process: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    try:
        return json.loads(process.stdout)
    except (json.JSONDecodeError, TypeError):
        return {
            "ok": False,
            "raw_stdout": process.stdout[-2000:],
            "stderr": process.stderr[-2000:],
        }


def run_cli(
    sandbox: Path,
    args: list[str],
    *,
    extra_env: dict[str, str] | None = None,
    timeout: int = 120,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    command = [sys.executable, "-m", "proxy_diag.cli", *args, "--root", str(sandbox)]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("PROXY_DIAG_KILL_AT", None)
    if extra_env:
        env.update(extra_env)
    process = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    return process, _parse_stdout(process)


def start_cli_process(sandbox: Path, args: list[str], log_path: Path) -> tuple[subprocess.Popen[str], Any]:
    command = [sys.executable, "-m", "proxy_diag.cli", *args, "--root", str(sandbox)]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("PROXY_DIAG_KILL_AT", None)
    log_handle = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        command,
        cwd=PROJECT_ROOT,
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return process, log_handle


def db_connect(sandbox: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(sandbox / "state" / "workbench.sqlite3")
    connection.row_factory = sqlite3.Row
    return connection


def expect_code(function, code: str) -> str | None:
    try:
        function()
    except WorkbenchError as exc:
        if exc.code == code:
            return exc.code
        raise AssertionError(f"expected {code}, got {exc.code}: {exc.message}") from exc
    raise AssertionError(f"expected {code}, but validation passed")


def _result_json_by_session(sandbox: Path) -> dict[str, str]:
    with db_connect(sandbox) as connection:
        rows = connection.execute(
            "SELECT connection_id, result_json FROM forwarding_attempt WHERE status='committed'"
        ).fetchall()
    return {row["connection_id"]: row["result_json"] for row in rows}


def run_verification(paths: Paths, sandbox: str | Path | None = None) -> dict[str, Any]:
    sandbox_root = Path(sandbox) if sandbox else paths.state_dir / "verify-sandbox"
    if not sandbox_root.is_absolute():
        sandbox_root = paths.root / sandbox_root
    if sandbox_root.exists():
        shutil.rmtree(sandbox_root)
    main_sandbox = sandbox_root / "main"
    recovery_sandbox = sandbox_root / "recovery"
    negative_dir = sandbox_root / "negative"
    for directory in (main_sandbox, recovery_sandbox, negative_dir):
        directory.mkdir(parents=True, exist_ok=True)
    generate_samples(main_sandbox)
    generate_samples(recovery_sandbox)

    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, **detail: Any) -> dict[str, Any]:
        item = {"name": name, "ok": bool(ok), "detail": detail}
        checks.append(item)
        return item

    def expect_cli(name: str, args: list[str], sandbox: Path = main_sandbox) -> dict[str, Any] | None:
        try:
            process, data = run_cli(sandbox, args)
        except subprocess.TimeoutExpired:
            add(name, False, reason="timeout")
            return None
        ok = process.returncode == 0 and data.get("ok") is True
        detail: dict[str, Any] = {"returncode": process.returncode}
        if not ok:
            detail["error"] = data.get("error")
            detail["stdout"] = process.stdout[-1000:]
            detail["stderr"] = process.stderr[-1000:]
        add(name, ok, **detail)
        return data if ok else None

    def expect_business_error(name: str, args: list[str], code: str, sandbox: Path = main_sandbox, extra_env: dict[str, str] | None = None) -> bool:
        process, data = run_cli(sandbox, args, extra_env=extra_env)
        actual = (data.get("error") or {}).get("code")
        add(
            name,
            process.returncode == 2 and actual == code,
            expected_code=code,
            actual_code=actual,
            returncode=process.returncode,
        )
        return actual == code

    required_files = [
        main_sandbox / "samples" / "current_package.json",
        main_sandbox / "samples" / "v1" / "proxy_log_v1.json",
        main_sandbox / "samples" / "v2" / "v2_manifest.json",
        main_sandbox / "samples" / "v2" / "metadata.sqlite3",
        main_sandbox / "samples" / "patches" / "forward_table_patch_0001.json",
        main_sandbox / "samples" / "logical_clock.json",
    ]
    add("deterministic_samples_present", all(path.exists() for path in required_files), file_count=len(required_files))

    load_result = expect_cli("load_base_package", ["load"])
    if load_result:
        add("initial_snapshot_is_full_replace", load_result.get("decision") == "full_replace", decision=load_result.get("decision"))
    merge_result = expect_cli("merge_cross_versions", ["merge"])
    if merge_result:
        add("unified_session_count", merge_result.get("session_count") == 17, session_count=merge_result.get("session_count"))

    v1 = read_json(main_sandbox / "samples" / "v1" / "proxy_log_v1.json")
    validation_checks: dict[str, bool] = {}
    bad = copy.deepcopy(v1)
    bad["sessions"][1]["connection_id"] = bad["sessions"][0]["connection_id"]
    validation_checks["duplicate_connection"] = expect_code(lambda: validate_v1(bad), "E_V1_CONNECTION_ID_NOT_UNIQUE") is not None
    bad = copy.deepcopy(v1)
    bad["sessions"][0]["records"][0]["direction"] = "sideways"
    validation_checks["direction_enum"] = expect_code(lambda: validate_v1(bad), "E_V1_DIRECTION_INVALID") is not None
    bad = copy.deepcopy(v1)
    bad["sessions"][0]["records"][1]["seq"] = bad["sessions"][0]["records"][0]["seq"]
    validation_checks["strict_sequence"] = expect_code(lambda: validate_v1(bad), "E_V1_SEQ_NOT_STRICTLY_INCREASING") is not None
    bad = copy.deepcopy(v1)
    bad["sessions"][0]["records"][0]["record_second"] = -1
    validation_checks["non_negative_second"] = expect_code(lambda: validate_v1(bad), "E_V1_RELATIVE_SECOND_INVALID") is not None
    bad = copy.deepcopy(v1)
    bad["flat_forward_table"][0]["weight"] = 999
    validation_checks["flat_table_consistency"] = expect_code(lambda: validate_v1(bad), "E_V1_TABLE_ENTRY_MISMATCH") is not None
    add("v1_business_validation_codes", all(validation_checks.values()), cases=validation_checks)

    hash_bad_dir = negative_dir / "hash-v2"
    shutil.copytree(main_sandbox / "samples" / "v2", hash_bad_dir)
    manifest_path = hash_bad_dir / "v2_manifest.json"
    manifest = read_json(manifest_path)
    manifest["shards"][0]["sha256"] = "0" * 64
    write_json_atomic(manifest_path, manifest)
    hash_package = negative_dir / "hash-package.json"
    write_json_atomic(
        hash_package,
        {
            "format": "proxy-log-package",
            "package_id": "negative-hash",
            "manifest_version": 901,
            "clock_file": str(main_sandbox / "samples" / "logical_clock.json"),
            "v1_file": str(main_sandbox / "samples" / "v1" / "proxy_log_v1.json"),
            "v2_manifest": str(manifest_path),
            "v1_table": str(main_sandbox / "samples" / "tables" / "v1_forward_table.json"),
            "v2_table": str(main_sandbox / "samples" / "tables" / "v2_forward_table.json"),
            "test_plan": str(main_sandbox / "samples" / "test_service_plan.json"),
        },
    )
    expect_business_error("v2_shard_hash_error_code", ["load", "--package", str(hash_package)], "E_V2_SHARD_HASH_MISMATCH")

    generation_bad_dir = negative_dir / "generation-v2"
    shutil.copytree(main_sandbox / "samples" / "packages_data" / "replace_v2", generation_bad_dir)
    bad_db = generation_bad_dir / "metadata.sqlite3"
    with sqlite3.connect(bad_db) as connection:
        connection.execute(
            "UPDATE session_generation_history SET generation=1 WHERE connection_id='conn-alpha' AND history_seq=1"
        )
        connection.commit()
    generation_package = negative_dir / "generation-package.json"
    write_json_atomic(
        generation_package,
        {
            "format": "proxy-log-package",
            "package_id": "negative-generation",
            "manifest_version": 902,
            "clock_file": str(main_sandbox / "samples" / "logical_clock.json"),
            "v1_file": str(main_sandbox / "samples" / "v1" / "proxy_log_v1.json"),
            "v2_manifest": str(generation_bad_dir / "v2_manifest.json"),
            "v1_table": str(main_sandbox / "samples" / "tables" / "v1_forward_table.json"),
            "v2_table": str(main_sandbox / "samples" / "tables" / "v2_forward_table.json"),
            "test_plan": str(main_sandbox / "samples" / "test_service_plan.json"),
        },
    )
    expect_business_error("v2_generation_rollback_code", ["load", "--package", str(generation_package)], "E_V2_GENERATION_ROLLBACK")

    plan = read_json(main_sandbox / "samples" / "test_service_plan.json")
    server, base_url, registry = start_loopback_server(plan)
    try:
        log_a = sandbox_root / "dup-a.log"
        log_b = sandbox_root / "dup-b.log"
        process_a, log_handle_a = start_cli_process(
            main_sandbox,
            ["replay", "--session", "conn-parallel-00", "--base-url", base_url, "--worker-id", "dup-a"],
            log_a,
        )
        process_b, log_handle_b = start_cli_process(
            main_sandbox,
            ["replay", "--session", "conn-parallel-00", "--base-url", base_url, "--worker-id", "dup-b"],
            log_b,
        )
        duplicate_returncodes = [process_a.wait(timeout=60), process_b.wait(timeout=60)]
        log_handle_a.close()
        log_handle_b.close()
        output_a = json.loads(log_a.read_text(encoding="utf-8"))
        output_b = json.loads(log_b.read_text(encoding="utf-8"))
        actions = sorted([output_a.get("action"), output_b.get("action")])
        add(
            "same_session_same_version_single_replay",
            duplicate_returncodes == [0, 0] and actions == ["already_committed", "replayed"],
            returncodes=duplicate_returncodes,
            actions=actions,
        )

        workers: list[tuple[subprocess.Popen[str], Any]] = []
        worker_logs: list[Path] = []
        for index in range(4):
            log_path = sandbox_root / f"parallel-worker-{index}.log"
            worker_logs.append(log_path)
            workers.append(
                start_cli_process(
                    main_sandbox,
                    ["replay", "--all", "--base-url", base_url, "--worker-id", f"parallel-{index}"],
                    log_path,
                )
            )
        worker_codes: list[int] = []
        for process, handle in workers:
            worker_codes.append(process.wait(timeout=90))
            handle.close()
        add("four_parallel_diagnostic_processes_exit_zero", all(code == 0 for code in worker_codes), returncodes=worker_codes)

        status_result = expect_cli("status_after_parallel_replay", ["status", "--no-write"])
        if status_result:
            counts = status_result.get("conclusion_counts", {})
            add(
                "initial_conclusion_counts",
                counts.get("success") == 15 and counts.get("failed") == 1 and counts.get("skipped") == 1,
                counts=counts,
            )

        unified = read_json(main_sandbox / "reports" / "unified_view.json")
        expected_counts: dict[str, int] = {}
        for session in unified["sessions"]:
            if session.get("tombstoned_in_selected_table"):
                continue
            for hop in sorted({int(record["hop"]) for record in session["records"]}):
                expected_counts[f"{session['connection_id']}:{hop}"] = 1
        service_state = registry.state()
        add(
            "each_session_hop_called_exactly_once",
            service_state["counts"] == expected_counts and service_state["call_count"] == sum(expected_counts.values()),
            actual_call_count=service_state["call_count"],
            expected_call_count=sum(expected_counts.values()),
            over_replayed=sorted(key for key, value in service_state["counts"].items() if value > 1),
        )

        _, status_stdout_1 = run_cli(main_sandbox, ["status", "--no-write"])
        _, status_stdout_2 = run_cli(main_sandbox, ["status", "--no-write"])
        add("status_json_deterministic", status_stdout_1 == status_stdout_2)

        before_patch = _result_json_by_session(main_sandbox)
        patch_processes: list[tuple[subprocess.Popen[str], Any, Path]] = []
        for index in range(3):
            patch_log = sandbox_root / f"patch-worker-{index}.log"
            process, handle = start_cli_process(
                main_sandbox,
                ["patch", str(main_sandbox / "samples" / "patches" / "forward_table_patch_0001.json")],
                patch_log,
            )
            patch_processes.append((process, handle, patch_log))
        patch_codes = [process.wait(timeout=60) for process, _, _ in patch_processes]
        patch_payloads: list[dict[str, Any]] = []
        for _, handle, log_path in patch_processes:
            handle.close()
            patch_payloads.append(json.loads(log_path.read_text(encoding="utf-8")))
        applied_count = sum(1 for item in patch_payloads if item.get("applied") is True)
        concurrent_idempotent_count = sum(1 for item in patch_payloads if item.get("idempotent") is True)
        add(
            "concurrent_patch_single_apply",
            patch_codes == [0, 0, 0] and applied_count == 1 and concurrent_idempotent_count == 2,
            returncodes=patch_codes,
            applied_count=applied_count,
            idempotent_count=concurrent_idempotent_count,
        )
        patch_result = next((item for item in patch_payloads if item.get("applied") is True), None)
        affected = ["conn-alpha", "conn-bravo", "conn-golf"]
        if patch_result:
            add("closure_session_set", patch_result.get("affected_sessions") == affected, affected=patch_result.get("affected_sessions"))
        after_patch_rows = _result_json_by_session(main_sandbox)
        reusable = [session_id for session_id in before_patch if session_id not in affected]
        unchanged = all(before_patch[session_id] == after_patch_rows.get(session_id) for session_id in reusable)
        add("unaffected_evidence_unchanged_by_patch", unchanged, reusable_count=len(reusable))

        patch_idem = expect_cli(
            "patch_is_idempotent",
            ["patch", str(main_sandbox / "samples" / "patches" / "forward_table_patch_0001.json")],
        )
        if patch_idem:
            add("repeated_patch_marked_idempotent", patch_idem.get("idempotent") is True and patch_idem.get("applied") is False)

        replay_patch = expect_cli(
            "replay_only_affected_sessions",
            ["replay", "--all", "--base-url", base_url, "--worker-id", "patch-replay"],
        )
        if replay_patch:
            processed = sorted(item["connection_id"] for item in replay_patch.get("processed", []))
            add("only_three_affected_sessions_replayed", processed == affected, processed=processed)
        after_replay_rows = _result_json_by_session(main_sandbox)
        add(
            "unaffected_evidence_still_bitwise_identical",
            all(before_patch[session_id] == after_replay_rows.get(session_id) for session_id in reusable),
            reusable_count=len(reusable),
        )
        add(
            "loopback_not_called_for_reused_sessions",
            registry.state()["call_count"] == sum(expected_counts.values()) + 4,
            call_count=registry.state()["call_count"],
        )
    finally:
        server.shutdown()

    crash_merge, merge_data = run_cli(
        recovery_sandbox,
        ["merge"],
        extra_env={"PROXY_DIAG_KILL_AT": "merge_before_upsert"},
    )
    add("sigkill_during_merge_returns_signal", crash_merge.returncode == -9, returncode=crash_merge.returncode)
    with db_connect(recovery_sandbox) as connection:
        merge_partial_count = connection.execute("SELECT COUNT(*) FROM session_state").fetchone()[0]
    add("merge_kill_leaves_no_partial_sessions", merge_partial_count == 0, count=merge_partial_count)
    expect_cli("merge_after_crash_succeeds", ["merge"], recovery_sandbox)

    recovery_plan = read_json(recovery_sandbox / "samples" / "test_service_plan.json")
    recovery_server, recovery_url, recovery_registry = start_loopback_server(recovery_plan)
    try:
        crash_replay, _ = run_cli(
            recovery_sandbox,
            ["replay", "--session", "conn-hotel", "--base-url", recovery_url],
            extra_env={"PROXY_DIAG_KILL_AT": "before_commit"},
        )
        add("sigkill_during_replay_returns_signal", crash_replay.returncode == -9, returncode=crash_replay.returncode)
        with db_connect(recovery_sandbox) as connection:
            hotel_claim = connection.execute(
                "SELECT status FROM forwarding_attempt WHERE connection_id='conn-hotel' ORDER BY attempt_id DESC LIMIT 1"
            ).fetchone()
        add("killed_attempt_remains_uncommitted", hotel_claim is not None and hotel_claim["status"] == "claimed", status=hotel_claim["status"] if hotel_claim else None)

        resume_result = expect_cli("resume_recovers_uncommitted", ["resume", "--base-url", recovery_url], recovery_sandbox)
        if resume_result:
            add("resume_reports_reset_claim", "conn-hotel" in ((resume_result.get("recovery") or {}).get("reset") or []))
        status_result = expect_cli("recovery_status_consistent", ["status", "--no-write"], recovery_sandbox)
        if status_result:
            counts = status_result.get("conclusion_counts", {})
            add("recovered_conclusion_counts", counts == {"success": 15, "failed": 1, "skipped": 1, "pending": 0, "claimed": 0}, counts=counts)
        with db_connect(recovery_sandbox) as connection:
            hotel_committed = connection.execute(
                "SELECT COUNT(*) FROM forwarding_attempt WHERE connection_id='conn-hotel' AND status='committed'"
            ).fetchone()[0]
            duplicate_committed = connection.execute(
                "SELECT connection_id, snapshot_version, generation, table_version, COUNT(*) AS n FROM forwarding_attempt WHERE status='committed' GROUP BY 1,2,3,4 HAVING n > 1"
            ).fetchall()
        add("committed_conclusions_not_duplicated", hotel_committed == 1 and not duplicate_committed, hotel_committed=hotel_committed)

        snapshot_crash, _ = run_cli(
            recovery_sandbox,
            ["load", "--package", "samples/packages/replace_package.json"],
            extra_env={"PROXY_DIAG_KILL_AT": "snapshot_before_switch"},
        )
        add("sigkill_during_snapshot_switch", snapshot_crash.returncode == -9, returncode=snapshot_crash.returncode)
        with db_connect(recovery_sandbox) as connection:
            pointer_version = connection.execute("SELECT snapshot_version FROM snapshot_pointer WHERE name='current'").fetchone()[0]
        add("snapshot_pointer_unchanged_after_kill", pointer_version == 1, pointer_version=pointer_version)

        replace_result = expect_cli(
            "atomic_full_snapshot_replace",
            ["load", "--package", "samples/packages/replace_package.json"],
            recovery_sandbox,
        )
        if replace_result:
            add("replace_decision_full", replace_result.get("decision") == "full_replace", decision=replace_result.get("decision"))
        expect_business_error(
            "version_rollback_rejected",
            ["load", "--package", "samples/packages/incremental_package.json"],
            "E_VERSION_ROLLBACK",
            recovery_sandbox,
        )
    finally:
        recovery_server.shutdown()

    passed = sum(1 for check in checks if check["ok"])
    report = {
        "ok": passed == len(checks),
        "command": "verify",
        "summary": {"check_count": len(checks), "passed": passed, "failed": len(checks) - passed},
        "checks": checks,
        "artifacts": {
            "sandbox": "state/verify-sandbox",
            "stable_status": "reports/status.json",
            "unified_view": "reports/unified_view.json",
        },
    }
    write_json_atomic(paths.diagnostic_report_path, report)
    return report
