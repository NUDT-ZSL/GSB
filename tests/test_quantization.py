from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from quantlab.config import WorkbenchConfig
from quantlab.deterministic import generate_manifest, generate_weights
from quantlab.errors import (
    BusinessError,
    E_QUANT_DTYPE,
    E_SCALE_INVALID,
    E_ZERO_POINT_RANGE,
)
from quantlab.pipeline import Workbench
from quantlab.quantization import (
    ROUNDING_MODE,
    quantize_layer,
    verify_dequantized_weights,
)

from tests.helpers import make_config


class QuantizationTests(unittest.TestCase):
    def test_quantization_is_byte_deterministic(self) -> None:
        config = WorkbenchConfig()
        entries = generate_manifest(config)
        entry = entries[0]
        weights_a = generate_weights(config, entry)
        weights_b = generate_weights(config, entry)
        record_a = quantize_layer(weights_a, entry)
        record_b = quantize_layer(weights_b, entry)
        self.assertEqual(record_a.dequantized.tobytes(), record_b.dequantized.tobytes())
        self.assertEqual(record_a.scales.tobytes(), record_b.scales.tobytes())
        self.assertEqual(record_a.codes.tobytes(), record_b.codes.tobytes())
        self.assertEqual(record_a.dequantized_checksum, record_b.dequantized_checksum)
        self.assertEqual(record_a.error["interval"][0] <= record_a.error["interval"][1], True)
        self.assertEqual(record_a.to_payload()["rounding"], ROUNDING_MODE)

    def test_fixed_point_can_be_independently_recomputed(self) -> None:
        config = WorkbenchConfig()
        entry = generate_manifest(config)[2]
        weights = generate_weights(config, entry)
        record = quantize_layer(weights, entry)
        verification = verify_dequantized_weights(record, weights)
        self.assertTrue(verification["codes_match"])
        self.assertTrue(verification["dequantized_from_codes_match"])
        self.assertTrue(verification["independent_recompute_match"])
        self.assertEqual(verification["max_abs_fixed_point_deviation"], 0.0)

    def test_unknown_quant_dtype_has_business_code_and_layer(self) -> None:
        config = WorkbenchConfig(manifest_overrides={1: {"quant_dtype": "float13"}})
        entry = generate_manifest(config)[1]
        with self.assertRaises(BusinessError) as captured:
            quantize_layer(generate_weights(config, entry), entry)
        error = captured.exception
        self.assertEqual(error.code, E_QUANT_DTYPE)
        self.assertEqual(error.layer_id, "layer_01")
        self.assertEqual(error.stage, "quantization")

    def test_non_positive_scale_has_business_code(self) -> None:
        config = WorkbenchConfig(manifest_overrides={0: {"scale_hint": 0.0}})
        entry = generate_manifest(config)[0]
        with self.assertRaises(BusinessError) as captured:
            quantize_layer(generate_weights(config, entry), entry)
        self.assertEqual(captured.exception.code, E_SCALE_INVALID)
        self.assertEqual(captured.exception.layer_id, "layer_00")

    def test_zero_point_out_of_range_has_business_code(self) -> None:
        config = WorkbenchConfig(manifest_overrides={2: {"zero_point": 999}})
        entry = generate_manifest(config)[2]
        with self.assertRaises(BusinessError) as captured:
            quantize_layer(generate_weights(config, entry), entry)
        self.assertEqual(captured.exception.code, E_ZERO_POINT_RANGE)
        self.assertEqual(captured.exception.layer_id, "layer_02")

    def test_pipeline_attributes_quantization_error_and_records_last_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = make_config(
                root,
                manifest_overrides={1: {"quant_dtype": "bad_dtype"}},
            )
            with self.assertRaises(BusinessError) as captured:
                Workbench(config).run(clean=True)
            self.assertEqual(captured.exception.code, E_QUANT_DTYPE)
            self.assertEqual(captured.exception.stage, "quantization")
            error_path = root / "state" / "last_error.json"
            self.assertTrue(error_path.exists())
            self.assertIn("E_QUANT_DTYPE", error_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
