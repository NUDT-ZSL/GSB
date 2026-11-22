#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"
ARTIFACTS = ROOT / "artifacts"
PYTHON = sys.executable


def cli(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        [PYTHON, "-m", "warbiter", *args],
        cwd=ROOT,
        env=merged,
        text=True,
        capture_output=True,
        check=False,
    )


def cli_json(*args: str, env: dict[str, str] | None = None) -> dict:
    result = cli(*args, env=env)
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {args}\n{result.stdout}\n{result.stderr}"
        )
    return json.loads(result.stdout)


def sqlite_rows(db: Path, query: str) -> list[dict]:
    connection = sqlite3.connect(db)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(query).fetchall()]
    finally:
        connection.close()


def init_and_apply(directory: Path, name: str) -> Path:
    db = directory / name
    cli_json(
        "init",
        "--db",
        str(db),
        "--schema",
        str(SAMPLES / "schema.json"),
        "--config",
        str(SAMPLES / "arbiter.json"),
    )
    cli_json(
        "apply",
        "--db",
        str(db),
        "--script",
        str(SAMPLES / "transactions.json"),
    )
    return db


def main() -> int:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    checks: dict[str, bool] = {}
    details: dict[str, object] = {}
    with tempfile.TemporaryDirectory(prefix="warbiter-validation-") as temp_name:
        temp = Path(temp_name)

        rename_source = temp / "rename-source"
        rename_target = temp / "rename-target"
        rename_source.write_text("atomic", encoding="utf-8", newline="\n")
        os.replace(rename_source, rename_target)
        checks["filesystem_atomic_rename"] = (
            rename_target.read_text(encoding="utf-8") == "atomic"
        )

        baseline = init_and_apply(temp, "baseline.db")
        conflict_pending = cli_json("conflict", "--db", str(baseline))
        resolve_result = cli_json("resolve", "--db", str(baseline))
        status = cli_json("status", "--db", str(baseline))
        verification = cli_json("verify", "--db", str(baseline))
        checks["workflow_verify"] = bool(verification["ok"])
        details["workflow"] = {
            "conflicts_pending_before_resolve": conflict_pending["detection"][
                "conflicts_pending"
            ],
            "conflict_kinds_before_resolve": conflict_pending["counts"][
                "conflicts_by_kind"
            ],
            "resolved_conflicts": resolve_result["resolved_conflicts"],
            "transaction_statuses": status["counts"]["transactions"],
            "conflict_decisions": status["counts"]["conflicts_by_decision"],
            "ledger_digest": verification["ledger_digest"],
            "final_rows_digest": verification["final_rows_digest"],
        }

        repeated = cli_json(
            "apply",
            "--db",
            str(baseline),
            "--script",
            str(SAMPLES / "transactions.json"),
        )
        status_after_repeat = cli_json("status", "--db", str(baseline))
        checks["idempotent_reapply"] = (
            repeated["inserted"] == []
            and len(repeated["skipped_idempotent"]) == 18
            and status_after_repeat == status
        )
        details["idempotent_reapply"] = {
            "inserted": repeated["inserted"],
            "skipped": len(repeated["skipped_idempotent"]),
            "ledger_unchanged": status_after_repeat["engine"]["ledger_digest"]
            == status["engine"]["ledger_digest"],
        }

        second = init_and_apply(temp, "second.db")
        cli_json("resolve", "--db", str(second))
        second_status = cli_json("status", "--db", str(second))
        second_verify = cli_json("verify", "--db", str(second))
        checks["deterministic_two_runs"] = (
            second_status == status
            and second_verify["ledger_digest"] == verification["ledger_digest"]
            and second_verify["final_rows_digest"]
            == verification["final_rows_digest"]
        )
        details["determinism"] = {
            "status_equal": second_status == status,
            "ledger_equal": second_verify["ledger_digest"]
            == verification["ledger_digest"],
            "final_rows_equal": second_verify["final_rows_digest"]
            == verification["final_rows_digest"],
        }

        policy_db = init_and_apply(temp, "policy.db")
        policy_change = cli_json(
            "policy",
            "--db",
            str(policy_db),
            "--schema",
            str(SAMPLES / "schema_policy_v2.json"),
            "--config",
            str(SAMPLES / "arbiter.json"),
        )
        cli_json("resolve", "--db", str(policy_db))
        policy_verify = cli_json("verify", "--db", str(policy_db))
        policy_rows = sqlite_rows(
            policy_db,
            """
            SELECT tx_id, status, policy_version, adjudication_policy_version
            FROM tx WHERE tx_id=1102
            """,
        )
        policy_conflict_versions = sqlite_rows(
            policy_db,
            "SELECT DISTINCT policy_version FROM conflicts ORDER BY policy_version",
        )
        checks["policy_snapshot_binding"] = (
            policy_change["current_policy_version"] == 2
            and policy_rows[0]["status"] == "rejected_conflict"
            and [row["policy_version"] for row in policy_conflict_versions] == [1]
            and bool(policy_verify["ok"])
        )
        details["policy_snapshot"] = {
            "previous_version": policy_change["previous_policy_version"],
            "current_version": policy_change["current_policy_version"],
            "tx_1102": policy_rows[0],
            "conflict_versions": [
                row["policy_version"] for row in policy_conflict_versions
            ],
        }

        crash_db = init_and_apply(temp, "crash_resolve.db")
        killed = cli(
            "resolve",
            "--db",
            str(crash_db),
            env={"WARBITER_FAULT_RESOLVE": "1"},
        )
        after_kill = {
            "exit_code": killed.returncode,
            "tx": sqlite_rows(
                crash_db,
                "SELECT status, COUNT(*) AS n FROM tx GROUP BY status ORDER BY status",
            ),
            "conflicts": sqlite_rows(
                crash_db,
                "SELECT status, COUNT(*) AS n FROM conflicts GROUP BY status ORDER BY status",
            ),
        }
        resumed = cli_json("resume", "--db", str(crash_db))
        crash_status = cli_json("status", "--db", str(crash_db))
        crash_verify = cli_json("verify", "--db", str(crash_db))
        checks["crash_resume_consistency"] = (
            killed.returncode == 137
            and after_kill["tx"][0]["status"] == "committed"
            and crash_status == status
            and bool(crash_verify["ok"])
        )
        details["crash_resume"] = {
            "kill_exit_code": killed.returncode,
            "state_after_kill": after_kill,
            "resolved_after_resume": resumed["resolution"]["resolved_conflicts"],
            "matches_uninterrupted": crash_status == status,
        }

        apply_crash_db = temp / "crash_apply.db"
        cli_json(
            "init",
            "--db",
            str(apply_crash_db),
            "--schema",
            str(SAMPLES / "schema.json"),
            "--config",
            str(SAMPLES / "arbiter.json"),
        )
        apply_killed = cli(
            "apply",
            "--db",
            str(apply_crash_db),
            "--script",
            str(SAMPLES / "transactions.json"),
            env={"WARBITER_FAULT_APPLY": "3"},
        )
        apply_after_kill = {
            "exit_code": apply_killed.returncode,
            "tx_count": sqlite_rows(
                apply_crash_db, "SELECT COUNT(*) AS n FROM tx"
            )[0]["n"],
            "conflict_count": sqlite_rows(
                apply_crash_db, "SELECT COUNT(*) AS n FROM conflicts"
            )[0]["n"],
        }
        cli_json(
            "apply",
            "--db",
            str(apply_crash_db),
            "--script",
            str(SAMPLES / "transactions.json"),
        )
        cli_json("resume", "--db", str(apply_crash_db))
        apply_crash_status = cli_json("status", "--db", str(apply_crash_db))
        checks["apply_crash_all_or_none"] = (
            apply_killed.returncode == 137
            and apply_after_kill["tx_count"] == 0
            and apply_after_kill["conflict_count"] == 0
            and apply_crash_status == status
        )
        details["apply_crash"] = {
            **apply_after_kill,
            "matches_uninterrupted_after_reapply": apply_crash_status == status,
        }

        concurrent_db = temp / "concurrent.db"
        cli_json(
            "init",
            "--db",
            str(concurrent_db),
            "--schema",
            str(SAMPLES / "schema.json"),
            "--config",
            str(SAMPLES / "arbiter.json"),
        )
        processes = [
            subprocess.Popen(
                [
                    PYTHON,
                    "-m",
                    "warbiter",
                    "apply",
                    "--db",
                    str(concurrent_db),
                    "--script",
                    str(script),
                ],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for script in sorted(
                (SAMPLES / "transactions_concurrent").glob("*.json")
            )
        ]
        concurrent_outputs = [process.communicate() for process in processes]
        cli_json("resolve", "--db", str(concurrent_db))
        concurrent_status = cli_json("status", "--db", str(concurrent_db))
        concurrent_verify = cli_json("verify", "--db", str(concurrent_db))
        metric_rows = [
            row
            for table in concurrent_status["tables"]
            if table["tenant"] == "globex" and table["table"] == "metrics"
            for row in table["rows"]
        ]
        checks["parallel_apply"] = (
            all(process.returncode == 0 for process in processes)
            and metric_rows[0]["values"] == {"id": 1, "value": 4}
            and metric_rows[0]["last_tx_id"] == 2004
            and bool(concurrent_verify["ok"])
        )
        details["parallel_apply"] = {
            "exit_codes": [process.returncode for process in processes],
            "conflicts": concurrent_status["counts"]["conflicts_by_kind"],
            "metrics_row": metric_rows[0],
            "verify_ok": concurrent_verify["ok"],
            "stderr": [
                stderr.strip()
                for _, stderr in concurrent_outputs
                if stderr.strip()
            ],
        }

        tests = subprocess.run(
            [PYTHON, "-m", "unittest", "discover", "-s", "tests", "-v"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        checks["unittest"] = tests.returncode == 0
        details["unittest"] = {
            "returncode": tests.returncode,
            "summary": [
                line
                for line in tests.stderr.splitlines()
                if line.startswith("Ran ") or line == "OK"
            ],
        }

        assertion_rows = sqlite_rows(
            baseline,
            """
            SELECT tx_id, result, action, record_version, record_values_json,
                   recorded_observation_json, evidence_json
            FROM assertion_evidence
            WHERE tx_id IN (1003, 1204)
            ORDER BY tx_id
            """,
        )
        assertion_evidence = []
        for row in assertion_rows:
            evidence = json.loads(row.pop("evidence_json"))
            row["record_values"] = json.loads(row.pop("record_values_json"))
            row["recorded_observation"] = json.loads(
                row.pop("recorded_observation_json")
            )
            row["assertion_text"] = evidence["assertion_text"]
            assertion_evidence.append(row)

        manifest_hash = hashlib.sha256(
            (SAMPLES / "manifest.json").read_bytes()
        ).hexdigest()
        report = {
            "schema": "warbiter-validation-report-v1",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "environment": {
                "python": sys.version.split()[0],
                "sqlite": sqlite3.sqlite_version,
                "samples_manifest_sha256": manifest_hash,
            },
            "ok": all(checks.values()),
            "checks": checks,
            "details": details,
            "assertion_evidence": assertion_evidence,
        }

        write_atomic(
            ARTIFACTS / "demo_status.json",
            json.dumps(status, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )
        write_atomic(
            ARTIFACTS / "demo_verify.json",
            json.dumps(verification, ensure_ascii=False, sort_keys=True, indent=2)
            + "\n",
        )
        write_atomic(
            ARTIFACTS / "demo_conflicts.json",
            json.dumps(
                conflict_pending, ensure_ascii=False, sort_keys=True, indent=2
            )
            + "\n",
        )
        write_atomic(
            ARTIFACTS / "validation_report.json",
            json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )
        write_atomic(ARTIFACTS / "validation_report.md", render_markdown(report))
        sys.stdout.write(
            json.dumps(
                {
                    "ok": report["ok"],
                    "checks": report["checks"],
                    "report": "artifacts/validation_report.json",
                },
                sort_keys=True,
            )
            + "\n"
        )
    return 0 if report["ok"] else 1


def write_atomic(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    with temporary.open("rb") as handle:
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def render_markdown(report: dict) -> str:
    workflow = report["details"]["workflow"]
    lines = [
        "# Warbiter 验证报告",
        "",
        f"- 生成时间: `{report['generated_at']}`",
        f"- Python: `{report['environment']['python']}`",
        f"- SQLite: `{report['environment']['sqlite']}`",
        f"- 总结果: **{'PASS' if report['ok'] else 'FAIL'}**",
        "",
        "## 检查项",
        "",
    ]
    for name, value in sorted(report["checks"].items()):
        lines.append(f"- `{name}`: {'PASS' if value else 'FAIL'}")
    lines.extend(
        [
            "",
            "## 主样本结果",
            "",
            f"- 裁决前待处理冲突: `{workflow['conflicts_pending_before_resolve']}`",
            f"- 冲突类型: `{json.dumps(workflow['conflict_kinds_before_resolve'], ensure_ascii=False, sort_keys=True)}`",
            f"- 已裁决冲突: `{workflow['resolved_conflicts']}`",
            f"- 事务状态: `{json.dumps(workflow['transaction_statuses'], ensure_ascii=False, sort_keys=True)}`",
            f"- 裁决分布: `{json.dumps(workflow['conflict_decisions'], ensure_ascii=False, sort_keys=True)}`",
            f"- 账本摘要: `{workflow['ledger_digest']}`",
            f"- 最终记录集摘要: `{workflow['final_rows_digest']}`",
            "",
            "## 断言重估",
            "",
        ]
    )
    for item in report["assertion_evidence"]:
        lines.append(
            f"- tx `{item['tx_id']}`: observed={json.dumps(item['recorded_observation'], ensure_ascii=False, sort_keys=True)}, "
            f"actual={json.dumps(item['record_values'], ensure_ascii=False, sort_keys=True)}, "
            f"result={bool(item['result'])}, action=`{item['action']}`"
        )
    lines.extend(
        [
            "",
            "## 故障与并发",
            "",
            f"- 中断恢复: `{json.dumps(report['details']['crash_resume'], ensure_ascii=False, sort_keys=True)}`",
            f"- 提交中断: `{json.dumps(report['details']['apply_crash'], ensure_ascii=False, sort_keys=True)}`",
            f"- 并行提交: `{json.dumps(report['details']['parallel_apply'], ensure_ascii=False, sort_keys=True)}`",
            f"- 单元测试: `{json.dumps(report['details']['unittest'], ensure_ascii=False, sort_keys=True)}`",
            "",
        ]
    )
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
