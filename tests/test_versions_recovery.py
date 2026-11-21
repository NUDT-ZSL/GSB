from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from drillbench.errors import BusinessError
from drillbench.store import StateStore
from drillbench.validation import load_snapshot

from .helpers import (
    ROOT,
    SAMPLES,
    cli,
    load_json,
    make_route,
    make_step,
    make_window,
    minimal_documents,
    write_documents,
    write_json,
)


class VersionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = StateStore(self.root / "state.db")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _load(self, sample_dir: Path):
        snapshot = load_snapshot(
            sample_dir / "plans.json",
            sample_dir / "services.json",
            sample_dir / "clock.json",
            sample_dir / "injections.json",
            plan_name="payment-failover",
        )
        return self.store.install_snapshot(snapshot)

    def test_noop_full_replacement_and_rollback(self) -> None:
        first = self._load(SAMPLES)
        self.assertEqual(first["change_kind"], "FULL_REPLACEMENT")
        self.assertEqual(first["version"], 1)
        self.assertEqual(first["comparison"]["injection_relation"], "INITIAL")
        noop = self._load(SAMPLES)
        self.assertEqual(noop["change_kind"], "NOOP")
        self.assertFalse(noop["comparison"]["content_digest_changed"])
        self.assertEqual(
            noop["comparison"]["injection_relation"], "UNCHANGED"
        )
        self.assertEqual(self.store.current_version(), 1)
        replaced = self._load(SAMPLES / "v2")
        self.assertEqual(replaced["change_kind"], "FULL_REPLACEMENT")
        self.assertEqual(replaced["version"], 2)
        self.assertEqual(replaced["comparison"]["added_steps"], [10])
        self.assertEqual(self.store.current_version(), 2)
        with self.assertRaises(BusinessError) as caught:
            snapshot = load_snapshot(
                SAMPLES / "plans.json",
                SAMPLES / "services.json",
                SAMPLES / "clock.json",
                SAMPLES / "injections.json",
                plan_name="payment-failover",
            )
            self.store.install_snapshot(snapshot, requested_version=1)
        self.assertEqual(caught.exception.code, "E_VERSION_ROLLBACK")
        with self.assertRaises(BusinessError) as implicit:
            snapshot = load_snapshot(
                SAMPLES / "plans.json",
                SAMPLES / "services.json",
                SAMPLES / "clock.json",
                SAMPLES / "injections.json",
                plan_name="payment-failover",
            )
            self.store.install_snapshot(snapshot)
        self.assertEqual(implicit.exception.code, "E_VERSION_ROLLBACK")
        old_info, _ = self.store.get_snapshot(1)
        new_info, _ = self.store.get_snapshot(2)
        self.assertNotEqual(old_info["content_digest"], new_info["content_digest"])
        self.assertEqual(self.store.current_version(), 2)

    def test_incremental_injection_append_is_classified(self) -> None:
        self._load(SAMPLES)
        injections = load_json(SAMPLES / "injections.json")
        injections["plans"][0]["injections"].append(
            {
                "id": "inj-report-reject",
                "step": 9,
                "at_ms": 3500,
                "type": "service_reject",
                "description": "newly appended report rejection",
            }
        )
        injection_path = self.root / "append" / "injections.json"
        write_json(injection_path, injections)
        snapshot = load_snapshot(
            SAMPLES / "plans.json",
            SAMPLES / "services.json",
            SAMPLES / "clock.json",
            injection_path,
            plan_name="payment-failover",
        )
        result = self.store.install_snapshot(snapshot)
        self.assertEqual(result["change_kind"], "INCREMENTAL_APPEND")
        self.assertEqual(result["version"], 2)
        self.assertEqual(
            result["comparison"]["injection_relation"], "APPEND"
        )
        self.assertEqual(
            result["comparison"]["added_injections"],
            ["inj-report-reject"],
        )


class RecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _load(self, state: Path) -> dict:
        code, payload, _, stderr = cli(
            "load",
            "--db",
            str(state / "state.db"),
            "--sample-dir",
            str(SAMPLES),
            "--plan",
            "payment-failover",
        )
        self.assertEqual(code, 0, stderr)
        self.assertTrue(payload["ok"])
        return payload

    def _run_crash(self, state: Path) -> int:
        code, _, _, _ = cli(
            "run",
            "--db",
            str(state / "state.db"),
            "--run-id",
            "demo",
            "--plan",
            "payment-failover",
            "--skip-step",
            "4",
            "--crash-at-step",
            "2",
            "--crash-phase",
            "after-response",
            expect_json=False,
        )
        return code

    def _resume(self, state: Path) -> dict:
        code, payload, _, stderr = cli(
            "resume",
            "--db",
            str(state / "state.db"),
            "--run-id",
            "demo",
            "--plan",
            "payment-failover",
            "--skip-step",
            "4",
        )
        self.assertEqual(code, 0, stderr)
        return payload

    def test_crash_resume_matches_clean_run(self) -> None:
        crashed = self.root / "crashed"
        clean = self.root / "clean"
        crashed.mkdir()
        clean.mkdir()
        self._load(crashed)
        self._load(clean)
        self.assertEqual(self._run_crash(crashed), 137)

        code, during, _, _ = cli(
            "status",
            "--db",
            str(crashed / "state.db"),
            "--run-id",
            "demo",
        )
        self.assertEqual(code, 0)
        self.assertEqual(during["run"]["state"], "RUNNING")
        running = [
            item for item in during["steps"] if item["stage"] == "RUNNING"
        ]
        self.assertEqual([item["seq"] for item in running], [2])
        self.assertIsNone(running[0]["observation"])

        resume_result = self._resume(crashed)
        self.assertEqual(
            resume_result["recovery"]["half_committed_steps"], [2]
        )
        code, _, _, stderr = cli(
            "run",
            "--db",
            str(clean / "state.db"),
            "--run-id",
            "demo",
            "--plan",
            "payment-failover",
            "--skip-step",
            "4",
        )
        self.assertEqual(code, 0, stderr)

        commands = [
            ("status",),
            ("verify",),
            ("inject",),
        ]
        for command in commands:
            extra = ("--samples-dir", str(SAMPLES)) if command[0] == "verify" else ()
            code_a, payload_a, _, _ = cli(
                *command,
                "--db",
                str(crashed / "state.db"),
                "--run-id",
                "demo",
                *extra,
            )
            code_b, payload_b, _, _ = cli(
                *command,
                "--db",
                str(clean / "state.db"),
                "--run-id",
                "demo",
                *extra,
            )
            self.assertEqual(code_a, 0)
            self.assertEqual(code_b, 0)
            self.assertEqual(payload_a, payload_b, command[0])

        store = StateStore(crashed / "state.db")
        with store.connect() as connection:
            count_row = connection.execute(
                """
                SELECT injection_id, COUNT(*) AS c
                FROM injection_consumptions
                GROUP BY injection_id
                """
            ).fetchall()
        self.assertTrue(count_row)
        self.assertTrue(all(int(row["c"]) == 1 for row in count_row))

    def test_half_commit_remains_bound_to_old_version(self) -> None:
        state = self.root / "bound"
        state.mkdir()
        self._load(state)
        self.assertEqual(self._run_crash(state), 137)
        code, payload, _, stderr = cli(
            "load",
            "--db",
            str(state / "state.db"),
            "--sample-dir",
            str(SAMPLES / "v2"),
            "--plan",
            "payment-failover",
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(payload["version"], 2)
        resumed = self._resume(state)
        self.assertEqual(resumed["version"], 1)
        code, status, _, _ = cli(
            "status",
            "--db",
            str(state / "state.db"),
            "--run-id",
            "demo",
        )
        self.assertEqual(code, 0)
        self.assertEqual(status["run"]["version"], 1)
        self.assertEqual(status["run"]["current_snapshot_version"], 2)
        self.assertFalse(status["run"]["bound_version_is_current"])
        self.assertEqual(status["run"]["state"], "BLOCKED")


class ConcurrentBranchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sample = self.root / "sample"
        steps = [make_step(1), make_step(2)]
        routes = [make_route(1), make_route(2)]
        windows = [make_window(1, 0, 400), make_window(2, 500, 400)]
        documents = minimal_documents(
            steps=steps,
            routes=routes,
            windows=windows,
        )
        paths = write_documents(self.sample, documents)
        code, payload, _, stderr = cli(
            "load",
            "--db",
            str(self.root / "state.db"),
            "--plans",
            str(paths[0]),
            "--services",
            str(paths[1]),
            "--clock",
            str(paths[2]),
            "--injections",
            str(paths[3]),
            "--plan",
            "fixture",
        )
        self.assertEqual(code, 0, stderr)
        self.assertTrue(payload["ok"])

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_parallel_processes_claim_distinct_ready_branches(self) -> None:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT)
        command = [
            sys.executable,
            "-m",
            "drillbench",
            "run",
            "--db",
            str(self.root / "state.db"),
            "--run-id",
            "parallel",
            "--plan",
            "fixture",
            "--once",
        ]
        first = subprocess.Popen(
            command,
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        second = subprocess.Popen(
            command,
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        out_a, err_a = first.communicate(timeout=15)
        out_b, err_b = second.communicate(timeout=15)
        self.assertEqual(first.returncode, 0, f"{out_a}\n{err_a}")
        self.assertEqual(second.returncode, 0, f"{out_b}\n{err_b}")

        code, status, _, stderr = cli(
            "status",
            "--db",
            str(self.root / "state.db"),
            "--run-id",
            "parallel",
        )
        self.assertEqual(code, 0, stderr)
        observed = [
            item["seq"]
            for item in status["steps"]
            if item["stage"] == "OBSERVED"
        ]
        self.assertEqual(observed, [1, 2])
        self.assertEqual(status["run"]["state"], "COMPLETED")

        code, final_run, _, _ = cli(
            "run",
            "--db",
            str(self.root / "state.db"),
            "--run-id",
            "parallel",
            "--plan",
            "fixture",
        )
        self.assertEqual(code, 0)
        self.assertEqual(final_run["executed_steps"], [])


if __name__ == "__main__":
    unittest.main()

