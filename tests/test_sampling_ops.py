from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from sampling_ops import (
    BusinessError,
    adjudicate_bias,
    decide_route,
    run_task,
    sample,
    self_check,
    topk,
    topk_all_routes,
)
from sampling_ops.decision import decision_bytes
from sampling_ops.generator import demo_tasks, generate_logits
from sampling_ops.snapshots import SnapshotStore


def make_task(**overrides):
    task = {
        "case_id": "unit-case",
        "vocab_size": 128,
        "batch_size": 2,
        "seed": 1234,
        "k_mode": "auto",
        "k_value": 7,
        "temperature": 0.9,
        "top_p": 0.9,
        "group_size": 4,
        "dtype": "float64",
        "tie_policy": "index_asc",
        "resource_profile": {
            "max_compare": 50_000_000,
            "max_memory_bytes": 32 * 1024 * 1024,
            "chunk_size": 32,
        },
    }
    task.update(overrides)
    return task


class TopKTests(unittest.TestCase):
    def test_three_routes_match_with_ties(self):
        rng = np.random.Generator(np.random.PCG64(9876))
        logits = np.round(rng.standard_normal((3, 513)) / 0.25) * 0.25
        for k in (1, 7, 64, 513):
            results = topk_all_routes(logits, k, tie_policy="index_asc", chunk_size=31)
            baseline = results["full_sort"].indices
            for route, result in results.items():
                np.testing.assert_array_equal(result.indices, baseline, err_msg=route)
                self.assertEqual(result.indices.shape, (3, k))
                for row in range(logits.shape[0]):
                    selected_scores = logits[row, result.indices[row]]
                    self.assertTrue(np.all(selected_scores[:-1] >= selected_scores[1:]))
                    for left in range(k - 1):
                        if selected_scores[left] == selected_scores[left + 1]:
                            self.assertLess(
                                int(result.indices[row, left]),
                                int(result.indices[row, left + 1]),
                            )

    def test_k_and_shape_errors(self):
        logits = np.zeros((2, 10), dtype=np.float64)
        with self.assertRaises(BusinessError) as context:
            topk(logits, 0, case_id="bad-k")
        self.assertEqual(context.exception.code, "E_K_INVALID")
        self.assertEqual(context.exception.field, "k_value")
        with self.assertRaises(BusinessError) as context:
            topk(logits, 11, case_id="bad-k")
        self.assertEqual(context.exception.code, "E_K_INVALID")
        with self.assertRaises(BusinessError) as context:
            topk(logits, 1.5, case_id="bad-k")
        self.assertEqual(context.exception.code, "E_K_INVALID")
        with self.assertRaises(BusinessError) as context:
            topk(np.zeros((2, 2, 2)), 1, case_id="bad-shape")
        self.assertEqual(context.exception.code, "E_SHAPE_INVALID")
        self.assertEqual(context.exception.field, "logits")


class DecisionTests(unittest.TestCase):
    def test_decision_is_byte_reproducible_and_within_limits(self):
        rows = demo_tasks()
        for row in rows[:4]:
            first = run_task(row)
            second = run_task(row)
            self.assertEqual(
                decision_bytes(first["decision"]),
                decision_bytes(second["decision"]),
            )
            self.assertEqual(
                first["decision"]["canonical_bytes_sha256"],
                second["decision"]["canonical_bytes_sha256"],
            )
            self.assertTrue(first["decision"]["selected_within_limits"])
            selected = first["decision"]["selected_route"]
            estimate = first["decision"]["estimates"][selected]
            limits = first["decision"]["resource_limits"]
            self.assertLessEqual(estimate["comparisons"], limits["max_compare"])
            self.assertLessEqual(
                estimate["peak_memory_bytes"], limits["max_memory_bytes"]
            )
        self.assertEqual(rows[0]["case_id"], "small-full-sort")
        self.assertEqual(run_task(rows[0])["decision"]["selected_route"], "full_sort")
        self.assertEqual(
            run_task(rows[1])["decision"]["selected_route"], "heap_select"
        )
        self.assertEqual(
            run_task(rows[2])["decision"]["selected_route"], "chunked_two_stage"
        )

    def test_decision_has_estimates_and_conflict_reasons(self):
        record = decide_route(
            4096,
            3,
            16,
            "float32",
            resource_profile={
                "max_compare": 100_000,
                "max_memory_bytes": 8 * 1024 * 1024,
                "compare_soft_ratio": 0.7,
                "chunk_size": 512,
            },
            case_id="decision-case",
        )
        self.assertIn(record["selected_route"], set(record["estimates"]))
        self.assertTrue(record["estimates"]["full_sort"]["comparisons"] > 0)
        self.assertTrue(record["conflict_resolution"])
        self.assertEqual(record["degraded_category"], "COMPARE_BUDGET_ADAPTATION")


