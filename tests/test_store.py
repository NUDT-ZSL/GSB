from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from qos_pipeline.serialization import canonical_json

from .helpers import ROOT


MANIFEST = ROOT / "samples" / "manifest.json"


def run_cli(*arguments: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    actual_env = os.environ.copy()
    actual_env["PYTHONPATH"] = (
        str(ROOT / "src")
        + os.pathsep
        + actual_env.get("PYTHONPATH", "")
    )
    if env:
        actual_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "qos_pipeline", *arguments],
        cwd=ROOT,
        env=actual_env,
        text=True,
        capture_output=True,
        check=False,
    )


class StoreTests(unittest.TestCase):
    def test_shape_resume_status_and_verify(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "state.db"
            loaded = run_cli(
                "load", "--manifest", str(MANIFEST), "--db", str(database)
            )
            self.assertEqual(loaded.returncode, 0, loaded.stdout + loaded.stderr)
            partial = run_cli(
                "shape", "--db", str(database), "--max-steps", "20", "--no-steps"
            )
            self.assertEqual(partial.returncode, 0, partial.stdout + partial.stderr)
            status = run_cli("status", "--db", str(database))
            payload = __import__("json").loads(status.stdout)
            self.assertEqual(payload["last_step"], 19)
            resumed = run_cli("resume", "--db", str(database), "--no-steps")
            self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
            verified = run_cli(
                "verify", "--db", str(database), "--no-steps"
            )
            result = __import__("json").loads(verified.stdout)
            self.assertTrue(result["ok"])
            self.assertTrue(result["complete"])
            self.assertTrue(result["chain_valid"])
            self.assertIsNone(result["first_mismatch"])

    def test_hard_crash_resumes_without_replaying_committed_steps(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "crash.db"
            self.assertEqual(
                run_cli(
                    "load", "--manifest", str(MANIFEST), "--db", str(database)
                ).returncode,
                0,
            )
            crashed = run_cli(
                "shape",
                "--db",
                str(database),
                "--max-steps",
                "20",
                "--crash-at-step",
                "12",
                "--crash-point",
                "after_refill",
                "--no-steps",
            )
            self.assertEqual(crashed.returncode, 137)
            status = __import__("json").loads(
                run_cli("status", "--db", str(database)).stdout
            )
            self.assertEqual(status["last_step"], 11)
            resumed = __import__("json").loads(
                run_cli("resume", "--db", str(database), "--no-steps").stdout
            )
            self.assertEqual(resumed["status"]["last_step"], 119)
            self.assertNotIn(11, resumed["steps_committed_this_call"])
            self.assertEqual(resumed["steps_committed_this_call"], list(range(12, 120)))
            verified = __import__("json").loads(
                run_cli("verify", "--db", str(database), "--no-steps").stdout
            )
            self.assertTrue(verified["ok"])

    def test_parallel_writers_commit_each_step_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "parallel.db"
            self.assertEqual(
                run_cli(
                    "load", "--manifest", str(MANIFEST), "--db", str(database)
                ).returncode,
                0,
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "src")
            processes = [
                subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "qos_pipeline",
                        "shape",
                        "--db",
                        str(database),
                        "--no-steps",
                    ],
                    cwd=ROOT,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for _ in range(4)
            ]
            outputs = [process.communicate(timeout=60) for process in processes]
            self.assertTrue(all(process.returncode == 0 for process in processes), outputs)
            verified = __import__("json").loads(
                run_cli("verify", "--db", str(database), "--no-steps").stdout
            )
            self.assertTrue(verified["ok"])
            with sqlite3.connect(database) as connection:
                counts = connection.execute(
                    "SELECT step_index, COUNT(*) FROM steps GROUP BY step_index"
                ).fetchall()
            self.assertTrue(counts)
            self.assertTrue(all(count == 1 for _, count in counts))

    def test_verify_detects_tampered_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "tampered.db"
            self.assertEqual(
                run_cli(
                    "load", "--manifest", str(MANIFEST), "--db", str(database)
                ).returncode,
                0,
            )
            self.assertEqual(
                run_cli(
                    "shape", "--db", str(database), "--max-steps", "2", "--no-steps"
                ).returncode,
                0,
            )
            with sqlite3.connect(database) as connection:
                row = connection.execute(
                    "SELECT payload_json FROM steps WHERE step_index=0"
                ).fetchone()
                payload = __import__("json").loads(row[0])
                payload["totals"]["passed_bytes"] += 1
                connection.execute(
                    "UPDATE steps SET payload_json=? WHERE step_index=0",
                    (canonical_json(payload),),
                )
                connection.commit()
            result = __import__("json").loads(
                run_cli("verify", "--db", str(database), "--no-steps").stdout
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["first_mismatch"]["kind"], "payload_mismatch")


if __name__ == "__main__":
    unittest.main()

