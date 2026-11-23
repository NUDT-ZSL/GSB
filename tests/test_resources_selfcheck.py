from __future__ import annotations

import unittest

from spkvc.config import PipelineConfig, ResourceLimits
from spkvc.errors import BusinessError

from .support import DatasetTestCase


class ResourceAndSelfCheckTests(DatasetTestCase):
    def test_recompute_utterance_limit_returns_resource_limit(self):
        config = PipelineConfig(
            resource_limits=ResourceLimits(max_recompute_utterances=1)
        )
        engine = self.engine("resource-recompute", config=config)
        engine.run()
        with self.assertRaises(BusinessError) as caught:
            engine.incremental(ref_revisions={"ref-spk-02": 2})
        self.assertEqual(caught.exception.code, "E_RESOURCE_LIMIT")
        self.assertEqual(
            caught.exception.field, "max_recompute_utterances"
        )
        self.assertEqual(
            caught.exception.details["affected_utterance_count"], 2
        )

    def test_resident_and_parallel_limits_degrade_with_categories(self):
        config = PipelineConfig(
            parallel_nodes=4,
            resource_limits=ResourceLimits(
                max_resident_embeddings=2,
                max_parallel_nodes=1,
            ),
        )
        engine = self.engine("resource-degrade", config=config)
        result = engine.run()
        self.assertIn("embedding_cache_lru", result.degradations)
        self.assertIn("parallel_nodes_capped", result.degradations)
        self.assertTrue(engine.self_check(result)["ok"])

    def test_invalid_resource_limit_is_rejected(self):
        config = PipelineConfig(
            resource_limits=ResourceLimits(max_parallel_nodes=0)
        )
        with self.assertRaises(BusinessError) as caught:
            config.validate()
        self.assertEqual(caught.exception.code, "E_RESOURCE_LIMIT")
        self.assertEqual(caught.exception.field, "max_parallel_nodes")

    def test_self_check_rejects_overlapping_recompute_and_reuse(self):
        engine = self.engine("selfcheck-overlap")
        result = engine.run()
        result.recomputed_nodes = [result.reused_nodes[0]]
        report = engine.self_check(result)
        self.assertFalse(report["ok"])
        disjoint = next(
            check
            for check in report["checks"]
            if check["name"] == "recompute_reuse_disjoint"
        )
        self.assertFalse(disjoint["ok"])

    def test_truth_observation_metrics_are_isolated_and_consistent(self):
        engine = self.engine("evaluation")
        engine.run()
        metrics = engine.evaluate(["utt-001"])
        self.assertTrue(metrics["frame_count_consistent"])
        self.assertEqual(set(metrics["utterances"]), {"utt-001"})
        row = metrics["utterances"]["utt-001"]
        self.assertGreaterEqual(row["f0_mae_hz"], 0.0)
        self.assertGreaterEqual(row["energy_mae"], 0.0)
        self.assertGreaterEqual(row["alignment_accuracy"], 0.0)


if __name__ == "__main__":
    unittest.main()

