from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vmpool.engine import apply_request, initial_state
from vmpool.model import load_sample

from .helpers import SAMPLE, copy_sample, read_json, run_cli, spawn_cli, write_json


class StoreCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def load(self, name: str = "state.db") -> Path:
        db = self.root / name
        _, result = run_cli(
            "load", "--sample", str(SAMPLE), "--db", str(db)
        )
        self.assertTrue(result["ok"])
        return db

    def test_two_fresh_runs_are_byte_identical(self) -> None:
        commands = [
            ("load",),
            ("apply", "--seq", "13"),
            ("status",),
            ("diagnose",),
            ("verify",),
            ("resume",),
        ]
        outputs: list[list[bytes]] = []
        for run_index in range(2):
            db = self.root / f"deterministic-{run_index}.db"
            run_outputs: list[bytes] = []
            for command in commands:
                args = list(command)
                if command[0] == "load":
                    args.extend(["--sample", str(SAMPLE)])
                args.extend(["--db", str(db)])
                completed, _ = run_cli(*args)
                run_outputs.append(completed.stdout)
            outputs.append(run_outputs)
        self.assertEqual(outputs[0], outputs[1])

    def test_apply_idempotent_and_verify(self) -> None:
        db = self.load()
        _, first = run_cli(
            "apply", "--db", str(db), "--seq", "5"
        )
        self.assertEqual(first["decision"]["reason_code"], "E_BUDGET")
        first_bytes = run_cli("apply", "--db", str(db), "--seq", "5")[0].stdout
        second_bytes = run_cli("apply", "--db", str(db), "--seq", "5")[0].stdout
        self.assertEqual(first_bytes, second_bytes)
        _, status = run_cli("status", "--db", str(db))
        self.assertEqual(status["version"], 5)
        _, verification = run_cli("verify", "--db", str(db))
        self.assertTrue(verification["ok"], verification["errors"])

    def test_load_is_idempotent_with_global_db_option(self) -> None:
        db = self.root / "idempotent-load.db"
        first, parsed = run_cli(
            "--db",
            str(db),
            "load",
            "--sample",
            str(SAMPLE),
        )
        second, _ = run_cli(
            "load",
            "--sample",
            str(SAMPLE),
            "--db",
            str(db),
        )
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(parsed["version"], 0)
        self.assertTrue(parsed["ok"])

    def test_crash_after_round_start_recovers_by_recomputing(self) -> None:
        db = self.load("round-start.db")
        completed, _ = run_cli(
            "apply",
            "--db",
            str(db),
            "--seq",
            "13",
            "--fault-point",
            "round-started",
            expected_code=75,
        )
        self.assertEqual(completed.returncode, 75)
        _, status = run_cli("status", "--db", str(db))
        self.assertTrue(status["recovery_required"])
        self.assertEqual(status["version"], 0)
        _, resume = run_cli("resume", "--db", str(db))
        self.assertEqual(resume["status"], "recovered")
        self.assertEqual(
            resume["recovery"]["recomputed_rounds"], ["round-00000001"]
        )
        self.assertEqual(resume["recovery"]["reused_work"], [])
        _, verification = run_cli("verify", "--db", str(db))
        self.assertTrue(verification["ok"], verification["errors"])

    def test_crash_after_work_written_reuses_work(self) -> None:
        db = self.load("work-written.db")
        run_cli(
            "apply",
            "--db",
            str(db),
            "--seq",
            "13",
            "--fault-point",
            "work-written",
            expected_code=75,
        )
        _, resume = run_cli("resume", "--db", str(db))
        self.assertEqual(resume["status"], "recovered")
        self.assertEqual(
            resume["recovery"]["reused_work"], ["round-00000001"]
        )
        self.assertEqual(resume["recovery"]["recomputed_rounds"], [])
        _, verification = run_cli("verify", "--db", str(db))
        self.assertTrue(verification["ok"], verification["errors"])

    def test_concurrent_same_target_is_idempotent_and_not_oversold(self) -> None:
        db = self.load("concurrent.db")
        processes = [
            spawn_cli("apply", "--db", str(db), "--seq", "8")
            for _ in range(8)
        ]
        outputs: list[bytes] = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(
                process.returncode,
                0,
                stderr.decode("utf-8", "replace"),
            )
            outputs.append(stdout)
        self.assertEqual(len(set(outputs)), 1)
        _, status = run_cli("status", "--db", str(db))
        self.assertEqual(status["version"], 8)
        self.assertLessEqual(
            status["pool"]["occupied_cpu_fixed"],
            status["pool"]["available_cpu_fixed"],
        )
        self.assertLessEqual(
            status["pool"]["occupied_memory_fixed"],
            status["pool"]["available_memory_fixed"],
        )
        scenario = load_sample(SAMPLE)
        state = initial_state(scenario)
        for request in scenario.requests:
            if request.seq > 8:
                break
            state = apply_request(scenario, state, request).state
        self.assertEqual(status["pool"], state["pool"])
        self.assertEqual(status["budget"], state["budget"])
        _, verification = run_cli("verify", "--db", str(db))
        self.assertTrue(verification["ok"], verification["errors"])

    def test_concurrent_distinct_targets_serialize_to_same_prefix(self) -> None:
        db = self.load("concurrent-distinct.db")
        targets = [3, 4, 5, 6, 7, 8]
        processes = [
            spawn_cli("apply", "--db", str(db), "--seq", str(target))
            for target in targets
        ]
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(
                process.returncode,
                0,
                stderr.decode("utf-8", "replace"),
            )
            parsed = __import__("json").loads(stdout)
            self.assertTrue(parsed["ok"])
        _, status = run_cli("status", "--db", str(db))
        self.assertEqual(status["version"], 8)
        self.assertEqual(status["pending_rounds"], [])
        _, verification = run_cli("verify", "--db", str(db))
        self.assertTrue(verification["ok"], verification["errors"])

    def test_invalid_load_does_not_create_database(self) -> None:
        sample = copy_sample(self.root / "bad-sample")
        policy = read_json(sample / "policy.json")
        policy["budget"] = "not-a-number"
        write_json(sample / "policy.json", policy)
        db = self.root / "should-not-exist.db"
        completed, error = run_cli(
            "load",
            "--sample",
            str(sample),
            "--db",
            str(db),
            expected_code=2,
        )
        self.assertIn(
            error["error_code"],
            {"E_DIGEST_MISMATCH", "E_INVALID_NUMBER"},
        )
        self.assertFalse(db.exists())
        self.assertFalse(Path(str(db) + ".lock").exists())


if __name__ == "__main__":
    unittest.main()
