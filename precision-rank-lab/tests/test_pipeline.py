import json
import tempfile
import unittest
from pathlib import Path

from prank.analysis import build_report, report_hash
from prank.config import build_config
from prank.data_gen import (apply_data_injections, generate_dataset,
                            read_jsonl, write_jsonl)
from prank.errors import BusinessError
from prank.pipeline import execute_dataset


RAW_CONFIG = {
    "feature_weights": {"term_match": 2.4, "category_match": 1.1,
                        "price_bucket": 0.8, "quality_scaled": 1.5},
    "score_bias": -1.2, "threshold": 0.42, "tolerance": 0.01,
    "gate_strategy": "tolerance_band", "top_k": 10, "rerank_n": 10,
    "rerank": {"category_penalty": 0.18, "position_decay": 0.85},
    "precision": {"reference": "float64",
                  "tiers": ["float64", "float32", "fixed"],
                  "fixed": {"fraction_bits": 16, "scale": 65536,
                            "rounding": "half_even", "int_bits": 62,
                            "exp_terms": 24}},
    "subnormal_protection": True,
    "bounds": {"price_min": 0.01, "price_max": 100000.0,
               "quality_min": 0.0, "quality_max": 1.0},
    "degradation": {"policy": "fallback_float64",
                    "allowed_policies": ["abort", "skip_query", "fallback_float64"]},
    "report": {"interval_quantiles": [0.0, 0.5, 0.95, 1.0],
               "near_threshold_delta": 0.05, "report_hash_alg": "sha256"},
}


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name) / "data"
        candidates = generate_dataset(seed=7, n_queries=6, min_items=20,
                                      max_items=40)
        write_jsonl(candidates, self.data_dir)
        self.cfg = build_config(json.loads(json.dumps(RAW_CONFIG)))

    def tearDown(self):
        self.tmp.cleanup()

    def test_deterministic_byte_identical_rerun(self):
        r1 = build_report(execute_dataset(self.data_dir, self.cfg), self.cfg,
                          seed=7, data_dir=str(self.data_dir))
        r2 = build_report(execute_dataset(self.data_dir, self.cfg), self.cfg,
                          seed=7, data_dir=str(self.data_dir))
        self.assertEqual(report_hash(r1), report_hash(r2))
        self.assertEqual(json.dumps(r1, sort_keys=True),
                         json.dumps(r2, sort_keys=True))

    def test_three_tiers_share_item_sets_and_tie_break(self):
        result = execute_dataset(self.data_dir, self.cfg)
        for record in result["per_query"]:
            tiers = record["tiers"]
            ref_ids = set(tiers["float64"]["item_ids"])
            for mode in ("float32", "fixed"):
                self.assertEqual(set(tiers[mode]["item_ids"]), ref_ids)
                # fine-rank and rerank operate on identical candidate sets
                self.assertEqual(set(tiers[mode]["fine_order"]),
                                 set(tiers[mode]["rerank_order"]))

    def test_fixed_recompute_and_intervals(self):
        report = build_report(execute_dataset(self.data_dir, self.cfg),
                              self.cfg, seed=7, data_dir=str(self.data_dir))
        fixed = report["tier_error_analysis"]["fixed"]
        self.assertTrue(fixed["fixed_recompute_check"]["recompute_exact_match"])
        self.assertTrue(
            fixed["fixed_recompute_check"]["all_within_quantization_bound"])
        for stage in ("features", "linear", "sigmoid"):
            interval = fixed["stages"][stage]["abs_error"]["interval"]
            keys = list(interval)
            values = [interval[k] for k in keys]
            self.assertEqual(values, sorted(values))

    def test_gate_no_admission_flips(self):
        report = build_report(execute_dataset(self.data_dir, self.cfg),
                              self.cfg, seed=7, data_dir=str(self.data_dir))
        stability = report["threshold_stability"]
        self.assertEqual(stability["band_strategy_cross_tier_flips"], 0)

    def test_dup_query_is_fatal_dataset_error(self):
        candidates = read_jsonl(self.data_dir)
        mutated, _ = apply_data_injections(
            candidates, [{"type": "dup_query", "target_query": "q0001"}],
            self.cfg.bounds)
        bad_dir = Path(self.tmp.name) / "bad"
        write_jsonl(mutated, bad_dir)
        with self.assertRaises(BusinessError) as ctx:
            execute_dataset(bad_dir, self.cfg)
        self.assertEqual(ctx.exception.code, "E_DUP_QUERY")
        self.assertEqual(ctx.exception.query_id, "q0001")

    def test_empty_terms_and_range_errors_degrade_with_chain(self):
        candidates = read_jsonl(self.data_dir)
        mutated, _ = apply_data_injections(candidates, [
            {"type": "empty_item_terms", "target_query": "q0002",
             "target_item_offset": 0},
            {"type": "value_range", "target_query": "q0003", "field": "price",
             "bad_value": -9.0},
        ], self.cfg.bounds)
        bad_dir = Path(self.tmp.name) / "degrade"
        write_jsonl(mutated, bad_dir)
        result = execute_dataset(bad_dir, self.cfg)
        codes = {e["code"] for e in result["business_events"]}
        self.assertIn("E_ITEM_EMPTY", codes)
        self.assertIn("E_VALUE_RANGE", codes)
        degraded = {d["query_id"]: d for d in result["degraded_queries"]}
        self.assertIn("q0002", degraded)
        self.assertIn("q0003", degraded)
        self.assertEqual(degraded["q0002"]["upstream_codes"], ["E_ITEM_EMPTY"])

    def test_abort_policy_propagates_error(self):
        cfg = build_config({**RAW_CONFIG,
                            "degradation": {"policy": "abort",
                                            "allowed_policies": ["abort"]}})
        candidates = read_jsonl(self.data_dir)
        mutated, _ = apply_data_injections(
            candidates,
            [{"type": "value_range", "target_query": "q0001",
              "field": "quality", "bad_value": 2.0}], cfg.bounds)
        bad_dir = Path(self.tmp.name) / "abort"
        write_jsonl(mutated, bad_dir)
        with self.assertRaises(BusinessError) as ctx:
            execute_dataset(bad_dir, cfg)
        self.assertEqual(ctx.exception.code, "E_VALUE_RANGE")

    def test_skip_query_policy(self):
        cfg = build_config({**RAW_CONFIG,
                            "degradation": {"policy": "skip_query",
                                            "allowed_policies": ["skip_query"]}})
        candidates = read_jsonl(self.data_dir)
        mutated, _ = apply_data_injections(
            candidates,
            [{"type": "empty_item_terms", "target_query": "q0004"}],
            cfg.bounds)
        bad_dir = Path(self.tmp.name) / "skip"
        write_jsonl(mutated, bad_dir)
        result = execute_dataset(bad_dir, cfg)
        record = next(q for q in result["per_query"] if q["query_id"] == "q0004")
        self.assertEqual(record["status"], "degraded")
        self.assertEqual(record["degradation"]["policy"], "skip_query")


if __name__ == "__main__":
    unittest.main()