class SamplingTests(unittest.TestCase):
    def test_sampling_uses_only_local_rng(self):
        logits = np.linspace(-2.0, 2.0, 40, dtype=np.float64)
        candidates = topk(logits, 12, case_id="rng")
        np.random.seed(77)
        before = np.random.get_state()
        first = sample(
            candidates.scores,
            candidates.indices,
            temperature=0.7,
            top_p=0.85,
            seed=9988,
            group_size=9,
            case_id="rng",
        )
        second = sample(
            candidates.scores,
            candidates.indices,
            temperature=0.7,
            top_p=0.85,
            seed=9988,
            group_size=9,
            case_id="rng",
        )
        after = np.random.get_state()
        np.testing.assert_array_equal(first.samples, second.samples)
        np.testing.assert_array_equal(first.random_values, second.random_values)
        self.assertEqual(before[0], after[0])
        np.testing.assert_array_equal(before[1], after[1])
        self.assertEqual(before[2:], after[2:])

    def test_top_p_boundary_is_inclusive(self):
        candidates = topk(np.asarray([[0.0, -1.0, -2.0]]), 3)
        scaled = sample(
            candidates.scores,
            candidates.indices,
            temperature=1.0,
            top_p=1.0,
            seed=1,
            group_size=1,
        )
        self.assertEqual(scaled.top_p_result.retained_counts, [3])
        truncated = sample(
            candidates.scores,
            candidates.indices,
            temperature=1.0,
            top_p=0.8,
            seed=1,
            group_size=1,
        )
        self.assertEqual(truncated.top_p_result.retained_counts, [2])
        self.assertEqual(
            truncated.top_p_result.cutoff_cumulative[0],
            truncated.top_p_result.scaled.cumulative_probabilities[0, 1],
        )


