from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from gemm_ops.config import (
    DEFAULT_INJECTIONS,
    load_cost_model,
    load_runtime,
    load_strategy_table,
)
from gemm_ops.engine import AdaptiveEngine
from gemm_ops.errors import GemmError
from gemm_ops.generator import generate_case
from gemm_ops.kernels import bmm, bmm_reference, execute_bmm, gemm, gemm_reference
from gemm_ops.precision import error_summary
from gemm_ops.reporting import atomic_write_json
from gemm_ops.runner import _run_injections, run_suite
from gemm_ops.selector import StrategySelector


ROOT = Path(__file__).resolve().parent.parent


def make_selector() -> StrategySelector:
    table, strategies = load_strategy_table()
    _, cost = load_cost_model()
    _, runtime = load_runtime()
    return StrategySelector(
        strategies,
        cost,
        strategy_table_raw=table,
        max_tile_elements=runtime.max_tile_elements,
        max_tile_dimension=runtime.max_tile_dimension,
    )


class GeneratorTests(unittest.TestCase):
    def test_generation_is_reproducible_and_places_special_values(self) -> None:
        spec = {
            "shape_id": "unit_gen",
            "M": 9,
            "N": 7,
            "K": 5,
            "batch": 3,
        }
        first = generate_case(spec, 12345, category="small")
        second = generate_case(spec, 12345, category="small")
        self.assertTrue(np.array_equal(first.A, second.A))
        self.assertTrue(np.array_equal(first.B, second.B))
        self.assertTrue(np.array_equal(first.C, second.C))
        self.assertEqual(first.seed, second.seed)
        self.assertEqual(first.special_values, second.special_values)
        self.assertTrue(np.isfinite(first.A).all())
        self.assertTrue(np.isfinite(first.B).all())
        self.assertTrue(np.isfinite(first.C).all())
        labels = {event["matrix_label"] for event in first.special_values}
        kinds = {event["kind"] for event in first.special_values}
        self.assertEqual(labels, {"A", "B", "C"})
        self.assertEqual(kinds, {"zero", "subnormal", "large_finite"})


class KernelTests(unittest.TestCase):
    def setUp(self) -> None:
        rng = np.random.default_rng(2027)
        self.A = rng.standard_normal((17, 13), dtype=np.float32)
        self.B = rng.standard_normal((13, 19), dtype=np.float32)
        self.C = rng.standard_normal((17, 19), dtype=np.float32)
        self.alpha = 1.25
        self.beta = -0.5
        self.reference = gemm_reference(
            self.A, self.B, self.C, alpha=self.alpha, beta=self.beta
        )

    def test_tiled_loop_orders_match_reference_with_boundary_crops(self) -> None:
        basic = gemm(self.A, self.B, self.C, self.alpha, self.beta)
        self.assertTrue(np.allclose(basic.astype(np.float64), self.reference, rtol=2e-3, atol=2e-3))
        for loop_order in ("ijk", "ikj", "jik"):
            result = gemm(
                self.A,
                self.B,
                self.C,
                self.alpha,
                self.beta,
                tile={"BM": 5, "BN": 7, "BK": 4},
                loop_order=loop_order,
            )
            self.assertEqual(result.shape, (17, 19))
            self.assertEqual(result.dtype, np.dtype(np.float32))
            self.assertTrue(
                np.allclose(result.astype(np.float64), self.reference, rtol=2e-3, atol=2e-3)
            )

    def test_beta_zero_does_not_read_or_reject_nan_c(self) -> None:
        C_nonfinite = np.full((17, 19), np.nan, dtype=np.float32)
        C_nonfinite[0, 0] = np.inf
        result = gemm(
            self.A,
            self.B,
            C_nonfinite,
            alpha=1.0,
            beta=0.0,
            tile={"BM": 5, "BN": 7, "BK": 4},
            loop_order="ikj",
        )
        expected = np.matmul(self.A, self.B).astype(np.float32)
        self.assertTrue(np.isfinite(result).all())
        self.assertTrue(np.allclose(result, expected, rtol=1e-6, atol=1e-6))

    def test_nonfinite_nonzero_beta_and_shape_errors_have_business_codes(self) -> None:
        A_bad = self.A.copy()
        A_bad[0, 0] = np.nan
        with self.assertRaises(GemmError) as context:
            gemm(A_bad, self.B, self.C, 1.0, 0.5)
        self.assertEqual(context.exception.code, "E_NONFINITE_INPUT")

        C_bad = self.C.copy()
        C_bad[0, 0] = np.inf
        with self.assertRaises(GemmError) as context:
            gemm(self.A, self.B, C_bad, 1.0, 0.5)
        self.assertEqual(context.exception.code, "E_NONFINITE_INPUT")

        with self.assertRaises(GemmError) as context:
            gemm(self.A, self.B[:11, :], self.C, 1.0, 0.0)
        self.assertEqual(context.exception.code, "E_SHAPE_MISMATCH")

    def test_invalid_tile_and_budget_codes(self) -> None:
        with self.assertRaises(GemmError) as context:
            gemm(
                self.A,
                self.B,
                self.C,
                tile={"BM": 0, "BN": 8, "BK": 8},
            )
        self.assertEqual(context.exception.code, "E_TILE_INVALID")
        selector = make_selector()
        with self.assertRaises(GemmError) as context:
            selector.select(8, 8, 8, batch=1, alpha=1.0, beta=0.0, cache_budget_bytes=0)
        self.assertEqual(context.exception.code, "E_BUDGET_INVALID")

    def test_bmm_rejects_batch_mismatch(self) -> None:
        A = np.zeros((3, 4, 5), dtype=np.float32)
        B = np.zeros((2, 5, 6), dtype=np.float32)
        C = np.zeros((3, 4, 6), dtype=np.float32)
        with self.assertRaises(GemmError) as context:
            bmm(A, B, C)
        self.assertEqual(context.exception.code, "E_SHAPE_MISMATCH")


