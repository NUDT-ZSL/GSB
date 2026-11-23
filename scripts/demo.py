#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def run_cli(db_path: Path, *args: str) -> dict[str, Any]:
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "quota_ledger",
            "--db",
            str(db_path),
            *args,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=120,
    )
    if process.returncode not in (0, 1):
        raise RuntimeError(
            f"命令失败 ({process.returncode}): {process.stderr}\n{process.stdout}"
        )
    try:
        return json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"命令未返回 JSON: {process.stdout}\n{process.stderr}"
        ) from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="确定性账本一键演示")
    parser.add_argument(
        "--data-dir",
        default=str(ROOT / "data" / "demo"),
        help="演示数据目录（会通过 load --reset 重建）",
    )
    parser.add_argument(
        "--artifacts-dir",
        default=str(ROOT / "artifacts"),
        help="验证报告输出目录",
    )
    args = parser.parse_args()
    data_dir = Path(args.data_dir)
    artifacts_dir = Path(args.artifacts_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "ledger.db"

    samples = ROOT / "samples"
    load = run_cli(
        db_path,
        "load",
        "--tenants",
        str(samples / "tenants.json"),
        "--services",
        str(samples / "services.json"),
        "--applications",
        str(samples / "applications.json"),
        "--reset",
    )
    submit = run_cli(
        db_path,
        "submit",
        "--all",
        "--round-id",
        "demo-round-1",
        "--round-start-ms",
        "0",
        "--round-budget-ms",
        "100",
        "--workers",
        "1",
    )
    resume_200 = run_cli(db_path, "resume", "--now-ms", "200")
    resume_500 = run_cli(db_path, "resume", "--now-ms", "500")
    version_replace = run_cli(
        db_path,
        "load",
        "--tenants",
        str(samples / "tenants_v2.json"),
        "--services",
        str(samples / "services.json"),
        "--applications",
        str(samples / "applications.json"),
    )
    rollback = run_cli(
        db_path,
        "load",
        "--tenants",
        str(samples / "invalid" / "tenants_rollback.json"),
        "--services",
        str(samples / "services.json"),
        "--applications",
        str(samples / "applications.json"),
    )
    invalid_duplicate = run_cli(
        db_path,
        "load",
        "--tenants",
        str(samples / "invalid" / "tenants_duplicate.json"),
        "--services",
        str(samples / "services.json"),
        "--applications",
        str(samples / "applications.json"),
    )
    invalid_negative_borrow = run_cli(
        db_path,
        "load",
        "--tenants",
        str(samples / "invalid" / "tenants_negative_borrow.json"),
        "--services",
        str(samples / "services.json"),
        "--applications",
        str(samples / "applications.json"),
    )
    invalid_applications = run_cli(
        db_path,
        "load",
        "--tenants",
        str(samples / "tenants_v2.json"),
        "--services",
        str(samples / "services.json"),
        "--applications",
        str(samples / "invalid" / "applications_bad.json"),
    )
    status = run_cli(db_path, "status")
    verify = run_cli(db_path, "verify")

    by_sequence = {
        int(app["sequence"]): app for app in status["applications"]
    }
    report = {
        "ok": bool(verify.get("ok"))
        and rollback.get("error", {}).get("code") == "VERSION_ROLLBACK"
        and invalid_duplicate.get("error", {}).get("code") == "TENANT_DUPLICATE"
        and invalid_negative_borrow.get("error", {}).get("code")
        == "INVALID_FIELD_VALUE"
        and invalid_applications.get("error", {}).get("code")
        == "APPLICATION_EXPIRY_BEFORE_REQUEST",
        "db_path": str(db_path.resolve()),
        "load": {
            "operation": load.get("operation"),
            "bundle_digest": load.get("bundle_digest"),
        },
        "submit_summary": {
            "selected": len(submit.get("selected", [])),
            "abandoned": [
                {
                    "application_id": item["application"]["application_id"],
                    "reason_code": item["application"]["reason_code"],
                }
                for item in submit.get("abandoned", [])
            ],
            "states": {
                state: sum(
                    1
                    for item in submit.get("results", [])
                    if item["application"]["status"] == state
                )
                for state in ("ADMITTED", "WAITING")
            },
        },
        "release_and_wake": {
            "at_200": [
                {
                    "released": item["release_event"]["application_id"],
                    "wake_attempts": [
                        {
                            "application_id": attempt["application_id"],
                            "outcome": attempt["outcome"],
                            "reason_code": attempt["reason_code"],
                        }
                        for attempt in item["wake_attempts"]
                    ],
                }
                for item in resume_200.get("due_releases", [])
            ],
            "at_500": [
                {
                    "released": item["release_event"]["application_id"],
                    "wake_attempts": [
                        {
                            "application_id": attempt["application_id"],
                            "outcome": attempt["outcome"],
                            "reason_code": attempt["reason_code"],
                        }
                        for attempt in item["wake_attempts"]
                    ],
                }
                for item in resume_500.get("due_releases", [])
            ],
        },
        "versioning": {
            "replacement_operation": version_replace.get("operation"),
            "team_red_version": next(
                tenant["declaration_version"]
                for tenant in status["tenants"]
                if tenant["name"] == "team-red"
            ),
            "rollback_error_code": rollback.get("error", {}).get("code"),
        },
        "invalid_inputs": {
            "duplicate_tenant": invalid_duplicate.get("error", {}).get("code"),
            "negative_borrow": invalid_negative_borrow.get("error", {}).get(
                "code"
            ),
            "bad_applications": invalid_applications.get("error", {}).get(
                "code"
            ),
        },
        "precision": {
            "sensitive_tenants": [
                tenant["name"]
                for tenant in status["tenants"]
                if tenant["precision_sensitive"]
            ],
            "sensitive_applications": [
                application_id
                for application_id, app in (
                    (app["application_id"], app)
                    for app in status["applications"]
                )
                if app["precision_sensitive"]
            ],
            "latest_event": status["precision_events"][0]
            if status["precision_events"]
            else None,
        },
        "final": {
            "logical_clock_ms": status["logical_clock_ms"],
            "tenants": status["tenants"],
            "wait_queue": status["wait_queue"],
            "verify": verify,
        },
        "trace_states": {
            str(sequence): {
                "application_id": app["application_id"],
                "status": app["status"],
                "reason_code": app["reason_code"],
                "admitted_version": app["admitted_version"],
            }
            for sequence, app in sorted(by_sequence.items())
        },
    }
    target = artifacts_dir / "demo_report.json"
    target.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "ok": report["ok"],
                "report": str(target.resolve()),
                "logical_clock_ms": status["logical_clock_ms"],
                "submit_states": report["submit_summary"]["states"],
                "abandoned": report["submit_summary"]["abandoned"],
                "release_events_at_200": len(resume_200.get("due_releases", [])),
                "release_events_at_500": len(resume_500.get("due_releases", [])),
                "version": report["versioning"],
                "invalid_inputs": report["invalid_inputs"],
                "verify": verify,
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
    )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
