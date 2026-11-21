from __future__ import annotations

import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from igeval import EvaluatorConfig, QualityEvaluator, build_demo_samples
from igeval.snapshot import EvaluationSnapshot, evolve_snapshot


class ResourceAndConcurrencyTests(unittest.TestCase):
    def test_cache_entry_limit_is_explicit_and_preserves_scores(self) -> None:
        config = EvaluatorConfig(
            workers=1,
            latency_budget_ms=1_000.0,
            max_cache_entries=1,
            max_feature_memory_bytes=8 * 1024 * 1024,
        )
        with tempfile.TemporaryDirectory() as runtime:
            evaluator = QualityEvaluator(
                build_demo_samples(2), config=config, runtime_dir=runtime
            )
            first = evaluator.query("sample-001")
            second = evaluator.query("sample-002")
        self.assertEqual(first["status"], "ok")
        self.assertEqual(second["status"], "error")
        self.assertEqual(second["code"], "E_RESOURCE_LIMIT")
        self.assertEqual(
            second["degradation"][0]["category"], "CACHE_ENTRY_LIMIT"
        )
        self.assertIsNotNone(second["final_score"])

    def test_feature_memory_limit_is_explicit(self) -> None:
        config = EvaluatorConfig(
            workers=1,
            latency_budget_ms=1_000.0,
            max_feature_memory_bytes=1,
        )
        with tempfile.TemporaryDirectory() as runtime:
            evaluator = QualityEvaluator(
                build_demo_samples(1), config=config, runtime_dir=runtime
            )
            response = evaluator.query("sample-001")
        self.assertEqual(response["code"], "E_RESOURCE_LIMIT")
        self.assertEqual(
            response["degradation"][0]["category"], "FEATURE_MEMORY_LIMIT"
        )
        self.assertIsNotNone(response["final_score"])

    def test_concurrent_queries_do_not_mix_snapshot_components(self) -> None:
        config = EvaluatorConfig(workers=1, latency_budget_ms=1_000_000.0)
        samples = build_demo_samples(4)
        with tempfile.TemporaryDirectory() as runtime:
            evaluator = QualityEvaluator(samples, config=config, runtime_dir=runtime)
            current = EvaluationSnapshot.from_dict(
                evaluator.get_snapshot()["snapshot"]
            )
            newer = evolve_snapshot(
                current,
                snapshot_version=2,
                normalization_version=2,
                alignment_version=2,
                alignment_changes={"prompt_weight": 0.70, "reference_weight": 0.30},
            )
            start = threading.Event()

            def run_queries() -> list[dict]:
                start.wait(timeout=5)
                output = []
                for index in range(6):
                    sample = samples[index % len(samples)]
                    output.append(
                        evaluator.query(
                            sample.sample_id,
                            latency_budget_ms=1_000_000.0,
                            use_cache=False,
                        )
                    )
                return output

            with ThreadPoolExecutor(max_workers=8) as executor:
                futures = [executor.submit(run_queries) for _ in range(8)]
                start.set()
                time.sleep(0.01)
                switched = evaluator.switch_snapshot(newer)
                self.assertEqual(switched["status"], "ok")
                responses = [item for future in futures for item in future.result()]

            for response in responses:
                self.assertEqual(response["status"], "ok", response.get("errors"))
                components = response["snapshot"]["components"]
                version = response["snapshot"]["snapshot_version"]
                if version == 1:
                    self.assertEqual(components["normalization"]["version"], 1)
                    self.assertEqual(components["alignment"]["version"], 1)
                    self.assertEqual(components["structure"]["version"], 1)
                else:
                    self.assertEqual(version, 2)
                    self.assertEqual(components["normalization"]["version"], 2)
                    self.assertEqual(components["alignment"]["version"], 2)
                    self.assertEqual(components["structure"]["version"], 1)

            after = evaluator.query("sample-001", use_cache=False)
            self.assertEqual(after["snapshot"]["snapshot_version"], 2)
            batch = evaluator.evaluate_batch()
            self.assertEqual(batch["snapshot"]["snapshot_version"], 2)

    def test_batch_and_query_can_run_while_switching(self) -> None:
        config = EvaluatorConfig(workers=1, latency_budget_ms=1_000_000.0)
        samples = build_demo_samples(4)
        with tempfile.TemporaryDirectory() as runtime:
            evaluator = QualityEvaluator(samples, config=config, runtime_dir=runtime)
            current = EvaluationSnapshot.from_dict(
                evaluator.get_snapshot()["snapshot"]
            )
            newer = evolve_snapshot(
                current,
                snapshot_version=2,
                structure_version=2,
                structure_changes={"region_weight": 0.40},
            )
            with ThreadPoolExecutor(max_workers=2) as executor:
                batch_future = executor.submit(evaluator.evaluate_batch)
                query_future = executor.submit(
                    evaluator.query,
                    "sample-002",
                )
                switch = evaluator.switch_snapshot(newer)
                self.assertEqual(switch["status"], "ok")
                batch = batch_future.result()
                query = query_future.result()
            self.assertIn(batch["snapshot"]["snapshot_version"], {1, 2})
            self.assertIn(query["snapshot"]["snapshot_version"], {1, 2})
            self.assertEqual(len(batch["results"]), 4)
            self.assertEqual(query["status"], "ok")


if __name__ == "__main__":
    unittest.main()