class SelectorTests(unittest.TestCase):
    def test_decision_contains_all_candidates_and_costs(self) -> None:
        selector = make_selector()
        decision = selector.select(
            128, 160, 192, batch=2, alpha=1.0, beta=0.5, cache_budget_bytes=262144
        )
        self.assertFalse(decision["cache_hit"])
        self.assertEqual(len(decision["candidates"]), len(selector.strategies))
        self.assertIn(decision["reason_code"], {
            "R_SMALL_SHAPE_LOW_OVERHEAD",
            "R_FLATTENED_BATCH_THROUGHPUT",
            "R_BATCH_PARALLEL_MIN_COST",
            "R_CACHE_FIT_LOW_PADDING",
            "R_MIN_ESTIMATED_COST",
        })
        self.assertIn("estimated_cost_seconds", decision["candidates"][0])
        self.assertIn("cost_components", decision["candidates"][0])
        self.assertIn("estimated_cost_seconds", decision["cost_model"]["formula"])

    def test_cache_key_changes_for_every_required_component(self) -> None:
        selector = make_selector()
        base = dict(M=16, N=16, K=16, batch=8, alpha=1.0, beta=0.5, budget=131072)
        first = selector.select(
            base["M"], base["N"], base["K"], batch=base["batch"],
            alpha=base["alpha"], beta=base["beta"], cache_budget_bytes=base["budget"]
        )
        second = selector.select(
            base["M"], base["N"], base["K"], batch=base["batch"],
            alpha=base["alpha"], beta=base["beta"], cache_budget_bytes=base["budget"]
        )
        self.assertFalse(first["cache_hit"])
        self.assertTrue(second["cache_hit"])
        variants = [
            dict(base, M=17),
            dict(base, N=17),
            dict(base, K=17),
            dict(base, batch=9),
            dict(base, alpha=1.1),
            dict(base, beta=0.6),
            dict(base, budget=131073),
        ]
        keys = {first["cache_key"]}
        for variant in variants:
            decision = selector.select(
                variant["M"], variant["N"], variant["K"], batch=variant["batch"],
                alpha=variant["alpha"], beta=variant["beta"],
                cache_budget_bytes=variant["budget"],
            )
            self.assertFalse(decision["cache_hit"])
            self.assertNotIn(decision["cache_key"], keys)
            keys.add(decision["cache_key"])
        self.assertEqual(selector.cache_stats()["cache_hits"], 1)
        self.assertEqual(selector.cache_stats()["cache_misses"], 8)
        invalidated = selector.invalidate(first["cache_key"])
        self.assertEqual(invalidated, 1)
        self.assertEqual(selector.cache_stats()["cache_invalidations"], 1)
        third = selector.select(
            base["M"], base["N"], base["K"], batch=base["batch"],
            alpha=base["alpha"], beta=base["beta"], cache_budget_bytes=base["budget"]
        )
        self.assertFalse(third["cache_hit"])

    def test_worker_count_does_not_change_selection(self) -> None:
        one = AdaptiveEngine(max_workers=1)
        four = AdaptiveEngine(max_workers=4)
        decision_one = one.select_bmm(
            64, 64, 128, batch=16, alpha=1.0, beta=0.25, cache_budget_bytes=262144
        )
        decision_four = four.select_bmm(
            64, 64, 128, batch=16, alpha=1.0, beta=0.25, cache_budget_bytes=262144
        )
        for field in (
            "strategy_id",
            "tile",
            "loop_order",
            "batch_schedule",
            "reason_code",
            "selected_estimated_cost_seconds",
            "candidates",
        ):
            self.assertEqual(decision_one[field], decision_four[field])


