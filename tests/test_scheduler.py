from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests.helpers import SAMPLES, json_output, load_default, run_cli


class SchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "state.sqlite3"
        load_default(self.db)
        run_cli("build", "--plan-version", "p1", db=self.db, check=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_budget_concurrency_and_non_timeout_abandon(self) -> None:
        process = run_cli(
            "schedule",
            "--plan-version",
            "p1",
            "--budget",
            "90000",
            "--max-concurrency",
            "2",
            "--rounds",
            "2",
            "--start-clock",
            "0",
            "--round-span-ms",
            "100",
            db=self.db,
            check=True,
        )
        result = json_output(process)["schedule"]
        for round_data in result["rounds"]:
            self.assertLessEqual(round_data["slots_used"], 2)
            self.assertLessEqual(round_data["used_budget"], round_data["budget"])
        abandoned = [
            item for item in result["items"] if item["status"] == "abandoned_budget"
        ]
        self.assertTrue(abandoned)
        for item in abandoned:
            self.assertEqual("round_budget_exhausted", item["reason"])
            self.assertNotIn("timeout", item["reason"])
            self.assertFalse(item["started"])

    def test_deadline_overflow_lists_chain_and_impact(self) -> None:
        process = run_cli(
            "schedule",
            "--plan-version",
            "p1",
            "--budget",
            "90000",
            "--max-concurrency",
            "3",
            "--rounds",
            "4",
            "--start-clock",
            "0",
            "--round-span-ms",
            "100",
            db=self.db,
            check=True,
        )
        result = json_output(process)["schedule"]
        overflow = [
            item
            for item in result["arbitrations"]
            if item["status"] == "deadline_overflow"
        ]
        self.assertTrue(overflow)
        self.assertTrue(overflow[0]["overflow_chain"])
        self.assertTrue(overflow[0]["affected_jobs"])
        self.assertEqual(
            sorted(item["deadline_clock_ms"] for item in result["arbitrations"]),
            [item["deadline_clock_ms"] for item in result["arbitrations"]],
        )

    def test_injected_deadlock_is_visible_and_never_silently_dropped(self) -> None:
        run_cli(
            "inject",
            "--plan-version",
            "p1",
            "--drift-count",
            "0",
            "--anomaly-count",
            "2",
            db=self.db,
            check=True,
        )
        process = run_cli(
            "schedule",
            "--plan-version",
            "p1",
            "--budget",
            "90000",
            "--max-concurrency",
            "3",
            "--rounds",
            "4",
            db=self.db,
            check=True,
        )
        result = json_output(process)["schedule"]
        blocked = [
            item for item in result["items"] if item["status"] == "blocked_anomaly"
        ]
        self.assertTrue(blocked)
        self.assertTrue(result["anomalies"])
        for item in blocked:
            self.assertIn(item["reason"], ("deadlock", "resource_overrun"))
            self.assertFalse(item["started"])

    def test_status_is_stable_and_contains_required_sections(self) -> None:
        first = run_cli("status", "--plan-version", "p1", db=self.db, check=True)
        second = run_cli("status", "--plan-version", "p1", db=self.db, check=True)
        self.assertEqual(first.stdout, second.stdout)
        status = json_output(first)
        self.assertTrue(status["tasks"])
        for task in status["tasks"]:
            self.assertIn("execution_ledger", task)
            self.assertIn("arbitration_decisions", task)
            self.assertIn("invalidation_closures", task)

    def test_all_resource_over_cap_nodes_are_marked(self) -> None:
        plan = json.loads((SAMPLES / "plans_v1.json").read_text())
        plan["plan_version"] = "over-cap"
        for job in plan["jobs"].values():
            job["threads_required"] = 32
        plan_path = Path(self.temp.name) / "over_cap.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        run_cli("load", "--plan", str(plan_path), db=self.db, check=True)
        run_cli("build", "--plan-version", "over-cap", db=self.db, check=True)
        process = run_cli(
            "schedule",
            "--plan-version",
            "over-cap",
            "--budget",
            "1",
            "--max-concurrency",
            "1",
            "--rounds",
            "1",
            db=self.db,
            check=True,
        )
        result = json_output(process)["schedule"]
        self.assertEqual(
            17,
            sum(1 for item in result["items"] if item["status"] == "blocked_anomaly"),
        )
        self.assertEqual(17, len(result["anomalies"]))


if __name__ == "__main__":
    unittest.main()

