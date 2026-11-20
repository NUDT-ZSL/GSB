import unittest

from ocrbench.config import PipelineConfig, ResourceLimits
from ocrbench.errors import ErrorCode, OcrBenchError
from ocrbench.graph import validate_topology
from ocrbench.resource import (
    check_match_candidates,
    evaluate_residency,
)


class TopologyTest(unittest.TestCase):
    def test_cycle_is_rejected(self):
        with self.assertRaises(OcrBenchError) as ctx:
            validate_topology(["a", "b", "c"], [("a", "b"), ("b", "c"), ("c", "a")])
        self.assertEqual(ctx.exception.code, ErrorCode.GRAPH_CYCLE)
        self.assertEqual(ctx.exception.details["remaining_nodes"], ["a", "b", "c"])

    def test_unknown_node_edge_is_rejected(self):
        with self.assertRaises(OcrBenchError) as ctx:
            validate_topology(["a"], [("a", "ghost")])
        self.assertEqual(ctx.exception.code, ErrorCode.GRAPH_CONFIG)

    def test_default_graph_is_acyclic_and_ordered(self):
        order = validate_topology(
            ("preprocess", "detect", "features", "match", "recognize", "aggregate"),
            PipelineConfig().edges,
        )
        self.assertEqual(
            order,
            ["preprocess", "detect", "features", "match", "recognize", "aggregate"],
        )


class ResourceGuardTest(unittest.TestCase):
    def test_resident_memory_hard_limit(self):
        limits = ResourceLimits(
            streaming_feature_bytes=1,
            max_resident_feature_bytes=10,
        )
        with self.assertRaises(OcrBenchError) as ctx:
            evaluate_residency(line_count=4, cells_per_line=8, cell_pixels=35, limits=limits)
        self.assertEqual(ctx.exception.code, ErrorCode.RESOURCE_LIMIT)
        self.assertGreater(ctx.exception.details["estimated_bytes"], 10)

    def test_match_candidate_limit(self):
        limits = ResourceLimits(max_match_candidates=5)
        with self.assertRaises(OcrBenchError) as ctx:
            check_match_candidates(36, limits, "p1", 2)
        self.assertEqual(ctx.exception.code, ErrorCode.RESOURCE_LIMIT)
        self.assertEqual(ctx.exception.stage, "match")
        self.assertEqual(ctx.exception.details["line_index"], 2)

    def test_streaming_degradation_flag(self):
        limits = ResourceLimits(
            streaming_feature_bytes=100,
            max_resident_feature_bytes=10_000_000,
        )
        decision = evaluate_residency(2, 4, 35, limits)
        self.assertTrue(decision.degraded)
        self.assertEqual(decision.degradation_category, "streaming_line_recognition")


if __name__ == "__main__":
    unittest.main()