class ConcurrencyTests(unittest.TestCase):
    def test_thread_count_preserves_output_decisions_and_statistics(self) -> None:
        rng = np.random.default_rng(2028)
        batch, M, N, K = 9, 11, 17, 13
        A = rng.standard_normal((batch, M, K), dtype=np.float32)
        B = rng.standard_normal((batch, K, N), dtype=np.float32)
        C = rng.standard_normal((batch, M, N), dtype=np.float32)
        serial = execute_bmm(
            A, B, C, 1.2, 0.3,
            tile={"BM": 5, "BN": 7, "BK": 4}, loop_order="ijk",
            batch_schedule="serial", max_workers=1, strategy_id="unit",
        )
        single = execute_bmm(
            A, B, C, 1.2, 0.3,
            tile={"BM": 5, "BN": 7, "BK": 4}, loop_order="ijk",
            batch_schedule="batch_parallel", max_workers=1, strategy_id="unit",
        )
        multi = execute_bmm(
            A, B, C, 1.2, 0.3,
            tile={"BM": 5, "BN": 7, "BK": 4}, loop_order="ijk",
            batch_schedule="batch_parallel", max_workers=4, strategy_id="unit",
        )
        flat_single = execute_bmm(
            A, B, C, 1.2, 0.3,
            tile={"BM": 5, "BN": 7, "BK": 4}, loop_order="ijk",
            batch_schedule="flattened", max_workers=1, strategy_id="unit",
            flatten_chunk_size=3,
        )
        flat_multi = execute_bmm(
            A, B, C, 1.2, 0.3,
            tile={"BM": 5, "BN": 7, "BK": 4}, loop_order="ijk",
            batch_schedule="flattened", max_workers=4, strategy_id="unit",
            flatten_chunk_size=3,
        )
        self.assertTrue(np.array_equal(serial.output, single.output))
        self.assertTrue(np.array_equal(serial.output, multi.output))
        self.assertTrue(np.array_equal(single.output, multi.output))
        self.assertEqual(serial.statistics, single.statistics)
        self.assertEqual(single.statistics, multi.statistics)
        self.assertEqual(serial.batch_decisions, single.batch_decisions)
        self.assertEqual(single.batch_decisions, multi.batch_decisions)
        self.assertTrue(np.array_equal(flat_single.output, flat_multi.output))
        self.assertTrue(np.array_equal(serial.output, flat_single.output))
        self.assertEqual(serial.statistics, flat_single.statistics)
        self.assertEqual(flat_single.statistics, flat_multi.statistics)
        self.assertEqual(serial.batch_decisions, flat_single.batch_decisions)
        self.assertIn("effective_batch_schedule", flat_single.scheduler)
        self.assertEqual(
            [item["batch_index"] for item in multi.batch_decisions], list(range(batch))
        )


