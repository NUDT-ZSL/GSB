from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from capacity_engine.engine import Engine
from capacity_engine.scene import parse_scene

from tests.helpers import mock_server, write_json


def _minimal_scene(port: int, budget: int, limit: int, gradients: list[int]) -> dict:
    return {
        "scene_id": "limit-test",
        "services": [
            {
                "name": "svc",
                "address": f"http://127.0.0.1:{port}",
                "success_status_codes": [200],
                "tenants": {
                    "tenant": {
                        "success_rate_min": 0.9,
                        "p95_latency_max_ms": 100,
                        "concurrency_limit": limit,
                    }
                },
            }
        ],
        "batches": [
            {
                "batch_id": "batch",
                "tenant": "tenant",
                "concurrency_gradients": gradients,
                "duration_logical_ms": 20,
                "request_pace_ms": 5,
                "allow_early_termination": False,
            }
        ],
        "clock": {"start_ms": 0, "window_ms": 5, "step_table": [5]},
        "precision": {
            "latency_scale": 1,
            "percent_scale": 10000,
            "compare_tolerance": {"success_rate": 0.05, "p95_latency_ms": 2},
            "degraded_margin": {"success_rate": 0.005, "p95_latency_ms": 0.1},
            "error_budget": {"success_rate": 0.001, "p95_latency_ms": 2},
            "request_logical_budget_ms": 100,
        },
        "budget": {"global_logical_time_ms": budget},
    }


class BudgetAndLimitTests(unittest.TestCase):
    def test_global_budget_exhaustion_abandons_remaining_units(self) -> None:
        with mock_server() as port, tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            scene_path = write_json(
                base / "budget.json",
                _minimal_scene(port, budget=30, limit=4, gradients=[1, 2]),
            )
            scene = parse_scene(json.loads(scene_path.read_text(encoding="utf-8")))
            result = Engine(scene, base / "budget.sqlite").run()
            self.assertEqual(result["budget_used_ms"], 20)
            statuses = result["report"]["unit_status_counts"]
            self.assertEqual(statuses.get("COMPLETED"), 1)
            self.assertEqual(statuses.get("ABANDONED"), 1)
            abandoned = result["report"]["abandoned_units"]
            self.assertEqual(abandoned[0]["reason"], "GLOBAL_LOGICAL_BUDGET_EXHAUSTED")

    def test_tenant_concurrency_limit_exhaustion_is_recorded(self) -> None:
        with mock_server() as port, tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            scene_path = write_json(
                base / "limit.json",
                _minimal_scene(port, budget=1000, limit=2, gradients=[4]),
            )
            scene = parse_scene(json.loads(scene_path.read_text(encoding="utf-8")))
            result = Engine(scene, base / "limit.sqlite").run()
            self.assertEqual(result["budget_used_ms"], 0)
            abandoned = result["report"]["abandoned_units"]
            self.assertEqual(len(abandoned), 1)
            self.assertEqual(abandoned[0]["reason"], "TENANT_CONCURRENCY_LIMIT_EXHAUSTED")

    def test_early_termination_skips_higher_gradients(self) -> None:
        with mock_server() as port, tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            document = _minimal_scene(port, budget=1000, limit=4, gradients=[1, 2, 4])
            document["batches"][0]["allow_early_termination"] = True
            scene_path = write_json(base / "early.json", document)
            scene = parse_scene(json.loads(scene_path.read_text(encoding="utf-8")))
            result = Engine(scene, base / "early.sqlite").run()
            statuses = {
                item["concurrency"]: item["status"]
                for item in result["report"]["gradients"]
            }
            self.assertEqual(statuses[1], "ABORT")
            self.assertEqual(statuses[2], "SKIPPED_EARLY_TERMINATION")
            self.assertEqual(statuses[4], "SKIPPED_EARLY_TERMINATION")


if __name__ == "__main__":
    unittest.main()

