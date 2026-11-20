#!/usr/bin/env python3
"""Run deterministic acceptance validation and write a stable JSON report."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from index_maintenance.canonical import canonical_json, sha256_json  # noqa: E402


SAMPLE = ROOT / "samples" / "fixed_sample.json"
VALIDATION_ROOT = ROOT / ".validation"
OUTPUT_ROOT = ROOT / "artifacts" / "validation"


def cli(
    workspace: Path,
    *arguments: str,
    check: bool = True,
    timeout: float = 90.0,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "index_maintenance.cli",
            "--workspace",
            str(workspace),
            *arguments,
        ],
        cwd=str(ROOT),
        env=environment,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"CLI failed ({result.returncode}): {result.stdout} {result.stderr}"
        )
    return result


def cli_json(
    workspace: Path,
    *arguments: str,
) -> dict[str, object]:
    result = cli(workspace, *arguments)
    return json.loads(result.stdout)


def prepare_workspace(name: str) -> Path:
    workspace = VALIDATION_ROOT / name
    workspace.mkdir(parents=True, exist_ok=True)
    cli(workspace, "load", "--sample", str(SAMPLE))
    return workspace


def run_pipeline(name: str) -> dict[str, object]:
    workspace = VALIDATION_ROOT / name
    workspace.mkdir(parents=True, exist_ok=True)
    load_result = cli(workspace, "load", "--sample", str(SAMPLE))
    apply_result = cli(workspace, "apply", "--from-seq", "1", "--to-seq", "15")
    status_result = cli(workspace, "status")
    verify_result = cli(workspace, "verify")
    output_dir = OUTPUT_ROOT / name
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "apply.json").write_text(apply_result.stdout, encoding="utf-8")
    (output_dir / "status.json").write_text(status_result.stdout, encoding="utf-8")
    (output_dir / "verify.json").write_text(verify_result.stdout, encoding="utf-8")
    status = json.loads(status_result.stdout)["status"]
    verify = json.loads(verify_result.stdout)
    return {
        "workspace": workspace,
        "load_bytes": load_result.stdout.encode("utf-8"),
        "apply_bytes": apply_result.stdout.encode("utf-8"),
        "status_bytes": status_result.stdout.encode("utf-8"),
        "verify_bytes": verify_result.stdout.encode("utf-8"),
        "status": status,
        "verify": verify,
    }


def crash_resume_check() -> dict[str, object]:
    workspace = prepare_workspace("crash-resume")
    crash = cli(
        workspace,
        "apply",
        "--from-seq",
        "1",
        "--to-seq",
        "15",
        "--crash-after-split",
        "3",
        check=False,
    )
    interrupted = cli_json(workspace, "status")["status"]
    verify_interrupted = cli(workspace, "verify", check=False)
    resumed = cli_json(workspace, "resume")
    final = cli_json(workspace, "status")["status"]
    verified = cli_json(workspace, "verify")
    output_dir = OUTPUT_ROOT / "crash-resume"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "interrupted_status.json").write_text(
        canonical_json({"status": interrupted}) + "\n",
        encoding="utf-8",
    )
    (output_dir / "resume.json").write_text(
        canonical_json(resumed) + "\n",
        encoding="utf-8",
    )
    (output_dir / "final_status.json").write_text(
        canonical_json({"status": final}) + "\n",
        encoding="utf-8",
    )
    verify_error = json.loads(verify_interrupted.stdout)
    return {
        "crash_exit": crash.returncode,
        "crash_stdout_empty": crash.stdout == "",
        "interrupted_watermark_seq": interrupted["ledger"]["watermark_seq"],
        "interrupted_attempt_phase": (
            interrupted["pending_attempts"][0]["phase"]
            if interrupted["pending_attempts"]
            else None
        ),
        "verify_before_resume_error": verify_error["error"]["code"],
        "discarded_attempts": len(resumed["discarded_attempts"]),
        "final_watermark_seq": final["ledger"]["watermark_seq"],
        "final_ledger_version": final["ledger"]["version"],
        "final_state_digest": final["state_digest"],
        "final_verify_digest": verified["verify_digest"],
        "pending_attempts": len(final["pending_attempts"]),
    }


def concurrent_check(clean_state_digest: str) -> dict[str, object]:
    workspace = prepare_workspace("concurrent-ranges")
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT)
    prefix = [
        sys.executable,
        "-m",
        "index_maintenance.cli",
        "--workspace",
        str(workspace),
        "apply",
    ]
    first = subprocess.Popen(
        prefix + ["--from-seq", "1", "--to-seq", "8"],
        cwd=str(ROOT),
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    second = subprocess.Popen(
        prefix + ["--from-seq", "9", "--to-seq", "15"],
        cwd=str(ROOT),
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    first_out, first_err = first.communicate(timeout=90)
    second_out, second_err = second.communicate(timeout=90)
    final = cli_json(workspace, "status")["status"]
    verified = cli_json(workspace, "verify")
    output_dir = OUTPUT_ROOT / "concurrent-ranges"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "range-1-8.json").write_text(first_out, encoding="utf-8")
    (output_dir / "range-9-15.json").write_text(second_out, encoding="utf-8")
    (output_dir / "final_status.json").write_text(
        canonical_json({"status": final}) + "\n",
        encoding="utf-8",
    )
    return {
        "range_1_8_exit": first.returncode,
        "range_9_15_exit": second.returncode,
        "stderr_empty": first_err == "" and second_err == "",
        "watermark_seq": final["ledger"]["watermark_seq"],
        "ledger_version": final["ledger"]["version"],
        "state_matches_clean_run": final["state_digest"] == clean_state_digest,
        "verify_digest": verified["verify_digest"],
    }


def secondary_termination_check(clean_state_digest: str) -> dict[str, object]:
    workspace = prepare_workspace("secondary-termination")
    crash = cli(
        workspace,
        "apply",
        "--from-seq",
        "1",
        "--to-seq",
        "15",
        "--crash-after-secondary",
        "1",
        check=False,
    )
    interrupted = cli_json(workspace, "status")["status"]
    resumed = cli_json(workspace, "resume")
    final = cli_json(workspace, "status")["status"]
    verified = cli_json(workspace, "verify")
    output_dir = OUTPUT_ROOT / "secondary-termination"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "interrupted_status.json").write_text(
        canonical_json({"status": interrupted}) + "\n",
        encoding="utf-8",
    )
    (output_dir / "resume.json").write_text(
        canonical_json(resumed) + "\n",
        encoding="utf-8",
    )
    (output_dir / "final_status.json").write_text(
        canonical_json({"status": final}) + "\n",
        encoding="utf-8",
    )
    return {
        "crash_exit": crash.returncode,
        "crash_stdout_empty": crash.stdout == "",
        "interrupted_watermark_seq": interrupted["ledger"]["watermark_seq"],
        "interrupted_attempt_phase": (
            interrupted["pending_attempts"][0]["phase"]
            if interrupted["pending_attempts"]
            else None
        ),
        "final_watermark_seq": final["ledger"]["watermark_seq"],
        "final_ledger_version": final["ledger"]["version"],
        "final_state_digest": final["state_digest"],
        "final_verify_digest": verified["verify_digest"],
        "pending_attempts": len(final["pending_attempts"]),
        "state_matches_clean_run": final["state_digest"] == clean_state_digest,
    }


def idempotent_check(clean_state_digest: str) -> dict[str, object]:
    workspace = prepare_workspace("idempotent-same-range")
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT)
    prefix = [
        sys.executable,
        "-m",
        "index_maintenance.cli",
        "--workspace",
        str(workspace),
        "apply",
    ]
    processes = [
        subprocess.Popen(
            prefix + ["--from-seq", "1", "--to-seq", "15"],
            cwd=str(ROOT),
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(2)
    ]
    results = [process.communicate(timeout=90) for process in processes]
    payloads = [json.loads(stdout) for stdout, _ in results]
    final = cli_json(workspace, "status")["status"]
    return {
        "exits": [process.returncode for process in processes],
        "applied_total": sum(len(payload["applied"]) for payload in payloads),
        "ledger_version": final["ledger"]["version"],
        "watermark_seq": final["ledger"]["watermark_seq"],
        "state_matches_clean_run": final["state_digest"] == clean_state_digest,
    }


def run_test_suite() -> dict[str, object]:
    stream = io.StringIO()
    suite = unittest.defaultTestLoader.discover(
        str(ROOT / "tests"),
        pattern="test_*.py",
        top_level_dir=str(ROOT),
    )
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    return {
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "was_successful": result.wasSuccessful(),
    }


def main() -> int:
    shutil.rmtree(VALIDATION_ROOT, ignore_errors=True)
    shutil.rmtree(OUTPUT_ROOT, ignore_errors=True)
    VALIDATION_ROOT.mkdir(parents=True, exist_ok=True)
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    run_a = run_pipeline("run-a")
    run_b = run_pipeline("run-b")
    status = run_a["status"]
    indexes = status["indexes"]
    deterministic = {
        "load_outputs_byte_identical": run_a["load_bytes"] == run_b["load_bytes"],
        "apply_outputs_byte_identical": run_a["apply_bytes"] == run_b["apply_bytes"],
        "status_outputs_byte_identical": run_a["status_bytes"] == run_b["status_bytes"],
        "verify_outputs_byte_identical": run_a["verify_bytes"] == run_b["verify_bytes"],
        "state_digest": status["state_digest"],
        "status_digest": status["status_digest"],
        "verify_digest": run_a["verify"]["verify_digest"],
    }
    adjudication = {
        "idx_status_region": {
            "decision": indexes["idx_status_region"]["decision"]["mode"],
            "first_trigger": indexes["idx_status_region"]["decision"][
                "first_trigger"
            ],
            "estimated_incremental_splits": indexes["idx_status_region"][
                "decision"
            ]["estimated_incremental_splits"],
            "actual_rebuild_splits": indexes["idx_status_region"]["decision"][
                "actual_rebuild_splits"
            ],
            "precision_sensitive": indexes["idx_status_region"]["decision"][
                "precision"
            ]["precision_sensitive"],
        },
        "idx_customer": {
            "decision": indexes["idx_customer"]["decision"]["mode"],
            "pending_items": indexes["idx_customer"]["decision"]["pending_items"],
            "touched_pages": indexes["idx_customer"]["decision"]["touched_pages"],
            "precision_sensitive": indexes["idx_customer"]["decision"]["precision"][
                "precision_sensitive"
            ],
        },
        "idx_email_lookup": {
            "last_changed_seq": indexes["idx_email_lookup"]["last_change_seq"],
            "index_reuse_records": sum(
                1
                for item in status["invalidations"]["items"]
                if item["index"] == "idx_email_lookup" and item["safe_reuse"]
            ),
        },
    }
    crash = crash_resume_check()
    crash["state_matches_clean_run"] = (
        crash["final_state_digest"] == status["state_digest"]
    )
    secondary_crash = secondary_termination_check(status["state_digest"])
    concurrent = concurrent_check(status["state_digest"])
    idempotent = idempotent_check(status["state_digest"])
    tests = run_test_suite()
    report = {
        "ok": all(
            [
                deterministic["apply_outputs_byte_identical"],
                deterministic["status_outputs_byte_identical"],
                deterministic["verify_outputs_byte_identical"],
                crash["crash_exit"] == 97,
                crash["verify_before_resume_error"] == "E_UNFINISHED_ATTEMPT",
                crash["state_matches_clean_run"],
                crash["pending_attempts"] == 0,
                secondary_crash["crash_exit"] == 97,
                secondary_crash["interrupted_attempt_phase"] == "secondary",
                secondary_crash["state_matches_clean_run"],
                secondary_crash["pending_attempts"] == 0,
                concurrent["state_matches_clean_run"],
                idempotent["state_matches_clean_run"],
                tests["was_successful"],
            ]
        ),
        "sample_id": status["sample_id"],
        "sample_sha256": status["ledger"]["sample_sha256"],
        "deterministic_runs": deterministic,
        "rebuild_adjudication": adjudication,
        "recovery": crash,
        "secondary_recovery": secondary_crash,
        "concurrency": concurrent,
        "idempotent_submission": idempotent,
        "test_suite": tests,
        "error_codes_exercised": [
            "E_BAD_INDEX_DECL",
            "E_BAD_OP",
            "E_BAD_SCHEMA_DECL",
            "E_BROKEN_PARENT_POINTER",
            "E_CLOCK_ROLLBACK",
            "E_DUP_PK",
            "E_INDEX_DIGEST_MISMATCH",
            "E_JSON_MALFORMED",
            "E_LEDGER_ROLLBACK",
            "E_MANIFEST_MISSING",
            "E_SPLIT_HEIGHT_OVERFLOW",
            "E_UNFINISHED_ATTEMPT",
            "E_UNIQUE_VIOLATION",
        ],
    }
    report["report_digest"] = sha256_json(report)
    report_path = ROOT / "artifacts" / "validation_report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(canonical_json({"ok": report["ok"], "report": "artifacts/validation_report.json"}))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
