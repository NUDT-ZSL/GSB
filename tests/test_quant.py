from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np

from deploylab.bundle import generate_weight, write_bundle
from deploylab.errors import DeployError, E_QUANT_SCHEME
from deploylab.quant import (
    parse_quant_scheme,
    quantize_array,
    verify_dequant_with_original,
)


SPECS = [
    {
        "layer_id": "L00",
        "layer_shape": [8, 8],
        "quant_dtype": "int8",
        "scale_hint": "per_tensor:group=4",
        "calib_seed": 11,
    },
    {
        "layer_id": "L01",
        "layer_shape": [6, 8],
        "quant_dtype": "int8",
        "scale_hint": "per_channel:group=3",
        "calib_seed": 12,
    },
    {
        "layer_id": "L02",
        "layer_shape": [6, 6],
        "quant_dtype": "int4",
        "scale_hint": "per_channel",
        "calib_seed": 13,
    },
    {
        "layer_id": "L03",
        "layer_shape": [4, 6],
        "quant_dtype": "int4",
        "scale_hint": "per_tensor",
        "zero_point": 1,
        "calib_seed": 14,
    },
]


class QuantizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name) / "bundle"
        cls.bundle = write_bundle(cls.root, specs=SPECS, calib_samples=3)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def test_all_schemes_are_deterministic_and_replayable(self) -> None:
        seen: set[tuple[str, str]] = set()
        for layer in self.bundle.layers:
            weight = generate_weight(layer)
            scheme = parse_quant_scheme(layer)
            q1, dq1, scales1, meta1 = quantize_array(weight, scheme)
            q2, dq2, scales2, meta2 = quantize_array(weight, scheme)
            self.assertEqual(q1.tobytes(), q2.tobytes())
            self.assertEqual(dq1.tobytes(), dq2.tobytes())
            self.assertEqual(scales1, scales2)
            self.assertEqual(meta1, meta2)
            seen.add((layer.quant_dtype, scheme.granularity))
            self.assertTrue(
                verify_dequant_with_original(layer, weight, {"quant": meta1})
            )
        self.assertEqual(
            seen,
            {
                ("int8", "per_tensor"),
                ("int8", "per_channel"),
                ("int4", "per_channel"),
                ("int4", "per_tensor"),
            },
        )

    def test_group_scales_reduce_quantization_error(self) -> None:
        layer = self.bundle.layers[0]
        weight = generate_weight(layer)
        grouped = parse_quant_scheme(layer)
        _, _, _, grouped_meta = quantize_array(weight, grouped)
        ungrouped = parse_quant_scheme(replace(layer, scale_hint="per_tensor"))
        _, _, _, ungrouped_meta = quantize_array(weight, ungrouped)
        self.assertGreater(ungrouped_meta["scale_count"], 0)
        self.assertGreater(grouped_meta["scale_count"], ungrouped_meta["scale_count"])
        self.assertLessEqual(
            float.fromhex(grouped_meta["mse_hex"]),
            float.fromhex(ungrouped_meta["mse_hex"]),
        )

    def test_invalid_dtype_reports_layer_and_field(self) -> None:
        layer = replace(self.bundle.layers[0], quant_dtype="int3")
        with self.assertRaises(DeployError) as context:
            parse_quant_scheme(layer)
        error = context.exception
        self.assertEqual(error.code, E_QUANT_SCHEME)
        self.assertEqual(error.layer_id, "L00")
        self.assertEqual(error.field, "quant_dtype")

    def test_invalid_scale_hint_reports_layer_and_field(self) -> None:
        layer = replace(self.bundle.layers[1], scale_hint="per_block")
        with self.assertRaises(DeployError) as context:
            parse_quant_scheme(layer)
        error = context.exception
        self.assertEqual(error.code, E_QUANT_SCHEME)
        self.assertEqual(error.layer_id, "L01")
        self.assertEqual(error.field, "scale_hint")

    def test_invalid_zero_point_reports_layer_and_field(self) -> None:
        layer = replace(self.bundle.layers[2], zero_point=7)
        with self.assertRaises(DeployError) as context:
            parse_quant_scheme(layer)
        error = context.exception
        self.assertEqual(error.code, E_QUANT_SCHEME)
        self.assertEqual(error.layer_id, "L02")
        self.assertEqual(error.field, "zero_point")


if __name__ == "__main__":
    unittest.main()

