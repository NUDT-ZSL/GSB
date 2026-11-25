from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from ragdiag.arbitration import arbitrate
from ragdiag.formats import load_repository
from ragdiag.inputs import load_requests, load_schedule
from ragdiag.planning import build_plan
from ragdiag.retrieval import load_corpus
from ragdiag.sample_generator import generate
from ragdiag.scheduler import admit_requests


class SchedulerBudgetTests(unittest.TestCase):
    def test_three_budget_codes_and_deterministic_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            generate(data)
            repository = load_repository(data / "tool_results.json")
            requests = load_requests(data / "requests.jsonl")
            base_config = load_schedule(data / "schedule.json")
            documents = load_corpus(data / "evidence_corpus.jsonl")
            config, _ = arbitrate(repository, requests, base_config)
            plan = build_plan(repository, documents, requests, config)
            first = admit_requests(repository, requests, plan, config)
            second = admit_requests(repository, requests, plan, config)
            self.assertEqual(first["chunks"], second["chunks"])
            self.assertEqual(
                first["decisions"], second["decisions"]
            )
            codes = {
                decision["unit_id"]: decision["business_code"]
                for decision in first["decisions"]
            }
            self.assertEqual(codes["conv-004@1"], "E_CONCURRENCY_BUDGET")
            self.assertEqual(codes["conv-005@1"], "E_TIME_BUDGET")
            self.assertEqual(codes["conv-006@1"], "E_MEMORY_BUDGET")
            self.assertEqual(codes["conv-001@1"], "E_OK")
            self.assertEqual(len(first["admitted_units"]), 6)
            self.assertEqual(
                first["logical_time_used_ms"],
                second["logical_time_used_ms"],
            )

    def test_priority_order_changes_arbitration_winner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            generate(data)
            repository = load_repository(data / "tool_results.json")
            requests = load_requests(data / "requests.jsonl")
            base_config = load_schedule(data / "schedule.json")
            memory_first = deepcopy(base_config)
            memory_first["arbitration"]["priority_order"] = [
                "memory",
                "quality",
                "latency",
            ]
            _, memory_log = arbitrate(
                repository, requests, memory_first
            )
            self.assertEqual(
                memory_log["selected_profile"], "compact"
            )
            latency_first = deepcopy(base_config)
            latency_first["arbitration"]["priority_order"] = [
                "latency",
                "quality",
                "memory",
            ]
            _, latency_log = arbitrate(
                repository, requests, latency_first
            )
            self.assertEqual(
                latency_log["selected_profile"], "expanded"
            )


if __name__ == "__main__":
    unittest.main()

