from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def cli(*args: str, env: dict[str, str] | None = None) -> tuple[int, dict]:
    process = subprocess.run(
        ["python3", "-m", "slowplan", *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env={**os.environ, **(env or {})},
    )
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError:
        payload = {"stdout": process.stdout, "stderr": process.stderr}
    return process.returncode, payload


def prepare_baseline(db: str) -> None:
    assert cli("load", "--db", db)[0] == 0
    assert cli("ingest", "--db", db, "--samples", str(SAMPLES / "slow_queries.json"))[0] == 0
    assert cli("profile", "--db", db)[0] == 0


def prepare_refresh_state(db: str) -> None:
    prepare_baseline(db)
    assert cli("apply", "--db", db, "--patch", str(SAMPLES / "stats_patch_v2.json"))[0] == 0
    assert (
        cli(
            "ingest",
            "--db",
            db,
            "--samples",
            str(SAMPLES / "slow_queries_after_refresh.json"),
        )[0]
        == 0
    )


def finish_processes(processes: list[subprocess.Popen]) -> list[int]:
    for process in processes:
        process.communicate()
    return [process.returncode for process in processes]


class RecoveryTests(unittest.TestCase):
    def test_ingest_crash_recovery_matches_uninterrupted_run(self) -> None:
        with tempfile.TemporaryDirectory(prefix="slowplan-rec-ingest-a-") as temp_a, tempfile.TemporaryDirectory(
            prefix="slowplan-rec-ingest-b-"
        ) as temp_b:
            expected_db = str(Path(temp_a) / "state.sqlite")
            crashed_db = str(Path(temp_b) / "state.sqlite")
            prepare_baseline(expected_db)
            expected_status = cli("status", "--db", expected_db)[1]

            assert cli("load", "--db", crashed_db)[0] == 0
            code, _ = cli(
                "ingest",
                "--db",
                crashed_db,
                "--samples",
                str(SAMPLES / "slow_queries.json"),
                env={"SLOWPLAN_FAULT_POINT": "ingest_after_journal"},
            )
            self.assertEqual(code, 91)
            crashed = cli("status", "--db", crashed_db)[1]
            self.assertEqual(crashed["sample_count"], 0)
            self.assertEqual(crashed["sample_watermark"], 0)
            resumed = cli("resume", "--db", crashed_db)[1]
            self.assertEqual(resumed["resumed_count"], 1)
            assert cli("profile", "--db", crashed_db)[0] == 0
            self.assertEqual(cli("status", "--db", crashed_db)[1], expected_status)

    def test_apply_and_profile_crash_recovery_match_uninterrupted_run(self) -> None:
        with tempfile.TemporaryDirectory(prefix="slowplan-rec-uninterrupted-") as temp:
            baseline_db = str(Path(temp) / "state.sqlite")
            prepare_refresh_state(baseline_db)
            assert cli("profile", "--db", baseline_db)[0] == 0
            expected_status = cli("status", "--db", baseline_db)[1]
            expected_verify = cli("verify", "--db", baseline_db)[1]
            self.assertTrue(expected_verify["ok"])

        with tempfile.TemporaryDirectory(prefix="slowplan-rec-apply-") as temp:
            db = str(Path(temp) / "state.sqlite")
            prepare_baseline(db)
            code, _ = cli(
                "apply",
                "--db",
                db,
                "--patch",
                str(SAMPLES / "stats_patch_v2.json"),
                env={"SLOWPLAN_FAULT_POINT": "apply_after_journal"},
            )
            self.assertEqual(code, 91)
            crashed = cli("status", "--db", db)[1]
            self.assertEqual(crashed["committed_stats_version"], 1)
            resumed = cli("resume", "--db", db)[1]
            self.assertEqual(resumed["resumed_count"], 1)
            assert (
                cli(
                    "ingest",
                    "--db",
                    db,
                    "--samples",
                    str(SAMPLES / "slow_queries_after_refresh.json"),
                )[0]
                == 0
            )
            assert cli("profile", "--db", db)[0] == 0
            status = cli("status", "--db", db)[1]
            verify = cli("verify", "--db", db)[1]
            self.assertEqual(status, expected_status)
            self.assertTrue(verify["ok"])
            self.assertEqual(verify["state_digest"], expected_verify["state_digest"])

        with tempfile.TemporaryDirectory(prefix="slowplan-rec-profile-") as temp:
            db = str(Path(temp) / "state.sqlite")
            prepare_refresh_state(db)
            code, _ = cli(
                "profile",
                "--db",
                db,
                env={"SLOWPLAN_FAULT_POINT": "profile_after_journal"},
            )
            self.assertEqual(code, 91)
            crashed = cli("status", "--db", db)[1]
            self.assertEqual(crashed["profile_generation"], 1)
            self.assertEqual(crashed["invalidations"]["pending"], 2)
            resumed = cli("resume", "--db", db)[1]
            self.assertEqual(resumed["resumed_count"], 1)
            status = cli("status", "--db", db)[1]
            verify = cli("verify", "--db", db)[1]
            self.assertEqual(status, expected_status)
            self.assertTrue(verify["ok"])
            self.assertEqual(verify["state_digest"], expected_verify["state_digest"])


class ConcurrencyTests(unittest.TestCase):
    def test_parallel_duplicates_commit_once(self) -> None:
        with tempfile.TemporaryDirectory(prefix="slowplan-concurrent-") as temp:
            db = str(Path(temp) / "state.sqlite")
            assert cli("load", "--db", db)[0] == 0
            ingest_processes = [
                subprocess.Popen(
                    [
                        "python3",
                        "-m",
                        "slowplan",
                        "ingest",
                        "--db",
                        db,
                        "--samples",
                        str(SAMPLES / "slow_queries.json"),
                    ],
                    cwd=ROOT,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for _ in range(4)
            ]
            self.assertEqual(finish_processes(ingest_processes), [0, 0, 0, 0])
            conn = sqlite3.connect(db)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 10)
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM operations WHERE op_type='ingest'"
                    ).fetchone()[0],
                    1,
                )
            finally:
                conn.close()
            assert cli("profile", "--db", db)[0] == 0
            apply_processes = [
                subprocess.Popen(
                    [
                        "python3",
                        "-m",
                        "slowplan",
                        "apply",
                        "--db",
                        db,
                        "--patch",
                        str(SAMPLES / "stats_patch_v2.json"),
                    ],
                    cwd=ROOT,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for _ in range(4)
            ]
            self.assertEqual(finish_processes(apply_processes), [0, 0, 0, 0])
            conn = sqlite3.connect(db)
            try:
                self.assertEqual(
                    [row[0] for row in conn.execute("SELECT version FROM stats_snapshots ORDER BY version")],
                    [1, 2],
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM operations WHERE op_type='apply'"
                    ).fetchone()[0],
                    1,
                )
            finally:
                conn.close()
            assert (
                cli(
                    "ingest",
                    "--db",
                    db,
                    "--samples",
                    str(SAMPLES / "slow_queries_after_refresh.json"),
                )[0]
                == 0
            )
            profile_processes = [
                subprocess.Popen(
                    ["python3", "-m", "slowplan", "profile", "--db", db],
                    cwd=ROOT,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for _ in range(4)
            ]
            self.assertEqual(finish_processes(profile_processes), [0, 0, 0, 0])
            verify = cli("verify", "--db", db)
            self.assertEqual(verify[0], 0)
            self.assertTrue(verify[1]["ok"], verify[1]["failed_checks"])

    def test_parallel_disjoint_batches_reach_stable_state(self) -> None:
        with tempfile.TemporaryDirectory(prefix="slowplan-order-a-") as temp_a, tempfile.TemporaryDirectory(
            prefix="slowplan-order-b-"
        ) as temp_b:
            db_a = str(Path(temp_a) / "state.sqlite")
            db_b = str(Path(temp_b) / "state.sqlite")
            for db in (db_a, db_b):
                assert cli("load", "--db", db)[0] == 0
                assert cli("apply", "--db", db, "--patch", str(SAMPLES / "stats_patch_v2.json"))[0] == 0
            assert (
                cli(
                    "ingest",
                    "--db",
                    db_a,
                    "--samples",
                    str(SAMPLES / "slow_queries.json"),
                )[0]
                == 0
            )
            assert (
                cli(
                    "ingest",
                    "--db",
                    db_a,
                    "--samples",
                    str(SAMPLES / "slow_queries_after_refresh.json"),
                )[0]
                == 0
            )
            assert (
                cli(
                    "ingest",
                    "--db",
                    db_b,
                    "--samples",
                    str(SAMPLES / "slow_queries_after_refresh.json"),
                )[0]
                == 0
            )
            assert (
                cli(
                    "ingest",
                    "--db",
                    db_b,
                    "--samples",
                    str(SAMPLES / "slow_queries.json"),
                )[0]
                == 0
            )
            assert cli("profile", "--db", db_a)[0] == 0
            assert cli("profile", "--db", db_b)[0] == 0
            status_a = cli("status", "--db", db_a)[1]
            status_b = cli("status", "--db", db_b)[1]
            self.assertEqual(status_a, status_b)
            self.assertTrue(cli("verify", "--db", db_a)[1]["ok"])
            self.assertTrue(cli("verify", "--db", db_b)[1]["ok"])


if __name__ == "__main__":
    unittest.main()
