from __future__ import annotations

import copy
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from elasticctl.service import status, verify

from tests.common import ROOT, load_fixture, sample_payload


class ConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="elastic-concurrency-")
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _spawn(
        self,
        db_file: Path,
        *args: str,
        env: dict[str, str] | None = None,
    ) -> subprocess.Popen[str]:
        process_env = os.environ.copy()
        process_env.pop("ELASTICCTL_FAULT", None)
        if env:
            process_env.update(env)
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "elasticctl",
                *args,
                "--db",
                str(db_file),
            ],
            cwd=ROOT,
            env=process_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_two_processes_share_budget_without_oversell(self) -> None:
        db_file = self.root / "shared.db"
        budget = copy.deepcopy(sample_payload("resource_budget.json"))
        budget["resourceBudget"]["poolCoreHours"] = "0.0002"
        load_fixture(db_file, budget=budget)
        api = self._spawn(
            db_file,
            "dispatch",
            "--at",
            "120000",
            "--workload",
            "wl-api",
        )
        queue = self._spawn(
            db_file,
            "dispatch",
            "--at",
            "120000",
            "--workload",
            "wl-queue",
        )
        api_out, api_err = api.communicate(timeout=30)
        queue_out, queue_err = queue.communicate(timeout=30)
        self.assertEqual(api.returncode, 0, api_err)
        self.assertEqual(queue.returncode, 0, queue_err)
        snapshot = status(db_file)
        self.assertLessEqual(
            snapshot["budget"]["coreUsedUnits"],
            snapshot["budget"]["poolCoreUnits"],
        )
        self.assertEqual(snapshot["budget"]["coreUsedUnits"], 400000)
        self.assertTrue(verify(db_file)["ok"])

    def test_same_workload_round_is_decided_and_dispatched_once(self) -> None:
        db_file = self.root / "same.db"
        load_fixture(db_file)
        one = self._spawn(
            db_file,
            "dispatch",
            "--at",
            "120000",
            "--workload",
            "wl-api",
        )
        two = self._spawn(
            db_file,
            "dispatch",
            "--at",
            "120000",
            "--workload",
            "wl-api",
        )
        one_out, one_err = one.communicate(timeout=30)
        two_out, two_err = two.communicate(timeout=30)
        self.assertEqual(one.returncode, 0, one_err)
        self.assertEqual(two.returncode, 0, two_err)
        snapshot = status(db_file)
        api = next(
            workload
            for workload in snapshot["workloads"]
            if workload["id"] == "wl-api"
        )
        self.assertEqual(api["replicas"], 6)
        self.assertEqual(snapshot["budget"]["coreUsedUnits"], 400000)
        self.assertTrue(verify(db_file)["ok"])


if __name__ == "__main__":
    unittest.main()

