from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from binning_engine.pipeline import run_pipeline
from binning_engine.reference import reference_encode


FIXTURES = ROOT / "fixtures"


def tiny_config(**overrides):
    config = {
        "version": 1,
        "config_name": "tiny",
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
        "priority": ["quality", "memory", "latency"],
    }
    config.update(overrides)
    return config


def tiny_rows():
    return [
        {"row_id": 4, "x": 3.0, "c": "b", "y": 1},
        {"row_id": 1, "x": 0.0, "c": "a", "y": 0},
        {"row_id": 3, "x": 2.0, "c": "b", "y": 1},
        {"row_id": 2, "x": 1.0, "c": "a", "y": 0},
    ]


class BinningEncodingTests(unittest.TestCase):
    def test_equal_width_reference_vectors(self):
        result = reference_encode(tiny_rows(), tiny_config())
        self.assertEqual(result["boundaries"]["columns"]["x"]["boundaries"], [1.5])
        vectors = [row["feature_vector"] for row in result["rows"]]
        self.assertEqual(
            vectors,
            [[1.0, 0.0], [1.0, 0.0], [0.0, 1.0], [0.0, 1.0]],
        )
        self.assertEqual([row["row_id"] for row in result["rows"]], [1, 2, 3, 4])

    def test_target_encoding_and_quantization(self):
        config = tiny_config(
            encoding={"method": "target_encoding", "smoothing": 0.0},
            quantization={"default": {"quant_scale": 0.25, "zero_point": 0}},
        )
        result = reference_encode(tiny_rows(), config)
        self.assertEqual(
            [row["feature_vector"][0] for row in result["rows"]],
            [0.0, 0.0, 1.0, 1.0],
        )
        self.assertEqual(
            [row["quantized_vector"][0] for row in result["rows"]],
            [0, 0, 4, 4],
        )
        self.assertIn("observed_abs_max", result["error_interval"])

    def test_equal_freq_and_lexicographic_column_order(self):
        rows = [
            {"row_id": i, "x": float(i), "z": float(10 - i), "c": "a", "y": i % 2}
            for i in range(8)
        ]
        config = tiny_config(
            numeric_columns=["z", "x"],
            bins={
                "x": {"bin_count": 2, "bin_method": "equal_freq"},
                "z": {"bin_count": 2, "bin_method": "equal_freq"},
            },
        )
        result = reference_encode(rows, config)
        self.assertEqual(result["boundaries"]["column_order"], ["x", "z"])
        self.assertEqual(result["boundaries"]["columns"]["x"]["boundaries"], [3.5])

    def test_precision_tier_changes_propagation(self):
        rows = tiny_rows()
        smoothing = 10.0
        values = {}
        for tier in ("float64", "float32", "int8"):
            result = reference_encode(
                rows,
                tiny_config(
                    precision_tier=tier,
                    encoding={"method": "target_encoding", "smoothing": smoothing},
                    quantization={
                        "default": {"quant_scale": 0.03125, "zero_point": 0}
                    },
                ),
            )
            values[tier] = result["rows"][0]["feature_vector"][0]
        self.assertNotEqual(values["float64"], values["float32"])
        self.assertNotEqual(values["float64"], values["int8"])


class PipelineParallelTests(unittest.TestCase):
    def test_serial_and_process_pool_outputs_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            common = dict(
                config=FIXTURES / "config" / "engine_config.json",
                samples_path=FIXTURES / "samples.jsonl",
            )
            serial = run_pipeline(
                **common,
                output_dir=root / "serial",
                workers=1,
                use_process_pool=False,
            )
            parallel = run_pipeline(
                **common,
                output_dir=root / "parallel",
                workers=2,
                batch_size=24,
                use_process_pool=True,
            )
            serial_bytes = (root / "serial" / "encoding_result.json").read_bytes()
            parallel_bytes = (root / "parallel" / "encoding_result.json").read_bytes()
            self.assertEqual(serial_bytes, parallel_bytes)
            self.assertTrue(parallel["scheduler_order"]["dispatch_matches_merge"])
            self.assertTrue(parallel["self_check"]["ok"])
            self.assertEqual(
                len(parallel["encoding_result"]["rows"]),
                serial["statistics"]["row_count"],
            )

    def test_warm_cache_reuses_all_stage_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "run"
            kwargs = dict(
                config=FIXTURES / "config" / "engine_config.json",
                samples_path=FIXTURES / "samples.jsonl",
                output_dir=output,
                workers=1,
                use_process_pool=False,
            )
            first = run_pipeline(**kwargs)
            second = run_pipeline(**kwargs)
            self.assertTrue(first["self_check"]["ok"])
            self.assertGreaterEqual(second["cache_events"]["hits"], 4)
            self.assertEqual(second["cache_events"]["recomputed_keys"], [])
            self.assertTrue(second["self_check"]["ok"])

    def test_v1_flat_config_matches_normalized_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_pipeline(
                FIXTURES / "config" / "engine_config.json",
                samples_path=FIXTURES / "samples.jsonl",
                output_dir=root / "v2",
                workers=1,
                use_process_pool=False,
            )
            run_pipeline(
                FIXTURES / "config" / "binning_config.v1.json",
                samples_path=FIXTURES / "samples.jsonl",
                output_dir=root / "v1",
                workers=1,
                use_process_pool=False,
            )
            self.assertEqual(
                (root / "v1" / "encoding_result.json").read_bytes(),
                (root / "v2" / "encoding_result.json").read_bytes(),
            )


if __name__ == "__main__":
    unittest.main()
