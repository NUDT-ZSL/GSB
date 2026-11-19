from __future__ import annotations

import json
import unittest

from sparse_precision.codes import E_RESOURCE_LIMIT
from sparse_precision.errors import SparseOperatorError
from sparse_precision.models import OperatorConfig
from sparse_precision.strategy import decide_strategy

from tests.helpers import metadata_for


class StrategyTests(unittest.TestCase):
    def test_strategy_is_byte_deterministic(self) -> None:
        metadata = metadata_for("spspmm_balanced_40")
        first = decide_strategy(metadata)
        second = decide_strategy(metadata)
        first_bytes = json.dumps(
            first, sort_keys=True, separators=(",", ":")
        ).encode()
        second_bytes = json.dumps(
            second, sort_keys=True, separators=(",", ":")
        ).encode()
        self.assertEqual(first_bytes, second_bytes)
        self.assertEqual(first["strategy_sha256"], second["strategy_sha256"])

    def test_coverage_and_cost_record(self) -> None:
        metadata = metadata_for("spspmm_batched_24")
        decision = decide_strategy(metadata)
        self.assertTrue(decision["coverage"]["all_rows_covered"])
        self.assertTrue(decision["coverage"]["all_k_covered"])
        self.assertGreaterEqual(
            decision["row_block_size"] * decision["row_blocks"],
            metadata["shape_mnk"][1],
        )
        self.assertGreaterEqual(
            decision["k_block_size"] * decision["k_blocks"], 64
        )
        self.assertIn("estimated_peak_bytes", decision)
        self.assertIn("partial_buffer_bytes", decision["cost_estimate"])
        self.assertIn("candidates_considered", decision)

    def test_aggregation_path_is_adaptive(self) -> None:
        decision = decide_strategy(metadata_for("spspmm_aggregated_32"))
        self.assertTrue(decision["use_aggregation_path"])
        self.assertGreater(decision["estimated_aggregation_bytes"], 0)

    def test_budget_downgrade_is_explicit(self) -> None:
        metadata = metadata_for("spspmm_aggregated_32")
        ideal = decide_strategy(metadata)
        config = OperatorConfig(
            max_blocks=8,
            max_memory_bytes=32 * 1024 * 1024,
            max_aggregation_bytes=1,
        )
        downgraded = decide_strategy(metadata, config=config)
        self.assertTrue(downgraded["degradation"]["degraded"])
        self.assertIn(
            downgraded["degradation"]["category"],
            {
                "larger_row_block",
                "smaller_row_block",
                "aggregation_disabled",
                "resource_replanned",
            },
        )
        self.assertNotEqual(
            ideal["row_block_size"],
            downgraded["row_block_size"],
        )
        self.assertLessEqual(downgraded["total_tiles"], 8)
        self.assertEqual(
            downgraded["degradation"]["semantics_unchanged"], True
        )
        self.assertEqual(
            downgraded["degradation"]["tolerance_unchanged"], True
        )

    def test_impossible_budget_returns_resource_error(self) -> None:
        metadata = metadata_for("spmm_dense_7")
        config = OperatorConfig(
            max_blocks=1,
            max_memory_bytes=1,
            max_aggregation_bytes=1,
        )
        with self.assertRaises(SparseOperatorError) as context:
            decide_strategy(metadata, config=config)
        self.assertEqual(context.exception.code, E_RESOURCE_LIMIT)
        self.assertEqual(context.exception.case_id, "spmm_dense_7")
        self.assertEqual(context.exception.phase, "strategy")

    def test_error_policy_rejects_feasible_downgrade(self) -> None:
        metadata = metadata_for("spspmm_aggregated_32")
        config = OperatorConfig(
            max_blocks=8,
            max_memory_bytes=32 * 1024 * 1024,
            max_aggregation_bytes=1,
            resource_policy="error",
        )
        with self.assertRaises(SparseOperatorError) as context:
            decide_strategy(metadata, config=config)
        self.assertEqual(context.exception.code, E_RESOURCE_LIMIT)
        self.assertEqual(context.exception.field, "resource_policy")
        self.assertIn("ideal_plan", context.exception.details)
        self.assertIn("selected_plan", context.exception.details)


if __name__ == "__main__":
    unittest.main()