class PipelineTests(unittest.TestCase):
    def test_pipeline_bias_and_self_check_are_consistent(self):
        task = demo_tasks()[0]
        result = run_task(task)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["business_code"], "OK")
        self.assertEqual(result["bias"]["business_code"], "OK")
        self.assertEqual(result["self_check"]["business_code"], "OK")
        routes = result["topk"]["per_route"]
        baseline = routes["full_sort"]["indices"]
        for route in ("heap_select", "chunked_two_stage"):
            self.assertEqual(routes[route]["indices"], baseline)
        replay = run_task(task)
        self.assertEqual(result["sampling"]["samples"], replay["sampling"]["samples"])

    def test_four_stage_injections_are_attributed(self):
        expected = {
            "inject-candidate-nonfinite": ("candidate_filter", "E_LOGITS_NON_FINITE", "logits"),
            "inject-temperature-zero": ("temperature_scale", "E_TEMPERATURE_INVALID", "temperature"),
            "inject-top-p-range": ("top_p_filter", "E_TOP_P_INVALID", "top_p"),
            "inject-seed-missing": ("sample", "E_SEED_INVALID", "seed"),
        }
        rows = {row["case_id"]: row for row in demo_tasks()}
        for case_id, (stage, code, field) in expected.items():
            result = run_task(rows[case_id])
            self.assertEqual(result["status"], "INJECTED_ERROR")
            matching = [
                item
                for item in result["stage_trace"]
                if item["stage"] == stage and item["status"] == "error"
            ]
            self.assertEqual(len(matching), 1)
            self.assertEqual(matching[0]["business_code"], code)
            self.assertTrue(matching[0]["injected"])
            error = next(item for item in result["errors"] if item["stage"] == stage)
            self.assertEqual(error["field"], field)
            injection = next(item for item in result["injections"] if item["stage"] == stage)
            self.assertEqual(injection["business_code"], code)
            self.assertEqual(injection["case_id"], case_id)
            if stage != "candidate_filter":
                self.assertIsNotNone(result["topk"]["candidates"])
            else:
                self.assertIsNone(result["topk"]["candidates"])

    def test_resource_overrun_still_emits_candidates_and_samples(self):
        row = next(item for item in demo_tasks() if item["case_id"] == "resource-overrun-check")
        result = run_task(row)
        self.assertEqual(result["status"], "OK")
        self.assertIn("W_RESOURCE_OVERRUN", result["warnings"])
        self.assertFalse(result["decision"]["selected_within_limits"])
        self.assertEqual(
            result["decision"]["degraded_category"], "RESOURCE_BUDGET_OVERRUN"
        )
        self.assertIsNotNone(result["topk"]["candidates"])
        self.assertIsNotNone(result["sampling"]["samples"])
        self.assertEqual(result["bias"]["business_code"], "OK")

    def test_input_business_codes(self):
        unknown = run_task(make_task(dtype="bfloat16"))
        self.assertEqual(unknown["business_code"], "E_DTYPE_UNSUPPORTED")
        self.assertEqual(unknown["errors"][0]["field"], "dtype")

        mismatch = run_task(make_task(), logits=np.zeros((3, 128), dtype=np.float64))
        self.assertEqual(mismatch["business_code"], "E_SHAPE_INVALID")
        self.assertEqual(mismatch["errors"][0]["field"], "logits")

        bad_k = run_task(make_task(k_value=129))
        self.assertEqual(bad_k["business_code"], "E_K_INVALID")
        self.assertEqual(bad_k["errors"][0]["field"], "k_value")

    def test_bias_detects_altered_route(self):
        row = demo_tasks()[0]
        result = run_task(row)
        route_results = {
            route: (
                np.asarray(payload["indices"]),
                np.asarray(payload["scores"]),
            )
            for route, payload in result["topk"]["per_route"].items()
        }
        altered = route_results["heap_select"]
        indices = altered[0].copy()
        indices[0, [0, 1]] = indices[0, [1, 0]]
        route_results["heap_select"] = (indices, altered[1])
        report = adjudicate_bias(
            row["case_id"],
            generate_logits(row),
            row["k_value"],
            route_results,
            temperature=row["temperature"],
            top_p=row["top_p"],
            seed=row["seed"],
            group_size=row["group_size"],
        )
        self.assertEqual(report["business_code"], "E_TOLERANCE_EXCEEDED")
        self.assertTrue(report["violations"])
        self.assertEqual(report["violations"][0]["route"], "heap_select")
        self.assertEqual(report["violations"][0]["case_id"], row["case_id"])

    def test_self_check_detects_replay_mismatch(self):
        row = demo_tasks()[0]
        result = run_task(row)
        candidates = {
            route: (np.asarray(payload["indices"]), np.asarray(payload["scores"]))
            for route, payload in result["topk"]["per_route"].items()
        }
        samples = {
            route: np.asarray(payload["samples"])
            for route, payload in result["sampling"]["per_route"].items()
        }
        samples["heap_select"] = samples["heap_select"].copy()
        samples["heap_select"][0, 0] = -1
        report = self_check(
            row["case_id"],
            candidates,
            samples,
            temperature=row["temperature"],
            top_p=row["top_p"],
            seed=row["seed"],
            group_size=row["group_size"],
        )
        self.assertEqual(report["business_code"], "E_INCONSISTENT")
        self.assertTrue(report["failures"])


class SnapshotTests(unittest.TestCase):
    def test_versioned_whole_replacement_and_checksum(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SnapshotStore(directory)
            first = store.put("config", {"a": 1, "nested": {"x": 1}})
            self.assertEqual(first["version"], 1)
            self.assertFalse(store.put("config", {"a": 1, "nested": {"x": 1}})["changed"])
            second = store.put("config", {"a": 2})
            self.assertEqual(second["version"], 2)
            self.assertEqual(second["content"], {"a": 2})
            self.assertNotIn("nested", second["content"])
            third = store.put("stats", {"ok": True})
            self.assertEqual(third["version"], 1)
            with self.assertRaises(BusinessError) as context:
                store.put("config", {"a": 3}, requested_version=1)
            self.assertEqual(context.exception.code, "E_SNAPSHOT_VERSION_REGRESSION")
            path = Path(directory) / "config.snapshot.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["checksum"] = "0" * 64
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(BusinessError) as context:
                store.load("config")
            self.assertEqual(context.exception.code, "E_SNAPSHOT_INVALID")


if __name__ == "__main__":
    unittest.main()

