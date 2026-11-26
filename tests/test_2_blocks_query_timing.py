import unittest

from kfdiff.tradeoff import describe_blocks, estimate_first_block_ms, timing_observation
from tests.helpers import make_studio


class BlockPlanningTests(unittest.TestCase):
    def test_blocks_cover_all_ordinals(self):
        blocks = describe_blocks(11, 3)
        self.assertEqual([b["ordinals"] for b in blocks],
                         [[0, 1, 2], [3, 4, 5], [6, 7, 8], [9, 10]])
        self.assertTrue(blocks[0]["is_first"])
        self.assertTrue(blocks[-1]["is_last"])

    def test_estimate_contains_required_factors(self):
        studio, _ = make_studio()
        task = studio.tasks["clip_0001"]
        estimate = estimate_first_block_ms(task, 3, 24, studio.config)
        self.assertEqual(estimate["first_block_keyframes"], 3)
        self.assertGreater(estimate["pixel_step_units"], 0)
        self.assertGreaterEqual(estimate["coupling_links"], 1)
        self.assertIn("estimated_first_query_ms", estimate)


class QueryAndTimingTests(unittest.TestCase):
    def test_partial_results_queryable_between_blocks(self):
        studio, _ = make_studio()
        iterator = studio.generate_incremental("clip_0008")
        plan_event = next(iterator)
        self.assertEqual(plan_event["event"], "plan")
        next(iterator)  # first block
        partial = studio.query_ready("clip_0008")
        self.assertGreater(partial["ready_count"], 0)
        self.assertLess(partial["ready_count"], partial["total_keyframes"])
        self.assertFalse(partial["complete"])
        for event in iterator:
            pass
        final = studio.query_ready("clip_0008")
        self.assertTrue(final["complete"])
        kf = final["keyframes"][0]
        self.assertEqual(len(kf["value_interval"]), 2)
        self.assertIn("content_hash", kf)
        self.assertIn("block_byte_span", kf)

    def test_generation_records_real_observation(self):
        studio, _ = make_studio()
        result = studio.generate("clip_0001", parallel=False)
        timing = result["first_block_timing"]
        self.assertGreaterEqual(timing["observed_first_block_ms"], 0.0)
        self.assertIn("within_budget", timing)
        self.assertIn("estimate_error_ratio", timing)

    def test_timing_observation_is_deterministic_function(self):
        estimate = {"budget_ms": 10.0, "estimated_first_block_ms": 8.0}
        under = timing_observation(estimate, 9.0)
        self.assertTrue(under["within_budget"])
        over = timing_observation(estimate, 12.0)
        self.assertEqual(over["observed_first_block_ms"], 12.0)
        self.assertFalse(over["within_budget"])
        self.assertEqual(over["estimate_error_ms"], 4.0)


if __name__ == "__main__":
    unittest.main()
