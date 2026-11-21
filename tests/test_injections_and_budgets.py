from __future__ import annotations

import unittest

from quant_diag.config import EngineConfig, Injection
from quant_diag.errors import BusinessError

from tests.helpers import Workspace


class InjectionAndBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = Workspace()

    def tearDown(self) -> None:
        self.workspace.cleanup()

    def _assert_injection(
        self, kind: str, layer_id: str, code: str, stage: str
    ) -> None:
        engine = self.workspace.engine(
            f"runtime_inject_{kind}",
            config=EngineConfig(injections=(Injection(kind, layer_id),)),
        )
        with self.assertRaises(BusinessError) as raised:
            engine.run()
        error = raised.exception
        self.assertEqual(error.code, code)
        self.assertEqual(error.stage, stage)
        self.assertEqual(error.layer_id, layer_id)
        self.assertIsNotNone(error.layer_index)
        self.assertTrue(error.trace)

    def test_required_injections_return_business_codes(self) -> None:
        self._assert_injection(
            "weight_truncate", "layer_1", "E_WEIGHT_TRUNCATED", "layer_quantization"
        )
        self._assert_injection(
            "unknown_dtype", "layer_1", "E_INVALID_QUANT_DTYPE", "layer_quantization"
        )
        self._assert_injection(
            "calibration_count_mismatch",
            "layer_1",
            "E_CALIBRATION_SHAPE_MISMATCH",
            "calibration_statistics",
        )
        self._assert_injection(
            "inference_input_dim",
            "layer_0",
            "E_INPUT_DIM_MISMATCH",
            "layer_inference",
        )

    def test_tolerance_injection_returns_boundary_layer(self) -> None:
        engine = self.workspace.engine(
            "runtime_tolerance",
            config=EngineConfig(tolerance_abs=0.0, tolerance_rel=0.0),
        )
        with self.assertRaises(BusinessError) as raised:
            engine.run()
        error = raised.exception
        self.assertEqual(error.code, "E_TOLERANCE_EXCEEDED")
        self.assertEqual(error.stage, "accuracy_diagnostics")
        self.assertIsNotNone(error.layer_id)
        self.assertIn("violations", error.details)
        self.assertGreater(error.details["violations"][0]["abs_mean"], 0.0)

    def test_stage_layer_budget_returns_resource_limit(self) -> None:
        engine = self.workspace.engine(
            "runtime_layer_budget",
            config=EngineConfig(max_layers_per_stage=2),
        )
        with self.assertRaises(BusinessError) as raised:
            engine.run()
        self.assertEqual(raised.exception.code, "E_RESOURCE_LIMIT")
        self.assertEqual(raised.exception.stage, "model_package_validation")

    def test_resident_weight_budget_returns_resource_limit(self) -> None:
        engine = self.workspace.engine(
            "runtime_weight_budget",
            config=EngineConfig(max_resident_weight_mb=0.001),
        )
        with self.assertRaises(BusinessError) as raised:
            engine.run()
        self.assertEqual(raised.exception.code, "E_RESOURCE_LIMIT")
        self.assertEqual(raised.exception.stage, "layer_quantization")

    def test_calibration_comparison_budget_returns_resource_limit(self) -> None:
        engine = self.workspace.engine(
            "runtime_calibration_budget",
            config=EngineConfig(max_calibration_comparisons=10),
        )
        with self.assertRaises(BusinessError) as raised:
            engine.run()
        self.assertEqual(raised.exception.code, "E_RESOURCE_LIMIT")
        self.assertEqual(raised.exception.stage, "calibration_statistics")

    def test_memory_pressure_degrades_to_grouped_quantization(self) -> None:
        config = EngineConfig(
            max_resident_weight_mb=0.075,
            tolerance_abs=10.0,
            tolerance_rel=10.0,
        )
        engine = self.workspace.engine("runtime_degrade", config=config)
        report = engine.run()
        snapshot = engine.latest_snapshot()
        self.assertTrue(report["ok"])
        self.assertTrue(snapshot["degradations"])
        self.assertEqual(
            snapshot["degradations"][0]["category"], "grouped_quantization"
        )
        self.assertEqual(
            snapshot["stages"]["layer_quantization"]["layers"]["layer_0"]["params"][
                "group_size"
            ],
            8,
        )


if __name__ == "__main__":
    unittest.main()
