from __future__ import annotations

import copy
import tempfile
import unittest

from igeval import EvaluatorConfig, QualityEvaluator, build_demo_samples, make_sample
from igeval.snapshot import EvaluationSnapshot, evolve_snapshot


class SnapshotAndCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = tempfile.TemporaryDirectory()
        self.addCleanup(self.runtime.cleanup)
        self.samples = build_demo_samples(4)
        self.config = EvaluatorConfig(workers=1, latency_budget_ms=1_000.0)
        self.evaluator = QualityEvaluator(
            self.samples,
            config=self.config,
            runtime_dir=self.runtime.name,
        )

    def test_snapshot_switch_is_atomic_for_new_queries_and_invalidates_all(self) -> None:
        first_batch = self.evaluator.evaluate_batch()
        self.assertEqual(first_batch["snapshot"]["snapshot_version"], 1)
        self.evaluator.query("sample-001")
        old = EvaluationSnapshot.from_dict(self.evaluator.get_snapshot()["snapshot"])
        new = evolve_snapshot(
            old,
            snapshot_version=2,
            normalization_version=2,
            normalization_changes={"mean": 0.60},
        )
        switched = self.evaluator.switch_snapshot(new)
        self.assertEqual(switched["status"], "ok")
        self.assertEqual(
            switched["invalidation"]["affected_sample_ids"],
            ["sample-001", "sample-002", "sample-003", "sample-004"],
        )
        event_report = self.evaluator.invalidation_report()
        self.assertFalse(event_report["coverage_ok"])
        self.assertEqual(
            event_report["events"][0]["pending_sample_ids"],
            ["sample-001", "sample-002", "sample-003", "sample-004"],
        )

        query = self.evaluator.query("sample-001")
        self.assertEqual(query["snapshot"]["snapshot_version"], 2)
        self.assertEqual(query["cache"]["status"], "stored")
        second_batch = self.evaluator.evaluate_batch()
        self.assertEqual(second_batch["snapshot"]["snapshot_version"], 2)
        self.assertTrue(self.evaluator.invalidation_report()["coverage_ok"])
        check = self.evaluator.self_check(second_batch)
        self.assertEqual(check["status"], "ok", check)

    def test_rollback_and_corruption_codes(self) -> None:
        current = EvaluationSnapshot.from_dict(self.evaluator.get_snapshot()["snapshot"])
        newer = evolve_snapshot(
            current,
            snapshot_version=2,
            structure_version=2,
            structure_changes={"edge_weight": 0.5},
        )
        self.assertEqual(self.evaluator.switch_snapshot(newer)["status"], "ok")
        rollback = self.evaluator.switch_snapshot(current)
        self.assertEqual(rollback["status"], "error")
        self.assertEqual(rollback["code"], "E_SNAPSHOT_ROLLBACK")

        corrupted = copy.deepcopy(self.evaluator.get_snapshot()["snapshot"])
        corrupted["components"]["normalization"]["mean"] = 0.77
        corrupt_result = self.evaluator.switch_snapshot(corrupted)
        self.assertEqual(corrupt_result["status"], "error")
        self.assertEqual(corrupt_result["code"], "E_SNAPSHOT_CORRUPT")
        self.assertEqual(
            corrupt_result["errors"][0]["field"],
            "components.normalization.checksum",
        )

    def test_snapshot_persists_and_reloads_current_version(self) -> None:
        current = EvaluationSnapshot.from_dict(self.evaluator.get_snapshot()["snapshot"])
        newer = evolve_snapshot(
            current,
            snapshot_version=2,
            normalization_version=2,
            normalization_changes={"mean": 0.61},
        )
        self.evaluator.switch_snapshot(newer)
        reloaded = QualityEvaluator(
            self.samples,
            config=self.config,
            runtime_dir=self.runtime.name,
        )
        snapshot = reloaded.get_snapshot()["snapshot"]
        self.assertEqual(snapshot["snapshot_version"], 2)
        self.assertEqual(snapshot["components"]["normalization"]["version"], 2)
        self.assertGreaterEqual(len(reloaded.get_snapshot()["files"]), 2)

    def test_prompt_update_and_append_invalidate_only_affected_samples(self) -> None:
        for sample_id in self.evaluator._all_sample_ids():
            self.evaluator.query(sample_id)
        old_entry = self.evaluator.cache_entry_report("sample-002")
        updated = self.evaluator.update_prompt(
            "sample-002",
            "a revised bright geometric poster with red circles",
        )
        self.assertEqual(updated["status"], "ok")
        self.assertEqual(updated["invalidation"]["affected_sample_ids"], ["sample-002"])
        self.assertEqual(
            updated["invalidation"]["invalidated_sample_ids"], ["sample-002"]
        )
        new_entry = self.evaluator.cache_entry_report("sample-002")
        self.assertNotEqual(old_entry["cache_key"], new_entry["cache_key"])
        query = self.evaluator.query("sample-002")
        self.assertEqual(query["cache"]["status"], "stored")
        self.assertEqual(
            query["sample"]["prompt"],
            "a revised bright geometric poster with red circles",
        )

        appended = self.evaluator.append_samples(
            [
                make_sample(
                    "sample-099",
                    9999,
                    "new green forest path",
                    "green foliage and light",
                    3,
                )
            ]
        )
        self.assertEqual(appended["status"], "ok")
        self.assertEqual(appended["invalidation"]["affected_sample_ids"], ["sample-099"])
        batch = self.evaluator.evaluate_batch()
        self.assertEqual(batch["summary"]["evaluated"], 5)
        invalidations = self.evaluator.invalidation_report()
        self.assertTrue(invalidations["coverage_ok"])
        for event in invalidations["events"]:
            hit = set(event["cache_hit_sample_ids"])
            recomputed = set(event["recomputed_sample_ids"])
            affected = set(event["affected_sample_ids"])
            self.assertFalse(hit & recomputed)
            self.assertEqual(hit | recomputed, affected)


if __name__ == "__main__":
    unittest.main()

