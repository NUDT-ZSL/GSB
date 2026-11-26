#!/usr/bin/env python3
"""Run the complete deterministic acceptance scenario and write evidence."""

from __future__ import annotations

import argparse
import json
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from seqledger.sample import generate_fixtures  # noqa: E402
from seqledger.store import LedgerStore  # noqa: E402
from seqledger.util import write_json_atomic  # noqa: E402


def run(args: list[str]) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, "-m", "seqledger", *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=60,
    )
    payload = json.loads(completed.stdout or "{}")
    if completed.returncode != 0:
        raise AssertionError(
            f"Command failed ({completed.returncode}): {args}\n"
            f"stdout={completed.stdout}\nstderr={completed.stderr}"
        )
    return payload


def run_killed(args: list[str]) -> int:
    completed = subprocess.run(
        [sys.executable, "-m", "seqledger", *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=60,
    )
    return completed.returncode


def load_args(fixtures: Path, db: Path) -> list[str]:
    return [
        "--db",
        str(db),
        "load",
        "--topology",
        str(fixtures / "topology.json"),
        "--events",
        str(fixtures / "events.jsonl"),
        "--subscriptions",
        str(fixtures / "subscriptions.json"),
    ]


def merge_args(fixtures: Path, db: Path) -> list[str]:
    return [
        "--db",
        str(db),
        "merge",
        "--v1",
        str(fixtures / "semantics_v1.json"),
        "--v2-manifest",
        str(fixtures / "semantics_v2" / "manifest.json"),
        "--v2-meta",
        str(fixtures / "semantics_v2" / "meta.sqlite3"),
        "--logical-clock-ms",
        "3000",
    ]


def verify_args(fixtures: Path, db: Path) -> list[str]:
    return [
        "--db",
        str(db),
        "verify",
        "--topology",
        str(fixtures / "topology.json"),
        "--events",
        str(fixtures / "events.jsonl"),
        "--subscriptions",
        str(fixtures / "subscriptions.json"),
        "--v1",
        str(fixtures / "semantics_v1.json"),
        "--v2-manifest",
        str(fixtures / "semantics_v2" / "manifest.json"),
        "--v2-meta",
        str(fixtures / "semantics_v2" / "meta.sqlite3"),
        "--merge-clock-ms",
        "3000",
        "--logical-clock-ms",
        "7000",
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--out-dir",
        default=str(ROOT / "artifacts" / "acceptance"),
    )
    args = parser.parse_args()
    out_dir = Path(args.out_dir)
    fixtures = ROOT / "fixtures"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    committed_manifest = json.loads(
        (fixtures / "fixture_manifest.json").read_text(encoding="utf-8")
    )

    with tempfile.TemporaryDirectory(prefix="seqledger-acceptance-") as temporary:
        temp = Path(temporary)
        regenerated = temp / "regenerated-fixtures"
        generated_manifest = generate_fixtures(regenerated)
        seed_reproduced = (
            generated_manifest["files"] == committed_manifest["files"]
        )
        if not seed_reproduced:
            raise AssertionError("Fixed-seed fixture regeneration was not bitwise stable")

        db = temp / "main.sqlite3"
        load = run(load_args(fixtures, db))
        merge = run(merge_args(fixtures, db))
        write_json_atomic(out_dir / "merge-evidence.json", merge)

        kill_exit = run_killed(
            [
                "--db",
                str(db),
                "replay",
                "--logical-clock-ms",
                "7000",
                "--fault-kill-after-commits",
                "5",
            ]
        )
        if kill_exit != -signal.SIGKILL:
            raise AssertionError(f"Expected SIGKILL, got return code {kill_exit}")
        resumed = run(
            [
                "--db",
                str(db),
                "resume",
                "--logical-clock-ms",
                "7000",
            ]
        )
        write_json_atomic(out_dir / "resume-result.json", resumed)
        status_before = run(["--db", str(db), "status"])
        write_json_atomic(out_dir / "status-before-patch.json", status_before)
        verify_before = run(verify_args(fixtures, db))
        write_json_atomic(out_dir / "verify-before-patch.json", verify_before)

        patch = run(
            [
                "--db",
                str(db),
                "patch",
                "--file",
                str(fixtures / "patch-001.json"),
            ]
        )
        write_json_atomic(out_dir / "patch-invalidation.json", patch)
        status_after = run(["--db", str(db), "status"])
        write_json_atomic(out_dir / "status-after-patch.json", status_after)
        verify_after = run(
            verify_args(fixtures, db)
            + ["--patch", str(fixtures / "patch-001.json")]
        )
        write_json_atomic(out_dir / "verify-after-patch.json", verify_after)

        merge_kill_db = temp / "merge-kill.sqlite3"
        run(load_args(fixtures, merge_kill_db))
        before_merge_kill = run(["--db", str(merge_kill_db), "status"])
        merge_kill_exit = run_killed(
            merge_args(fixtures, merge_kill_db)
            + ["--fault-kill-before-commit"]
        )
        after_merge_kill = run(["--db", str(merge_kill_db), "status"])
        merge_recovered = run(merge_args(fixtures, merge_kill_db))

        concurrency_db = temp / "concurrent.sqlite3"
        run(load_args(fixtures, concurrency_db))
        run(merge_args(fixtures, concurrency_db))
        worker_a = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "seqledger",
                "--db",
                str(concurrency_db),
                "replay",
                "--logical-clock-ms",
                "7000",
                "--partition",
                "orders/0",
                "--partition",
                "orders/1",
                "--partition",
                "orders/3",
            ],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        worker_b = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "seqledger",
                "--db",
                str(concurrency_db),
                "replay",
                "--logical-clock-ms",
                "7000",
                "--partition",
                "orders/2",
                "--partition",
                "payments/0",
                "--partition",
                "payments/1",
                "--partition",
                "payments/2",
            ],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout_a, stderr_a = worker_a.communicate(timeout=60)
        stdout_b, stderr_b = worker_b.communicate(timeout=60)
        if worker_a.returncode != 0 or worker_b.returncode != 0:
            raise AssertionError(
                "Concurrent replay failed\n"
                f"A={worker_a.returncode} {stdout_a} {stderr_a}\n"
                f"B={worker_b.returncode} {stdout_b} {stderr_b}"
            )
        concurrency_status = run(["--db", str(concurrency_db), "status"])
        concurrency_verify = run(verify_args(fixtures, concurrency_db))
        write_json_atomic(out_dir / "concurrency-status.json", concurrency_status)

        deterministic_db = temp / "deterministic.sqlite3"
        run(load_args(fixtures, deterministic_db))
        run(merge_args(fixtures, deterministic_db))
        run(
            [
                "--db",
                str(deterministic_db),
                "replay",
                "--logical-clock-ms",
                "7000",
            ]
        )
        deterministic_status = run(["--db", str(deterministic_db), "status"])
        determinism_equal = {
            "partitions": deterministic_status["partitions"],
            "totals": deterministic_status["totals"],
            "logical_clock_ms": deterministic_status["logical_clock_ms"],
        } == {
            "partitions": status_before["partitions"],
            "totals": status_before["totals"],
            "logical_clock_ms": status_before["logical_clock_ms"],
        }

        state_after, _ = LedgerStore(str(db)).read_state()
        unique_claims = {
            claim["event_seq"]
            for claim in state_after["delivery_claims"].values()
        }
        report = {
            "ok": True,
            "schema": "seqledger-acceptance-report/v1",
            "seed": committed_manifest["seed"],
            "fixture_regenerated_bitwise": seed_reproduced,
            "fixture_files": committed_manifest["files"],
            "load_event_count": load["event_count"],
            "merge_ledger_version": merge["ledger_version"],
            "merge_v1_converted_entries": merge["format_migration"]["v1_conversion_count"],
            "merge_conflict_entries": merge["format_migration"]["decision_counts"]["conflicts"],
            "replay_sigkill_observed": kill_exit == -signal.SIGKILL,
            "resume_recovered_pointer_version": resumed["recovered_pointer_version"],
            "resume_actions": resumed["action_count"],
            "backlog_before_patch": status_before["totals"]["backlog_depth"],
            "dead_letters_before_patch": status_before["totals"]["dead_letter_count"],
            "verify_before_patch_bitwise": verify_before["bitwise_match"],
            "verify_before_patch_sha256": verify_before["actual_projection_sha256"],
            "patch_affected_partitions": patch["invalidation"]["affected_partitions"],
            "patch_reused_partitions": patch["invalidation"]["reused_partitions"],
            "patch_closure_chain": [
                chain["path"]
                for chain in patch["invalidation"]["chains"]
                if len(chain["path"]) == 3
            ],
            "verify_after_patch_bitwise": verify_after["bitwise_match"],
            "verify_after_patch_sha256": verify_after["actual_projection_sha256"],
            "merge_kill_exit": merge_kill_exit,
            "merge_kill_pointer_unchanged": (
                before_merge_kill["ledger_version"]
                == after_merge_kill["ledger_version"]
                and before_merge_kill["ledger_sha256"]
                == after_merge_kill["ledger_sha256"]
            ),
            "merge_after_kill_succeeded": merge_recovered["ok"],
            "concurrent_workers_ok": True,
            "concurrent_backlog": concurrency_status["totals"]["backlog_depth"],
            "concurrent_verify_bitwise": concurrency_verify["bitwise_match"],
            "serial_status_bitwise_repeatable": determinism_equal,
            "unique_claim_event_count": len(unique_claims),
            "delivered_decision_count": status_after["totals"][
                "delivered_decision_count"
            ],
            "status_after_patch_backlog": status_after["totals"]["backlog_depth"],
        }
        required = [
            seed_reproduced,
            kill_exit == -signal.SIGKILL,
            status_before["totals"]["backlog_depth"] == 0,
            verify_before["bitwise_match"],
            len(patch["invalidation"]["affected_partitions"]) > 0,
            verify_after["bitwise_match"],
            before_merge_kill["ledger_version"]
            == after_merge_kill["ledger_version"],
            concurrency_status["totals"]["backlog_depth"] == 0,
            concurrency_verify["bitwise_match"],
            determinism_equal,
            len(unique_claims) == 18,
        ]
        if not all(required):
            report["ok"] = False
        write_json_atomic(out_dir / "acceptance-report.json", report)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
