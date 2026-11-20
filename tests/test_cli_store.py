from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def run_cli(
    db: Path,
    *args: str,
    sample_dir: Path = SAMPLES,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        "-m",
        "controlplane",
        "--db",
        str(db),
        "--sample-dir",
        str(sample_dir),
        *args,
    ]
    result = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"command failed ({result.returncode}): {' '.join(command)}\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
    return result


class CliStoreTests(unittest.TestCase):
    def test_invalid_input_does_not_create_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bad_samples = root / "samples"
            shutil.copytree(SAMPLES, bad_samples)
            clusters_path = bad_samples / "clusters.json"
            data = json.loads(clusters_path.read_text(encoding="utf-8"))
            data["clusters"][0]["priority"] = -1
            clusters_path.write_text(json.dumps(data), encoding="utf-8")
            db = root / "state.db"
            result = run_cli(
                db, "load", sample_dir=bad_samples, check=False
            )
            self.assertEqual(result.returncode, 2)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["error"]["code"], "E_INVALID_RANGE")
            self.assertFalse(db.exists())
            self.assertFalse(Path(str(db) + ".lock").exists())

    def test_fresh_runs_are_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as left_temp, tempfile.TemporaryDirectory() as right_temp:
            left = Path(left_temp) / "state.db"
            right = Path(right_temp) / "state.db"
            reports: dict[str, tuple[str, str]] = {}
            for name, db in (("left", left), ("right", right)):
                load = run_cli(db, "load")
                distribute = run_cli(db, "distribute")
                status = run_cli(db, "status")
                explain = run_cli(db, "explain")
                verify = run_cli(db, "verify")
                reports[name] = (
                    load.stdout
                    + distribute.stdout
                    + status.stdout
                    + explain.stdout
                    + verify.stdout
                )
            self.assertEqual(reports["left"], reports["right"])

    def test_distribute_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "state.db"
            run_cli(db, "load")
            first = run_cli(db, "distribute")
            second = run_cli(db, "distribute")
            self.assertEqual(first.stdout, second.stdout)
            payload = json.loads(second.stdout)
            self.assertEqual(payload["round_id"], "r0001")
            self.assertEqual(payload["logical_clock"], 10)
            with sqlite3.connect(db) as conn:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM rounds").fetchone()[0],
                    1,
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM round_clusters"
                    ).fetchone()[0],
                    8,
                )

    def test_resume_requires_an_existing_pending_round(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "state.db"
            run_cli(db, "load")
            result = run_cli(db, "resume", check=False)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(
                json.loads(result.stdout)["error"]["code"],
                "E_NO_PENDING_ROUND",
            )
            missing_db = root / "missing.db"
            missing = run_cli(
                missing_db, "resume", check=False
            )
            self.assertEqual(missing.returncode, 2)
            self.assertEqual(
                json.loads(missing.stdout)["error"]["code"],
                "E_DB_MISSING",
            )
            self.assertFalse(missing_db.exists())

    def test_interrupt_then_resume_only_computes_missing_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "state.db"
            run_cli(db, "load")
            interrupted = run_cli(
                db, "distribute", "--interrupt-after", "3", check=False
            )
            self.assertEqual(interrupted.returncode, 75)
            before = run_cli(db, "status")
            before_payload = json.loads(before.stdout)
            self.assertIsNone(before_payload["logical_clock"])
            self.assertEqual(len(before_payload["pending_rounds"]), 1)
            self.assertEqual(
                before_payload["pending_rounds"][0]["completed_work"], 3
            )
            with sqlite3.connect(db) as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM current_pointer"
                    ).fetchone()[0],
                    0,
                )

            resumed = run_cli(db, "resume", "--report-work")
            resumed_payload = json.loads(resumed.stdout)
            self.assertEqual(resumed_payload["status"], "COMMITTED")
            self.assertEqual(resumed_payload["computed_this_run"], 5)
            self.assertEqual(resumed_payload["reused_this_run"], 3)
            self.assertEqual(resumed_payload["logical_clock"], 10)
            verify = run_cli(db, "verify")
            self.assertTrue(json.loads(verify.stdout)["ok"])
            repeated = run_cli(db, "distribute")
            self.assertEqual(repeated.stdout, run_cli(db, "distribute").stdout)

    def test_multiple_processes_commit_one_complete_round(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "state.db"
            run_cli(db, "load")
            groups = [
                ("use1-a", "use1-b"),
                ("usw1-a", "usw1-b"),
                ("euc1-a", "euc1-b"),
                ("aps1-a", "aps1-b"),
            ]
            processes = []
            for group in groups:
                args = [
                    sys.executable,
                    "-m",
                    "controlplane",
                    "--db",
                    str(db),
                    "--sample-dir",
                    str(SAMPLES),
                    "distribute",
                ]
                for cluster in group:
                    args.extend(["--cluster", cluster])
                processes.append(
                    subprocess.Popen(
                        args,
                        cwd=ROOT,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                )
            for process in processes:
                stdout, stderr = process.communicate(timeout=30)
                self.assertEqual(
                    process.returncode,
                    0,
                    msg=f"stdout={stdout}\nstderr={stderr}",
                )
            with sqlite3.connect(db) as conn:
                rounds = conn.execute(
                    "SELECT round_id, status, logical_clock FROM rounds"
                ).fetchall()
                self.assertEqual(rounds, [("r0001", "COMMITTED", 10)])
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM work_items WHERE status='COMPLETE'"
                    ).fetchone()[0],
                    8,
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM round_clusters"
                    ).fetchone()[0],
                    8,
                )
            verify = run_cli(db, "verify")
            self.assertTrue(json.loads(verify.stdout)["ok"])

    def test_verify_detects_corrupted_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "state.db"
            run_cli(db, "load")
            run_cli(db, "distribute")
            with sqlite3.connect(db) as conn:
                conn.execute(
                    "UPDATE round_clusters SET summary_hash='corrupt' "
                    "WHERE cluster='use1-a'"
                )
                conn.commit()
            result = run_cli(db, "verify", check=False)
            self.assertEqual(result.returncode, 3)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["ok"])
            checks = {check["name"]: check["ok"] for check in payload["checks"]}
            self.assertFalse(checks["all_result_hashes_valid"])

    def test_version_change_replaces_pointer_without_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            changed_samples = root / "changed-samples"
            shutil.copytree(SAMPLES, changed_samples)
            db = root / "state.db"
            run_cli(db, "load")
            v1 = run_cli(db, "distribute")
            v1_payload = json.loads(v1.stdout)

            policy_path = changed_samples / "distribution_policy.json"
            policy = json.loads(policy_path.read_text(encoding="utf-8"))
            policy["policy"]["retry_budget"] = 1
            policy_path.write_text(json.dumps(policy), encoding="utf-8")
            v2 = run_cli(
                db, "distribute", sample_dir=changed_samples
            )
            v2_payload = json.loads(v2.stdout)
            self.assertNotEqual(
                v1_payload["version_id"], v2_payload["version_id"]
            )
            self.assertEqual(v2_payload["round_id"], "r0002")
            self.assertEqual(v2_payload["logical_clock"], 20)

            stale = run_cli(
                db, "distribute", sample_dir=SAMPLES, check=False
            )
            self.assertEqual(stale.returncode, 2)
            self.assertEqual(
                json.loads(stale.stdout)["error"]["code"],
                "E_STALE_VERSION",
            )
            with sqlite3.connect(db) as conn:
                pointer = conn.execute(
                    "SELECT round_id, version_id, logical_clock "
                    "FROM current_pointer"
                ).fetchone()
                self.assertEqual(
                    pointer,
                    (
                        v2_payload["round_id"],
                        v2_payload["version_id"],
                        v2_payload["logical_clock"],
                    ),
                )

    def test_old_pending_round_cannot_overwrite_newer_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            changed_samples = root / "changed-samples"
            shutil.copytree(SAMPLES, changed_samples)
            db = root / "state.db"
            run_cli(db, "load")
            interrupted = run_cli(
                db, "distribute", "--interrupt-after", "3", check=False
            )
            self.assertEqual(interrupted.returncode, 75)

            policy_path = changed_samples / "distribution_policy.json"
            policy = json.loads(policy_path.read_text(encoding="utf-8"))
            policy["policy"]["retry_budget"] = 1
            policy_path.write_text(json.dumps(policy), encoding="utf-8")
            v2 = json.loads(
                run_cli(db, "distribute", sample_dir=changed_samples).stdout
            )

            stale_resume = run_cli(
                db, "resume", sample_dir=SAMPLES, check=False
            )
            self.assertEqual(stale_resume.returncode, 2)
            self.assertEqual(
                json.loads(stale_resume.stdout)["error"]["code"],
                "E_STALE_VERSION",
            )
            with sqlite3.connect(db) as conn:
                pointer = conn.execute(
                    "SELECT round_id, version_id FROM current_pointer"
                ).fetchone()
                self.assertEqual(pointer, (v2["round_id"], v2["version_id"]))
                old_round = conn.execute(
                    "SELECT status FROM rounds WHERE version_id != ?",
                    (v2["version_id"],),
                ).fetchone()
                self.assertEqual(old_round[0], "PENDING")


if __name__ == "__main__":
    unittest.main()
