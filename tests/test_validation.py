import json
import tempfile
import unittest
from pathlib import Path

from ckptmgr.derivation import derive_checkpoint
from ckptmgr.errors import (
    DtypeInvalidError,
    LayerNameDuplicateError,
    ShardCountInvalidError,
    ShardSizeExceededError,
    ShapeInvalidError,
    StrategyInvalidError,
    VersionOrderInvalidError,
)
from ckptmgr.model import parse_plan
from tests.helpers import run_cli


def base_plan():
    return {
        "checkpoint": "unit",
        "global_seed": 42,
        "layers": [
            {"name": "a", "shape": [2, 3], "dtype": "float32"},
            {"name": "b", "shape": [4], "dtype": "int8"},
        ],
        "strategy": {
            "shard_count": 2,
            "max_shard_bytes": 65536,
            "max_concurrency": 2,
            "round_budget_ms": 100,
        },
        "versions": [
            {"version": 1, "changed_layers": ["*"]},
            {"version": 2, "changed_layers": ["a"]},
        ],
    }


class ValidationTests(unittest.TestCase):
    def test_plan_roundtrip_is_deterministic(self):
        plan = parse_plan(base_plan())
        reparsed = parse_plan(plan.to_canonical())
        self.assertEqual(plan.digest(), reparsed.digest())
        first = derive_checkpoint(plan, 2)
        second = derive_checkpoint(reparsed, 2)
        self.assertEqual(first.parameter_digest, second.parameter_digest)
        self.assertEqual(
            [(item.shard_id, item.digest) for item in first.shards],
            [(item.shard_id, item.digest) for item in second.shards],
        )

    def test_duplicate_layer_name(self):
        raw = base_plan()
        raw["layers"].append({"name": "a", "shape": [1], "dtype": "float32"})
        with self.assertRaises(LayerNameDuplicateError) as caught:
            parse_plan(raw)
        self.assertEqual(caught.exception.code, "E_PLAN_LAYER_NAME_DUPLICATE")

    def test_non_positive_shape_rejected(self):
        raw = base_plan()
        raw["layers"][0]["shape"] = [2, 0]
        with self.assertRaises(ShapeInvalidError) as caught:
            parse_plan(raw)
        self.assertEqual(caught.exception.code, "E_PLAN_SHAPE_INVALID")

    def test_unknown_dtype_rejected(self):
        raw = base_plan()
        raw["layers"][0]["dtype"] = "float128"
        with self.assertRaises(DtypeInvalidError) as caught:
            parse_plan(raw)
        self.assertEqual(caught.exception.code, "E_PLAN_DTYPE_INVALID")

    def test_shard_count_cannot_exceed_layers(self):
        raw = base_plan()
        raw["strategy"]["shard_count"] = 3
        with self.assertRaises(ShardCountInvalidError) as caught:
            parse_plan(raw)
        self.assertEqual(caught.exception.code, "E_PLAN_SHARD_COUNT_INVALID")

    def test_non_positive_strategy_values_rejected(self):
        for field in ("max_concurrency", "max_shard_bytes", "round_budget_ms"):
            raw = base_plan()
            raw["strategy"][field] = 0
            with self.assertRaises(StrategyInvalidError) as caught:
                parse_plan(raw)
            self.assertEqual(caught.exception.code, "E_PLAN_STRATEGY_INVALID")

    def test_versions_must_be_strictly_increasing(self):
        raw = base_plan()
        raw["versions"] = [2, 1]
        with self.assertRaises(VersionOrderInvalidError) as caught:
            parse_plan(raw)
        self.assertEqual(caught.exception.code, "E_PLAN_VERSION_ORDER_INVALID")

    def test_shard_byte_limit_is_checked_before_materialization(self):
        raw = base_plan()
        raw["strategy"]["max_shard_bytes"] = 8
        plan = parse_plan(raw)
        with self.assertRaises(ShardSizeExceededError) as caught:
            derive_checkpoint(plan, 1)
        self.assertEqual(caught.exception.code, "E_SHARD_SIZE_EXCEEDED")

        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            plan_path = temp_path / "oversized-plan.json"
            plan_path.write_text(json.dumps(raw), encoding="utf-8")
            root = temp_path / "workspace"
            completed, payload = run_cli(
                "save",
                "--root",
                str(root),
                "--plan",
                str(plan_path),
                "--version",
                "1",
            )
            self.assertEqual(completed.returncode, 2)
            self.assertEqual(payload["error"]["code"], "E_SHARD_SIZE_EXCEEDED")
            self.assertFalse(root.exists())

    def test_invalid_plan_cli_writes_nothing(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            raw = base_plan()
            raw["layers"][0]["shape"] = [-1]
            plan_path = temp_path / "bad-plan.json"
            plan_path.write_text(json.dumps(raw), encoding="utf-8")
            root = temp_path / "workspace"
            completed, payload = run_cli(
                "save",
                "--root",
                str(root),
                "--plan",
                str(plan_path),
                "--version",
                "1",
            )
            self.assertEqual(completed.returncode, 2)
            self.assertFalse(root.exists())
            self.assertEqual(payload["error"]["code"], "E_PLAN_SHAPE_INVALID")


if __name__ == "__main__":
    unittest.main()
