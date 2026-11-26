from __future__ import annotations

import os
import subprocess
import sys
import unittest

from engine_diag.cost import (
    evaluate_candidates,
    generate_drift_events,
)
from engine_diag.models import (
    parse_plan_file,
    parse_platform_stats_file,
    parse_tasks_file,
)
from engine_diag.subtasks import generate_plan_chains, plan_subtask_summary
from tests.helpers import ROOT, SAMPLES


class DeterminismCostTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = parse_plan_file(SAMPLES / "plans_v1.json")
        self.tasks = {
            task.job_id: task
            for task in parse_tasks_file(SAMPLES / "tasks_v1.jsonl")
        }
        self.stats = parse_platform_stats_file(SAMPLES / "platform_stats.json")
        self.chains = generate_plan_chains(self.plan, self.tasks)

    def test_subtask_summary_is_stable_across_hash_seeds(self) -> None:
        summaries = []
        expected = plan_subtask_summary(self.chains)
        for hash_seed in ("0", "1", "random"):
            env = os.environ.copy()
            env["PYTHONHASHSEED"] = hash_seed
            env["PYTHONPATH"] = str(ROOT)
            process = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "from engine_diag.models import parse_plan_file,"
                        "parse_tasks_file;"
                        "from engine_diag.subtasks import generate_plan_chains,"
                        "plan_subtask_summary;"
                        "p=parse_plan_file('samples/plans_v1.json');"
                        "t={x.job_id:x for x in parse_tasks_file("
                        "'samples/tasks_v1.jsonl')};"
                        "print(plan_subtask_summary(generate_plan_chains(p,t)))"
                    ),
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )
            summaries.append(process.stdout.strip())
        self.assertEqual([expected, expected, expected], summaries)

    def test_cost_evaluation_is_byte_for_byte_stable(self) -> None:
        first = evaluate_candidates(
            plan=self.plan,
            chains=self.chains,
            platform=self.stats,
        )
        second = evaluate_candidates(
            plan=self.plan,
            chains=self.chains,
            platform=self.stats,
        )
        self.assertEqual(first["result_digest"], second["result_digest"])
        self.assertEqual(3, len(first["candidates"]))
        for result in first["candidates"].values():
            indexes = [step["aggregation_index"] for step in result["steps"]]
            self.assertEqual(list(range(len(indexes))), indexes)
            self.assertEqual(
                result["aggregation_order"],
                [step["subtask_id"] for step in result["steps"]],
            )
            self.assertGreaterEqual(result["total_cost"], 0)
            self.assertGreater(result["cpu_instructions"], 0)
            self.assertGreater(result["memory_bytes"], 0)

    def test_drift_truth_uses_same_logical_clocks(self) -> None:
        clean = evaluate_candidates(
            plan=self.plan,
            chains=self.chains,
            platform=self.stats,
        )
        events = generate_drift_events(
            plan=self.plan,
            chains=self.chains,
            seed=777,
            count=6,
            anomaly_count=1,
            duration_steps=3,
        )
        drifted = evaluate_candidates(
            plan=self.plan,
            chains=self.chains,
            platform=self.stats,
            events=events,
        )
        event_clocks = {
            event.logical_clock_ms for event in events if event.anomaly_type is None
        }
        seen_clocks = {
            step["logical_clock_ms"]
            for result in drifted["candidates"].values()
            for step in result["steps"]
            if step["drift_events"]
        }
        self.assertTrue(event_clocks)
        self.assertTrue(event_clocks.issubset(seen_clocks))
        for candidate_id, result in drifted["candidates"].items():
            self.assertGreater(
                result["total_cost"],
                clean["candidates"][candidate_id]["total_cost"],
            )
            self.assertGreater(result["drift_delta"], 0)


if __name__ == "__main__":
    unittest.main()

