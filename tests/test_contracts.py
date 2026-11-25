from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from binning_engine.cache import ContentCache, make_cache_key
from binning_engine.codes import EngineError
from binning_engine.config import normalize_config
from binning_engine.encoding import encode_rows
from binning_engine.hashing import write_atomic_json
from binning_engine.pipeline import run_pipeline
from binning_engine.reference import reference_encode
from binning_engine.resource import plan_resources
from binning_engine.scheduler import BatchScheduler
from binning_engine.snapshot import SnapshotStore


def tiny_config(**overrides):
    config = {
        "version": 1,
        "numeric_columns": ["x"],
        "categorical_columns": ["c"],
        "label_column": "y",
        "vocabulary": {"c": ["a", "b"]},
        "bins": {"x": {"bin_count": 2, "bin_method": "equal_width"}},
        "encoding": {"method": "one_hot", "smoothing": 0.0},
        "quantization": {"default": {"quant_scale": 1.0, "zero_point": 0}},
        "precision_tier": "int8",
        "scheduler": {"workers": 1, "batch_size": 2},
        "resource_budget": {
            "max_memory_bytes": 64 * 1024 * 1024,
            "max_parallel_slots": 2,
            "max_comparisons": 1_000_000,
            "max_encoding_length": 100_000,
        },
    }
    config.update(overrides)
    return config


def tiny_rows():
    return [
        {"row_id": 1, "x": 0.0, "c": "a", "y": 0},
        {"row_id": 2, "x": 1.0, "c": "a", "y": 0},
        {"row_id": 3, "x": 2.0, "c": "b", "y": 1},
        {"row_id": 4, "x": 3.0, "c": "b", "y": 1},
    ]


class BusinessCodeTests(unittest.TestCase):
    def assert_code(self, code, callback):
        with self.assertRaises(EngineError) as context:
            callback()
        self.assertEqual(context.exception.code, code)

    def test_input_and_config_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            duplicate = tiny_rows() + [dict(tiny_rows()[0])]
            self.assert_code(
                "E_DUP_INPUT",
                lambda: run_pipeline(
                    tiny_config(),
                    records=duplicate,
                    output_dir=Path(tmp) / "dup",
                    use_process_pool=False,
                ),
            )
            nan_rows = tiny_rows()
            nan_rows[0]["x"] = float("nan")
            self.assert_code(
                "E_NAN_VALUE",
                lambda: run_pipeline(
                    tiny_config(),
                    records=nan_rows,
                    output_dir=Path(tmp) / "nan",
                    use_process_pool=False,
                ),
            )

        self.assert_code(
            "E_CODE_METHOD",
            lambda: normalize_config(tiny_config(encoding={"method": "unknown"})),
        )
        self.assert_code(
            "E_QUANT_PARAM",
            lambda: normalize_config(
                tiny_config(
                    quantization={"default": {"quant_scale": 0.0, "zero_point": 0}}
                )
            ),
        )

    def test_worker_row_error_attributed(self):
        config = normalize_config(tiny_config())
        reference = reference_encode(tiny_rows(), config)
        bad_rows = tiny_rows()
        bad_rows[1]["c"] = "outside"
        batch = encode_rows(
            bad_rows,
            config,
            reference["boundaries"],
            reference["feature_spec"],
            reference["context"],
            batch_id="batch-00007",
        )
        self.assertEqual(batch["success_count"], 3)
        self.assertEqual(batch["error_count"], 1)
        error = batch["row_errors"][0]["error"]
        self.assertEqual(error["code"], "E_CATEGORY_VIOLATION")
        self.assertEqual(error["batch_id"], "batch-00007")
        self.assertIsNotNone(error["row_id"])

        scheduler = BatchScheduler(workers=1, use_process_pool=True)
        results = scheduler.run_ordered(
            [
                {
                    "batch_id": "batch-00000",
                    "rows": bad_rows,
                    "config": config,
                    "boundary_result": reference["boundaries"],
                    "feature_spec": reference["feature_spec"],
                    "context": reference["context"],
                }
            ]
        )
        self.assertEqual(results[0]["error_count"], 1)


class CacheSnapshotResourceTests(unittest.TestCase):
    def test_corrupt_cache_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = ContentCache(Path(tmp))
            key = make_cache_key("input_validation", {"sample": "abc"})
            cache.put("input_validation", key, {"value": 1})
            path = cache._path("input_validation", key)
            envelope = json.loads(path.read_text())
            envelope["data"]["value"] = 2
            write_atomic_json(path, envelope)
            with self.assertRaises(EngineError) as context:
                cache.get("input_validation", key)
            self.assertEqual(context.exception.code, "E_CACHE_CORRUPT")

    def test_snapshot_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(Path(tmp))
            first = store.put("encoding_result", {"ok": 1}, 3)
            again = store.put("encoding_result", {"ok": 1}, 3)
            self.assertEqual(first["checksum"], again["checksum"])
            with self.assertRaises(EngineError) as context:
                store.put("encoding_result", {"ok": 2}, 2)
            self.assertEqual(context.exception.code, "E_SNAPSHOT_ROLLBACK")
            with self.assertRaises(EngineError) as context:
                store.put("encoding_result", {"ok": 3}, 3)
            self.assertEqual(context.exception.code, "E_CONFIG_VERSION")

    def test_pipeline_config_version_rollback_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "run"
            rows = tiny_rows()
            run_pipeline(
                tiny_config(version=3),
                records=rows,
                output_dir=output,
                use_process_pool=False,
            )
            with self.assertRaises(EngineError) as context:
                run_pipeline(
                    tiny_config(version=2),
                    records=rows,
                    output_dir=output,
                    use_process_pool=False,
                )
            self.assertEqual(context.exception.code, "E_CONFIG_VERSION")

    def test_resource_limits(self):
        config = normalize_config(tiny_config())
        plan = plan_resources(config, 100, requested_workers=8, requested_batch_size=10)
        self.assertEqual(plan["code"], "E_RESOURCE_LIMIT")
        self.assertLessEqual(plan["workers"], 2)
        self.assertTrue(plan["decisions"])

        impossible = normalize_config(
            tiny_config(
                resource_budget={
                    "max_memory_bytes": 1,
                    "max_parallel_slots": 1,
                    "max_comparisons": 1,
                    "max_encoding_length": 1,
                }
            )
        )
        with self.assertRaises(EngineError) as context:
            plan_resources(impossible, 100)
        self.assertEqual(context.exception.code, "E_RESOURCE_LIMIT")

    def test_degraded_batches_carry_business_code(self):
        config = tiny_config(
            scheduler={"workers": 4, "batch_size": 2},
            resource_budget={
                "max_memory_bytes": 64 * 1024 * 1024,
                "max_parallel_slots": 2,
                "max_comparisons": 1_000_000,
                "max_encoding_length": 100_000,
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = run_pipeline(
                config,
                records=tiny_rows(),
                output_dir=Path(tmp) / "resource",
                workers=4,
                use_process_pool=False,
            )
        self.assertEqual(result["resource_plan"]["code"], "E_RESOURCE_LIMIT")
        self.assertLessEqual(result["resource_plan"]["workers"], 2)
        self.assertIn("E_RESOURCE_LIMIT", result["batch_summaries"][0]["business_codes"])
        self.assertTrue(result["batch_summaries"][0]["resource_decisions"])


if __name__ == "__main__":
    unittest.main()
