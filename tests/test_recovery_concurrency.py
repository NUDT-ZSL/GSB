from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tests.helpers import SAMPLES, run_cli


class RecoveryAndConcurrencyTests(unittest.TestCase):
    def _load_sample(self, database: Path, sample: str, clock: int) -> None:
        run_cli(
            "load",
            "--db",
            str(database),
            "--manifest",
            str(SAMPLES / sample / "manifest.json"),
            "--config",
            str(SAMPLES / sample / "config.json"),
            "--clock",
            str(clock),
            check=True,
        )

    def _ledger_matches_objects(self, database: Path, bucket: str) -> None:
        with sqlite3.connect(database) as connection:
            used = {
                row[0]: row[1]
                for row in connection.execute(
                    "SELECT layer, used_bytes FROM layers WHERE bucket = ?",
                    (bucket,),
                )
            }
            computed = {
                row[0]: row[1]
                for row in connection.execute(
                    """
                    SELECT current_layer, SUM(size_bytes) FROM objects
                    WHERE bucket = ? GROUP BY current_layer
                    """,
                    (bucket,),
                )
            }
        self.assertEqual(used.get("hot", 0), computed.get("hot", 0))
        self.assertEqual(used.get("cold", 0), computed.get("cold", 0))

    def test_crash_after_reservation_is_recovered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "state.db"
            self._load_sample(database, "small", 1000)
            completed, _ = run_cli(
                "migrate",
                "--db",
                str(database),
                "--bucket",
                "small-a",
                "--round",
                "1",
                "--clock",
                "1100",
                env={"TIERLAB_CRASH_AT": "after_reserve"},
            )
            self.assertEqual(completed.returncode, 137)
            status = run_cli(
                "status",
                "--db",
                str(database),
                check=True,
            )[1]["buckets"][0]
            self.assertEqual(len(status["inflight"]), 2)
            self.assertEqual(status["last_batch"]["status"], "in_flight")
            self.assertEqual(status["mode"], "exact")
            self.assertIsNotNone(status["last_mode_decision"])
            verify = run_cli("verify", "--db", str(database))
            self.assertEqual(verify[0].returncode, 3)
            self.assertIn(
                "E_IN_FLIGHT_BATCH",
                {item["code"] for item in verify[1]["violations"]},
            )
            resumed = run_cli(
                "resume",
                "--db",
                str(database),
                check=True,
            )[1]
            self.assertEqual(resumed["resumed_count"], 1)
            status = run_cli(
                "status",
                "--db",
                str(database),
                check=True,
            )[1]["buckets"][0]
            self.assertEqual(status["inflight"], [])
            self.assertEqual(status["round_watermark"], 1)
            self._ledger_matches_objects(database, "small-a")
            verify = run_cli(
                "verify",
                "--db",
                str(database),
                check=True,
            )[1]
            self.assertTrue(verify["ok"])

    def test_crash_during_finalize_rolls_back_and_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "state.db"
            self._load_sample(database, "small", 1000)
            completed, _ = run_cli(
                "migrate",
                "--db",
                str(database),
                "--bucket",
                "small-a",
                "--round",
                "1",
                "--clock",
                "1100",
                env={"TIERLAB_CRASH_AT": "during_finalize"},
            )
            self.assertEqual(completed.returncode, 137)
            status = run_cli(
                "status",
                "--db",
                str(database),
                check=True,
            )[1]["buckets"][0]
            self.assertEqual(len(status["inflight"]), 2)
            self.assertEqual(status["round_watermark"], 0)
            run_cli("resume", "--db", str(database), check=True)
            self._ledger_matches_objects(database, "small-a")
            verify = run_cli(
                "verify",
                "--db",
                str(database),
                check=True,
            )[1]
            self.assertTrue(verify["ok"])

    def test_crash_during_reservation_leaves_no_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "state.db"
            self._load_sample(database, "small", 1000)
            completed, _ = run_cli(
                "migrate",
                "--db",
                str(database),
                "--bucket",
                "small-a",
                "--round",
                "1",
                "--clock",
                "1100",
                env={"TIERLAB_CRASH_AT": "during_reserve"},
            )
            self.assertEqual(completed.returncode, 137)
            status = run_cli(
                "status",
                "--db",
                str(database),
                check=True,
            )[1]["buckets"][0]
            self.assertEqual(status["round_watermark"], 0)
            self.assertIsNone(status["last_batch"])
            self.assertEqual(status["inflight"], [])
            run_cli(
                "migrate",
                "--db",
                str(database),
                "--bucket",
                "small-a",
                "--round",
                "1",
                "--clock",
                "1100",
                check=True,
            )
            verify = run_cli(
                "verify",
                "--db",
                str(database),
                check=True,
            )[1]
            self.assertTrue(verify["ok"])

    def test_resume_detects_half_committed_object_and_reconciles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.json"
            config = root / "config.json"
            manifest.write_text(
                json.dumps(
                    {
                        "bucket": "tiny-a",
                        "objects": [
                            {
                                "key": "hot-low",
                                "size_bytes": 100,
                                "last_access_clock": 0,
                                "access_count": 0,
                                "heat_weight": 1,
                                "declared_tier": "hot",
                                "migratable": True,
                            },
                            {
                                "key": "cold-high",
                                "size_bytes": 100,
                                "last_access_clock": 0,
                                "access_count": 20,
                                "heat_weight": 100,
                                "declared_tier": "cold",
                                "migratable": True,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            config.write_text(
                json.dumps(
                    {
                        "hot_capacity_bytes": 300,
                        "cold_capacity_bytes": 300,
                        "round_byte_budget": 1000,
                        "migration_concurrency": 1,
                        "hot_threshold": "5",
                        "clock_tolerance": 1000,
                        "mode": "exact",
                        "backoff": {
                            "base_delay_clocks": 50,
                            "max_delay_clocks": 200,
                            "multiplier": 2,
                            "max_consecutive_failures": 3,
                        },
                        "sampling": {
                            "ratio": "0.5",
                            "confidence_z": "1.96",
                            "min_sample_per_stratum": 2,
                            "random_seed": 3,
                        },
                        "adaptive": {
                            "exact_max_objects": 100,
                            "exact_max_candidates": 100,
                            "max_estimated_error_ratio": "0.5",
                        },
                    }
                ),
                encoding="utf-8",
            )
            database = root / "state.db"
            run_cli(
                "load",
                "--db",
                str(database),
                "--manifest",
                str(manifest),
                "--config",
                str(config),
                "--clock",
                "0",
                check=True,
            )
            completed, _ = run_cli(
                "migrate",
                "--db",
                str(database),
                "--bucket",
                "tiny-a",
                "--round",
                "1",
                "--clock",
                "0",
                env={"TIERLAB_CRASH_AT": "after_reserve"},
            )
            self.assertEqual(completed.returncode, 137)
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    UPDATE objects
                    SET current_layer = 'hot', version = version + 1,
                        failure_count = 2, backoff_until_clock = 99
                    WHERE bucket = 'tiny-a' AND key = 'cold-high'
                    """
                )
            run_cli(
                "resume",
                "--db",
                str(database),
                check=True,
            )
            self._ledger_matches_objects(database, "tiny-a")
            verify = run_cli(
                "verify",
                "--db",
                str(database),
                check=True,
            )[1]
            self.assertTrue(verify["ok"])
            with sqlite3.connect(database) as connection:
                result_json = connection.execute(
                    """
                    SELECT result_json FROM batches
                    WHERE bucket = 'tiny-a' AND round = 1
                    """
                ).fetchone()[0]
                object_row = connection.execute(
                    """
                    SELECT current_layer, version, failure_count,
                           backoff_until_clock, migration_state
                    FROM objects
                    WHERE bucket = 'tiny-a' AND key = 'cold-high'
                    """
                ).fetchone()
            result = json.loads(result_json)
            self.assertEqual(
                result["recovery"]["recovered_committed_observations"],
                1,
            )
            self.assertEqual(object_row[0], "hot")
            self.assertEqual(object_row[1], 1)
            self.assertEqual(object_row[2], 0)
            self.assertEqual(object_row[3], 0)
            self.assertEqual(object_row[4], "migrated")

    def test_concurrent_buckets_and_same_batch_idempotence(self) -> None:
        import subprocess
        import sys

        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "state.db"
            self._load_sample(database, "small", 1000)
            self._load_sample(database, "adaptive", 10000)
            commands = [
                [
                    sys.executable,
                    "-m",
                    "tierlab",
                    "migrate",
                    "--db",
                    str(database),
                    "--bucket",
                    "small-a",
                    "--round",
                    "1",
                    "--clock",
                    "1100",
                ],
                [
                    sys.executable,
                    "-m",
                    "tierlab",
                    "migrate",
                    "--db",
                    str(database),
                    "--bucket",
                    "adaptive-a",
                    "--round",
                    "1",
                    "--clock",
                    "10100",
                ],
            ]
            processes = [
                subprocess.Popen(
                    command,
                    cwd=SAMPLES.parent,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                for command in commands
            ]
            for process in processes:
                stdout, stderr = process.communicate(timeout=60)
                self.assertEqual(
                    process.returncode,
                    0,
                    msg=f"stdout={stdout}\nstderr={stderr}",
                )
            verify = run_cli(
                "verify",
                "--db",
                str(database),
                check=True,
            )[1]
            self.assertTrue(verify["ok"])
            self.assertEqual(len(verify["buckets"]), 2)

            replay_commands = [
                [
                    sys.executable,
                    "-m",
                    "tierlab",
                    "migrate",
                    "--db",
                    str(database),
                    "--bucket",
                    "small-a",
                    "--round",
                    "1",
                    "--clock",
                    "1100",
                ]
                for _ in range(4)
            ]
            replay_processes = [
                subprocess.Popen(
                    command,
                    cwd=SAMPLES.parent,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                for command in replay_commands
            ]
            payloads = []
            for process in replay_processes:
                stdout, stderr = process.communicate(timeout=60)
                self.assertEqual(
                    process.returncode,
                    0,
                    msg=f"stdout={stdout}\nstderr={stderr}",
                )
                payloads.append(json.loads(stdout))
            self.assertTrue(all(payload == payloads[0] for payload in payloads))
            with sqlite3.connect(database) as connection:
                duplicate_batches = connection.execute(
                    """
                    SELECT COUNT(*) FROM batches
                    WHERE bucket = 'small-a' AND round = 1
                    """
                ).fetchone()[0]
                duplicate_inflight = connection.execute(
                    """
                    SELECT COUNT(*) FROM inflight
                    WHERE bucket = 'small-a' AND round = 1
                    """
                ).fetchone()[0]
            self.assertEqual(duplicate_batches, 1)
            self.assertEqual(duplicate_inflight, 2)


if __name__ == "__main__":
    unittest.main()