class PrecisionAndReportTests(unittest.TestCase):
    def test_error_summary_reports_absolute_and_relative_metrics(self) -> None:
        reference = np.array([[1.0, 0.0], [100.0, -2.0]], dtype=np.float64)
        actual = reference.astype(np.float32) + np.array(
            [[1e-6, 1e-9], [1e-4, 0.0]], dtype=np.float32
        )
        summary = error_summary(actual, reference, relative_epsilon=1e-12)
        self.assertGreater(summary["max_absolute_error"], 0.0)
        self.assertGreater(summary["max_relative_error"], 0.0)
        self.assertEqual(summary["absolute_error_distribution"]["count"], 4)
        self.assertEqual(summary["nonfinite_output_count"], 0)

    def test_atomic_json_write_replaces_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "report.json"
            atomic_write_json(path, {"version": 1})
            atomic_write_json(path, {"version": 2})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"version": 2})
            self.assertEqual(list(path.parent.glob(".*.tmp")), [])

    def test_injection_samples_are_isolated_and_all_codes_hit(self) -> None:
        config = json.loads(Path(DEFAULT_INJECTIONS).read_text(encoding="utf-8"))
        summary, failures = _run_injections(config, AdaptiveEngine())
        self.assertEqual(summary["total"], 8)
        self.assertEqual(summary["matched_count"], 8)
        self.assertTrue(summary["isolation_preserved"])
        self.assertEqual(failures, [])
        observed = {
            item["expected_code"] for item in summary["results"] if item["matched"]
        }
        self.assertEqual(
            observed,
            {
                "E_NONFINITE_INPUT",
                "E_SHAPE_MISMATCH",
                "E_TILE_INVALID",
                "E_BUDGET_INVALID",
            },
        )

    def test_one_bad_injection_does_not_stop_following_samples(self) -> None:
        config = {
            "items": [
                {
                    "injection_id": "INTENTIONAL_MISMATCH",
                    "kind": "budget_zero",
                    "expected_code": "E_NONFINITE_INPUT",
                    "M": 6,
                    "N": 5,
                    "K": 4,
                    "batch": 1,
                    "alpha": 1.0,
                    "beta": 0.0,
                    "cache_budget_bytes": 0,
                },
                {
                    "injection_id": "STILL_RUNS",
                    "kind": "shape_mismatch",
                    "expected_code": "E_SHAPE_MISMATCH",
                    "M": 6,
                    "N": 5,
                    "K": 4,
                    "batch": 1,
                    "mismatch": "gemm_inner",
                },
            ]
        }
        summary, failures = _run_injections(config, AdaptiveEngine())
        self.assertEqual(summary["total"], 2)
        self.assertEqual(len(summary["results"]), 2)
        self.assertTrue(summary["isolation_preserved"])
        self.assertEqual(summary["results"][0]["observed_code"], "E_BUDGET_INVALID")
        self.assertFalse(summary["results"][0]["matched"])
        self.assertTrue(summary["results"][1]["matched"])
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["injection_id"], "INTENTIONAL_MISMATCH")

    def test_small_end_to_end_suite_writes_complete_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            shape_specs = {
                "schema_version": "1.0",
                "seed": 4242,
                "defaults": {
                    "alpha": 1.0,
                    "beta": 0.25,
                    "cache_budget_bytes": 32768,
                },
                "categories": [
                    {
                        "name": "small",
                        "specs": [
                            {"shape_id": "s1", "M": 5, "N": 7, "K": 4, "batch": 1},
                            {"shape_id": "s2", "M": 9, "N": 8, "K": 6, "batch": 2},
                            {"shape_id": "s3", "M": 12, "N": 10, "K": 9, "batch": 3},
                        ],
                    },
                    {
                        "name": "medium",
                        "specs": [
                            {"shape_id": "m1", "M": 24, "N": 20, "K": 22, "batch": 1},
                            {"shape_id": "m2", "M": 31, "N": 29, "K": 24, "batch": 4},
                            {"shape_id": "m3", "M": 40, "N": 33, "K": 28, "batch": 2},
                        ],
                    },
                    {
                        "name": "large_k",
                        "specs": [
                            {"shape_id": "k1", "M": 8, "N": 9, "K": 128, "batch": 1},
                            {"shape_id": "k2", "M": 16, "N": 12, "K": 192, "batch": 2},
                            {"shape_id": "k3", "M": 7, "N": 11, "K": 256, "batch": 3},
                        ],
                    },
                    {
                        "name": "large_batch",
                        "specs": [
                            {"shape_id": "b1", "M": 8, "N": 8, "K": 8, "batch": 32},
                            {"shape_id": "b2", "M": 12, "N": 9, "K": 10, "batch": 64},
                            {"shape_id": "b3", "M": 6, "N": 14, "K": 7, "batch": 128},
                        ],
                    },
                ],
            }
            shape_path = temporary / "shapes.json"
            shape_path.write_text(json.dumps(shape_specs), encoding="utf-8")
            output = temporary / "report.json"
            report = run_suite(
                shape_specs_path=shape_path,
                injection_config_path=DEFAULT_INJECTIONS,
                output_path=output,
                max_workers=3,
            )
            self.assertTrue(report["validation_summary"]["passed"])
            self.assertEqual(report["failure_list"], [])
            self.assertEqual(report["injection"]["matched_count"], 8)
            self.assertEqual(report["cache"]["cache_hits"], 12)
            self.assertEqual(report["cache"]["cache_misses"], 12)
            self.assertEqual(len(report["shapes"]), 12)
            self.assertEqual(
                report["validation_summary"]["category_counts"],
                {"small": 3, "medium": 3, "large_k": 3, "large_batch": 3},
            )
            persisted = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(persisted["component"], "adaptive_gemm_bmm")
            self.assertIn("failure_list", persisted)
            self.assertIn("cache", persisted)
            self.assertIn("accuracy", persisted["shapes"][0])
            self.assertIn("strategy_benchmarks", persisted["shapes"][0])
            self.assertEqual(list(output.parent.glob(".*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
