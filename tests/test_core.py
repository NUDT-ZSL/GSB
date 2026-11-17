from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from reward_diag.config import ConfigError, load_config, validate_config
from reward_diag.dependencies import build_summary_references, load_dependencies
from reward_diag.features import extract_many, extract_pair, fixed_hash_bucket
from reward_diag.io_utils import canonical_json
from reward_diag.samples import load_records
from reward_diag.scoring import score_many, score_pair
from reward_diag.summary import compute_summary


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "sample_data"


class CoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_config(str(SAMPLES / "feature_config.json"))
        cls.dependencies = load_dependencies(str(SAMPLES / "dependencies.json"))

    def test_invalid_weights_have_business_code(self) -> None:
        with self.assertRaises(ConfigError) as context:
            validate_config(
                json.loads((SAMPLES / "fixtures" / "illegal_weight.json").read_text())
            )
        self.assertEqual(context.exception.code, "E_WEIGHT_INVALID")

    def test_loader_isolates_invalid_rows_and_preserves_order(self) -> None:
        valid = {
            "pair_id": "p001",
            "prompt": "amber beacon",
            "chosen": "cedar delta",
            "rejected": "forest garden",
            "annotator_id": "a01",
            "timestamp": "2026-01-01T00:00:00Z",
        }
        missing = dict(valid, pair_id="p-missing")
        missing.pop("timestamp")
        empty = dict(valid, pair_id="p-empty", chosen="   ")
        bad_time = dict(valid, pair_id="p-bad-time", timestamp="not-a-time")
        entries = [
            {"source": f"test:{index}", "batch_id": "TEST", "raw": canonical_json(value)}
            for index, value in enumerate(
                [valid, missing, empty, bad_time, dict(valid, chosen="duplicate")], start=1
            )
        ]
        loaded = load_records(entries)
        self.assertEqual(list(loaded["valid"]), ["p001"])
        self.assertEqual(len(loaded["failures"]), 4)
        codes = [item["business_code"] for item in loaded["failures"]]
        self.assertEqual(codes.count("E_SAMPLE_INVALID"), 3)
        self.assertEqual(codes.count("E_EMPTY_TEXT"), 1)
        self.assertTrue(any("duplicate_pair_id" in item["reason"] for item in loaded["failures"]))

    def test_fixed_hash_and_concurrency_are_stable(self) -> None:
        pair = {
            "pair_id": "p001",
            "prompt": "Amber beacon?",
            "chosen": "cedar delta amber",
            "rejected": "forest garden",
            "annotator_id": "a01",
            "timestamp": "2026-01-01T00:00:00Z",
        }
        self.assertEqual(
            fixed_hash_bucket("amber", self.config),
            fixed_hash_bucket("amber", self.config),
        )
        pairs = {f"p{index:03d}": dict(pair, pair_id=f"p{index:03d}") for index in range(1, 9)}
        serial_config = copy.deepcopy(self.config)
        serial_config["concurrency"] = 1
        parallel_config = copy.deepcopy(self.config)
        parallel_config["concurrency"] = 8
        serial_features = extract_many(pairs, serial_config, "TEST")
        parallel_features = extract_many(pairs, parallel_config, "TEST")
        self.assertEqual(serial_features["entries"], parallel_features["entries"])
        serial_scores = score_many(serial_features["entries"], serial_config, "TEST")
        parallel_scores = score_many(parallel_features["entries"], parallel_config, "TEST")
        self.assertEqual(serial_scores["entries"], parallel_scores["entries"])

    def test_threshold_is_strict_and_equal_is_clear(self) -> None:
        config = validate_config(
            {
                "feature_dimensions": 8,
                "hash_buckets": 8,
                "weights": [0.25] * 8,
                "score_precision": 6,
                "reward_delta_threshold": 0.5,
            }
        )
        feature = {
            "pair_id": "equal",
            "cache_key": "cache",
            "vectors": {
                "chosen": [2, 0, 0, 0, 0, 0, 0, 0],
                "rejected": [0, 0, 0, 0, 0, 0, 0, 0],
            },
        }
        equal_score = score_pair(feature, config)
        self.assertEqual(equal_score["reward_delta"], 0.5)
        self.assertEqual(equal_score["threshold_relation"], "equal")
        self.assertFalse(equal_score["is_boundary"])
        strict_config = copy.deepcopy(config)
        strict_config["reward_delta_threshold"] = 0.500001
        boundary_score = score_pair(feature, strict_config)
        self.assertTrue(boundary_score["is_boundary"])
        self.assertEqual(boundary_score["decision"], "boundary")

    def test_empty_subword_is_warning_not_crash(self) -> None:
        pair = {
            "pair_id": "empty-token",
            "prompt": "!!!",
            "chosen": "???",
            "rejected": "...",
            "annotator_id": "a01",
            "timestamp": "2026-01-01T00:00:00Z",
        }
        feature = extract_pair(pair, self.config)
        self.assertEqual(feature["warnings"][0]["code"], "W_EMPTY_SUBWORDS")
        self.assertEqual(feature["referenced_buckets"], [])

    def test_boundary_samples_sort_by_pair_id(self) -> None:
        pairs = {}
        for pair_id in ("z-pair", "a-pair"):
            pairs[pair_id] = {
                "pair_id": pair_id,
                "prompt": "amber beacon",
                "chosen": "cedar delta",
                "rejected": "cedar delta",
                "annotator_id": "a01",
                "timestamp": "2026-01-01T00:00:00Z",
            }
        loaded = {
            "valid": pairs,
            "record_fingerprints": {key: key for key in pairs},
            "failures": [],
        }
        features = extract_many(pairs, self.config, "TEST")["entries"]
        scores = score_many(features, self.config, "TEST")["entries"]
        summary = compute_summary(
            loaded,
            features,
            scores,
            [],
            [],
            [],
            self.config,
            ["TEST"],
            "test-data",
            self.dependencies,
        )
        self.assertEqual(summary["boundary_pair_ids"], ["a-pair", "z-pair"])

    def test_dependency_reference_resolution_uses_real_buckets(self) -> None:
        refs = build_summary_references(
            self.dependencies, ["BASE", "B001"], [1, 4, 9]
        )
        self.assertEqual(refs["bucket_frequency"]["batches"], ["BASE", "B001"])
        self.assertEqual(refs["bucket_frequency"]["buckets"], [1, 4, 9])
        self.assertEqual(refs["failure_list"]["buckets"], [])


if __name__ == "__main__":
    unittest.main()
