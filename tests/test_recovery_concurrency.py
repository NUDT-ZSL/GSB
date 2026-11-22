from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path

from hotgray.engine import Engine

from .support import (
    ROOT,
    TempWorkspace,
    new_engine,
    run_cli,
    sqlite_rows,
    warmed_engine,
)


class RecoveryAndConcurrencyTests(unittest.TestCase):
    def test_forced_exit_after_materialization_recovers_only_pending_steps(self) -> None:
        with TempWorkspace() as workspace:
            db_path = workspace.path / "state.db"
            init = run_cli(
                [
                    "init",
                    "--db",
                    str(db_path),
                    "--samples",
                    str(ROOT / "samples"),
                ]
            )
            self.assertEqual(init.returncode, 0, init.stderr + init.stdout)
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(ROOT) + os.pathsep + environment.get("PYTHONPATH", "")
            process = subprocess.run(
                [
                    str(Path(os.sys.executable)),
                    "-m",
                    "hotgray",
                    "apply-patch",
                    "--db",
                    str(db_path),
                    "--samples",
                    str(ROOT / "samples"),
                    "--at-ms",
                    "1000",
                    "--crash-after-materialize",
                    "5",
                ],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(process.returncode, 70, process.stderr)
            self.assertNotIn('"ok": true', process.stdout.lower())
            self.assertEqual(
                sqlite_rows(
                    db_path,
                    "SELECT COUNT(*) AS count FROM version_bindings WHERE version = 2",
                )[0]["count"],
                0,
            )

            engine = Engine.open(db_path)
            resumed = engine.resume(at_ms=1100)
            self.assertEqual(resumed["status"], "warming")
            self.assertEqual(
                sqlite_rows(
                    db_path,
                    "SELECT COUNT(*) AS count FROM round_steps WHERE step_key = 'patch.applied'",
                )[0]["count"],
                1,
            )
            self.assertEqual(
                sqlite_rows(
                    db_path,
                    "SELECT COUNT(*) AS count FROM version_bindings WHERE version = 2 AND committed = 1",
                )[0]["count"],
                1,
            )
            self.assertEqual(
                sqlite_rows(db_path, "SELECT COUNT(*) AS count FROM materializations")[0]["count"],
                10,
            )
            engine.tick(at_ms=1200)
            snapshot = engine.snapshot()
            self.assertTrue(
                all(item["state"] == "gray" for item in snapshot["replicas"] if item["eligible"])
            )
            self.assertEqual(snapshot["round"]["current_level"], 1)
            self.assertEqual(snapshot["events"][1]["event_type"], "patch_committed")

    def test_concurrent_patch_processes_commit_once(self) -> None:
        with TempWorkspace() as workspace:
            db_path = workspace.path / "state.db"
            new_engine(workspace.path)
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(ROOT) + os.pathsep + environment.get("PYTHONPATH", "")
            processes = [
                subprocess.Popen(
                    [
                        os.sys.executable,
                        "-m",
                        "hotgray",
                        "apply-patch",
                        "--db",
                        str(db_path),
                        "--samples",
                        str(ROOT / "samples"),
                        "--at-ms",
                        "1000",
                    ],
                    cwd=ROOT,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for _ in range(4)
            ]
            outputs = []
            for process in processes:
                stdout, stderr = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, stderr)
                outputs.append(json.loads(stdout))
            self.assertEqual(sum(1 for item in outputs if not item["reused"]), 1)
            self.assertEqual(
                sqlite_rows(db_path, "SELECT COUNT(*) AS count FROM version_bindings WHERE version = 2")[0]["count"],
                1,
            )
            self.assertEqual(
                sqlite_rows(db_path, "SELECT COUNT(*) AS count FROM materializations")[0]["count"],
                10,
            )

    def test_concurrent_identical_requests_are_committed_once(self) -> None:
        with TempWorkspace() as workspace:
            engine = warmed_engine(workspace.path)
            engine.advance_stage(at_ms=1200, level=1)
            db_path = engine.store.db_path
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(ROOT) + os.pathsep + environment.get("PYTHONPATH", "")
            processes = [
                subprocess.Popen(
                    [
                        os.sys.executable,
                        "-m",
                        "hotgray",
                        "request",
                        "--db",
                        str(db_path),
                        "--samples",
                        str(ROOT / "samples"),
                        "--at-ms",
                        "1210",
                        "--replica-id",
                        "replica-a",
                        "--request-id",
                        "concurrent-request",
                        "--input",
                        "[1,2,3]",
                    ],
                    cwd=ROOT,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for _ in range(4)
            ]
            outputs = []
            for process in processes:
                stdout, stderr = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, stderr)
                outputs.append(json.loads(stdout))
            self.assertEqual(sum(1 for item in outputs if not item["reused"]), 1)
            self.assertEqual(
                sqlite_rows(db_path, "SELECT COUNT(*) AS count FROM requests WHERE request_id = 'concurrent-request'")[0]["count"],
                1,
            )


if __name__ == "__main__":
    unittest.main()
