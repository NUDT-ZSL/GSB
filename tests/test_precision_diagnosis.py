"""要求 3/4：双精度档同语义、顺序不变量与误差传播账本。"""

from __future__ import annotations

from sr_pipeline.diagnose import diagnose_query
from sr_pipeline.fusion import fuse
from sr_pipeline.index import build_index
from sr_pipeline.precision import (
    MODE_FIXED,
    MODE_FLOAT64,
    PrecisionConfig,
    build_ops,
)
from sr_pipeline.recall import run_all_routes

from ._base import SampleCase


class PrecisionDiagnosisTest(SampleCase):
    def _fuse_all(self, precision: PrecisionConfig):
        config = self.config.with_precision(precision)
        ops = build_ops(precision)
        index = build_index(
            self.docs, self.queries, self.term_weights, config, ops
        )
        results = {}
        for query in self.queries:
            route_results = run_all_routes(query, index, config, ops)
            results[query["query_id"]] = fuse(
                query, route_results, config, ops
            )
        return config, results

    def test_fixed_ops_quantize_and_arithmetic(self) -> None:
        ops = build_ops(
            PrecisionConfig(mode=MODE_FIXED, scale=1000, rounding="half_even")
        )
        self.assertEqual(ops.from_float(0.5), 500)
        # 0.0005 在二进制浮点下略小于 0.5 个量化步，向下舍入
        self.assertEqual(ops.from_float(0.0005), 0)
        self.assertEqual(ops.from_float(0.0015), 2)  # 奇数步长，银行家舍入向上
        # 0.2 * 0.3 在 1000 缩放下
        product = ops.mul(ops.from_float(0.2), ops.from_float(0.3))
        self.assertEqual(product, 60)
        self.assertAlmostEqual(ops.to_float(product), 0.06)
        # sqrt(0.25) = 0.5
        root = ops.sqrt(ops.from_float(0.25))
        self.assertEqual(root, 500)

    def test_rounding_modes_distinct(self) -> None:
        for rounding, expected in (
            ("half_up", 2),
            ("half_down", 1),
            ("truncate", 1),
            ("half_even", 2),
        ):
            ops = build_ops(
                PrecisionConfig(mode=MODE_FIXED, scale=10, rounding=rounding)
            )
            self.assertEqual(ops.from_float(0.15), expected)

    def test_order_and_dedup_invariant_across_modes(self) -> None:
        _, float_results = self._fuse_all(
            PrecisionConfig(mode=MODE_FLOAT64)
        )
        fixed_config, fixed_results = self._fuse_all(
            PrecisionConfig(
                mode=MODE_FIXED,
                scale=self.config.precision.scale,
                rounding=self.config.precision.rounding,
            )
        )
        for query_id, reference in float_results.items():
            actual = fixed_results[query_id]
            self.assertEqual(
                [c.doc_id for c in reference.candidates],
                [c.doc_id for c in actual.candidates],
            )
            self.assertEqual(
                {c.doc_id: c.owner_route for c in reference.candidates},
                {c.doc_id: c.owner_route for c in actual.candidates},
            )
            # 只允许分数出现数值差异，且差异有界
            for ref_candidate, fixed_candidate in zip(
                reference.candidates, actual.candidates
            ):
                self.assertLess(
                    abs(
                        ref_candidate.final_score_float
                        - fixed_candidate.final_score_float
                    ),
                    1e-4,
                )

    def test_error_ledger_levels_and_amplification(self) -> None:
        _, float_results = self._fuse_all(
            PrecisionConfig(mode=MODE_FLOAT64)
        )
        fixed_config, fixed_results = self._fuse_all(
            PrecisionConfig(mode=MODE_FIXED, scale=1_000_000)
        )
        query_id = self.queries[0]["query_id"]
        diagnosis = diagnose_query(
            query_id,
            float_results[query_id],
            fixed_results[query_id],
            fixed_config,
        )
        self.assertTrue(diagnosis.ordering_preserved)
        self.assertTrue(diagnosis.dedup_preserved)
        levels = {level.level for level in diagnosis.levels}
        self.assertEqual(
            levels, {"raw", "normalized", "weighted", "final"}
        )
        for level in diagnosis.levels:
            self.assertGreaterEqual(level.abs_error, 0.0)
            self.assertGreaterEqual(level.rel_error, 0.0)
        self.assertTrue(diagnosis.dominant_sources)
        self.assertIn("stable", diagnosis.threshold)
        self.assertIn("guard_band", diagnosis.threshold)
