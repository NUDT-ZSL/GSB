from __future__ import annotations

import unittest

import numpy as np

from quant_diag.config import EngineConfig
from quant_diag.errors import E_LAYER_INCOMPLETE, BusinessError
from quant_diag.inference import infer_one_layer
from quant_diag.quant import dequantize_layer

from tests.helpers import Workspace


class QuantizationDeterminismTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = Workspace()

    def tearDown(self) -> None:
        self.workspace.cleanup()

    def test_quant_dequant_and_full_run_are_byte_deterministic(self) -> None:
        first = self.workspace.engine("runtime_same_a")
        second = self.workspace.engine("runtime_same_b")
        first_report = first.run()
        second_report = second.run()
        first_snapshot = first.latest_snapshot()
        second_snapshot = second.latest_snapshot()
        self.assertEqual(first_report["result_digest"], second_report["result_digest"])
        self.assertEqual(
            first_snapshot["content_checksum"], second_snapshot["content_checksum"]
        )
        first_quant = first_snapshot["stages"]["layer_quantization"]["layers"]
        second_quant = second_snapshot["stages"]["layer_quantization"]["layers"]
        for layer_id in first_snapshot["layer_order"]:
            self.assertEqual(first_quant[layer_id]["codes"], second_quant[layer_id]["codes"])
            self.assertEqual(
                first_quant[layer_id]["dequant_weights"],
                second_quant[layer_id]["dequant_weights"],
            )
            np.testing.assert_array_equal(
                dequantize_layer(first_quant[layer_id], scale_mode="stored"),
                np.asarray(
                    first_quant[layer_id]["dequant_weights"], dtype=np.float64
                ).reshape(tuple(first_quant[layer_id]["layer_shape"])),
            )

    def test_missing_quant_layer_returns_business_code_and_layer(self) -> None:
        engine = self.workspace.engine("runtime_missing")
        engine.run()
        snapshot = engine.latest_snapshot()
        layer = engine.manifest.layers[1]
        with self.assertRaises(BusinessError) as raised:
            infer_one_layer(
                {},
                layer,
                np.zeros(layer.input_dim, dtype=np.float64),
                EngineConfig(),
            )
        error = raised.exception
        self.assertEqual(error.code, E_LAYER_INCOMPLETE)
        self.assertEqual(error.stage, "layer_inference")
        self.assertEqual(error.layer_id, layer.layer_id)
        self.assertEqual(error.layer_index, layer.layer_index)

    def test_stage_views_have_identical_layer_sets(self) -> None:
        engine = self.workspace.engine("runtime_stage_sets")
        report = engine.run()
        snapshot = engine.latest_snapshot()
        expected = set(snapshot["layer_order"])
        for stage in (
            "model_package_validation",
            "layer_quantization",
            "calibration_statistics",
            "layer_inference",
        ):
            self.assertEqual(
                set(snapshot["stages"][stage]["completed_layers"]), expected
            )
        self.assertTrue(report["self_check"]["passed"])
        self.assertEqual(snapshot["stage"], "layer_inference")


if __name__ == "__main__":
    unittest.main()
