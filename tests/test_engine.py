from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from feature_diag.cache import CacheStore
from feature_diag.config import replace_config
from feature_diag.engine import add_cross_slot, build_feature_plan, compute, feature_cache_key, fit_context
from feature_diag.errors import (
    E_BAD_INPUT,
    E_ENCODING_ERROR,
    E_PARAM_INVALID,
    E_RESOURCE_LIMIT,
    EngineError,
)
from feature_diag.generator import prepare_workspace
from feature_diag.query import QueryService
from feature_diag.sample import read_rows_csv
from feature_diag.snapshot import load_v2
from feature_diag.timing import LogicalClock
from feature_diag.util import canonical_json, sha256_json


class EngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        prepare_workspace(self.root)
        self.config = load_v2(self.root / "configs" / "v2")
        self.rows = read_rows_csv(self.root / "data" / "sample.csv", self.config)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_encoding_and_selection_are_deterministic(self) -> None:
        first = compute(self.rows, self.config)
        second = compute(copy.deepcopy(self.rows), copy.deepcopy(self.config))
        self.assertEqual(first.matrix_digest, second.matrix_digest)
        self.assertEqual(first.selection_key, second.selection_key)
        self.assertEqual(first.feature_scores, second.feature_scores)
        self.assertEqual(first.retained_feature_ids, second.retained_feature_ids)
        self.assertEqual(
            canonical_json(first.payload()),
            canonical_json(second.payload()),
        )
        self.assertEqual(len(first.retained_feature_ids), self.config.selector["target_k"])

    def test_feature_cache_key_is_content_addressed(self) -> None:
        plan = build_feature_plan(self.config)
        context = fit_context(self.config, sorted(self.rows, key=lambda row: row["row_id"]))
        feature = plan.features[0]
        first_key = feature_cache_key(feature, self.rows[0], context)
        mutated = copy.deepcopy(self.rows[0])
        mutated["numerics"]["age"] = 77.0
        second_key = feature_cache_key(feature, mutated, context)
        self.assertNotEqual(first_key, second_key)

    def test_query_matches_batch_record_and_reports_latency(self) -> None:
        computation = compute(self.rows, self.config)
        cache = CacheStore(self.root / "work" / "query-cache.sqlite", self.config.limits["max_cache_entries"])
        cache.commit(computation, input_digest="input", config_digest=self.config.semantic_digest)
        response = QueryService(cache).query_row(
            "r0030",
            clock=LogicalClock(),
        )
        self.assertEqual(
            canonical_json(response["record"]),
            canonical_json(computation.record_for("r0030")),
        )
        self.assertTrue(response["record"]["selection_key"])
        self.assertEqual(
            len(response["record"]["feature_ids"]),
            len(response["record"]["encoded"]),
        )
        self.assertGreater(response["latency"]["actual_ns"], 0)
        self.assertGreater(response["latency"]["estimated_ns"], 0)
        self.assertEqual(
            response["latency"]["completed_steps"],
            [
                "validate_request",
                "load_current_selection",
                "locate_row_record",
                "decode_encoding_vector",
                "project_retained_features",
            ],
        )

    def test_query_timeout_returns_completed_steps(self) -> None:
        computation = compute(self.rows, self.config)
        cache = CacheStore(self.root / "work" / "timeout-cache.sqlite", self.config.limits["max_cache_entries"])
        cache.commit(computation, input_digest="input", config_digest=self.config.semantic_digest)
        with self.assertRaises(EngineError) as caught:
            QueryService(cache).query_row("r0001", timeout_ms=0, clock=LogicalClock())
        self.assertEqual(caught.exception.code, "E_LATENCY_BUDGET")
        self.assertEqual(caught.exception.completed_steps, ["validate_request"])

    def test_validation_and_resource_errors(self) -> None:
        with self.assertRaises(EngineError) as duplicate:
            duplicate_rows = copy.deepcopy(self.rows)
            duplicate_rows.append(copy.deepcopy(duplicate_rows[0]))
            compute(duplicate_rows, self.config)
        self.assertEqual(duplicate.exception.code, E_BAD_INPUT)
        self.assertIn("duplicate_row_ids", duplicate.exception.detail)

        with self.assertRaises(EngineError) as nonfinite:
            invalid = copy.deepcopy(self.rows)
            invalid[0]["numerics"]["score"] = float("inf")
            compute(invalid, self.config)
        self.assertEqual(nonfinite.exception.code, E_BAD_INPUT)
        self.assertEqual(nonfinite.exception.detail["row"], "r0001")
        self.assertEqual(nonfinite.exception.detail["field"], "score")

        with self.assertRaises(EngineError) as category:
            invalid = copy.deepcopy(self.rows)
            invalid[0]["categoricals"]["region"] = "outside"
            compute(invalid, self.config)
        self.assertEqual(category.exception.code, E_ENCODING_ERROR)
        self.assertEqual(category.exception.detail["row"], "r0001")
        self.assertEqual(category.exception.detail["field"], "region")

        with self.assertRaises(EngineError) as missing_cross:
            add_cross_slot(
                self.config,
                {
                    "slot": "invalid",
                    "columns": ["region", "missing"],
                    "codec": "one_hot",
                    "prior_weight": 5.0,
                },
            )
        self.assertEqual(missing_cross.exception.code, E_PARAM_INVALID)

        with self.assertRaises(EngineError) as resource:
            compute(self.rows, replace_config(self.config, limits={**self.config.limits, "max_resident_bytes": 1}))
        self.assertEqual(resource.exception.code, E_RESOURCE_LIMIT)

    def test_mutual_information_selector_is_deterministic(self) -> None:
        config = replace_config(
            self.config,
            selector={
                "method": "mutual_info",
                "target_k": 10,
                "mi_bins": 8,
                "tie_break": "feature_id_asc",
            },
        )
        first = compute(self.rows, config)
        second = compute(copy.deepcopy(self.rows), config)
        self.assertEqual(first.selection_key, second.selection_key)
        self.assertEqual(first.feature_scores, second.feature_scores)
        self.assertEqual(len(first.retained_feature_ids), 10)


if __name__ == "__main__":
    unittest.main()
