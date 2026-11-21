from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from deploylab import Pipeline
from deploylab.bundle import write_bundle
from deploylab.errors import DeployError, E_UNATTRIBUTED
from deploylab.faults import FaultInjector


SPECS = [
    {
        "layer_id": "L00",
        "layer_shape": [8, 8],
        "quant_dtype": "int8",
        "scale_hint": "per_tensor:group=4",
        "calib_seed": 3101,
    },
    {
        "layer_id": "L01",
        "layer_shape": [6, 8],
        "quant_dtype": "int8",
        "scale_hint": "per_channel",
        "calib_seed": 3102,
    },
    {
        "layer_id": "L02",
        "layer_shape": [6, 6],
        "quant_dtype": "int4",
        "scale_hint": "per_channel:group=3",
        "calib_seed": 3103,
    },
    {
        "layer_id": "L03",
        "layer_shape": [4, 6],
        "quant_dtype": "int4",
        "scale_hint": "per_tensor",
        "calib_seed": 3104,
    },
]


class FaultInjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.bundle_path = cls.root / "bundle"
        write_bundle(cls.bundle_path, specs=SPECS, calib_samples=4)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def _inject(self, name: str, stage: str, kind: str, layer: str | None):
        db_path = self.root / f"{name}.sqlite"
        faults = FaultInjector.build(
            [{"stage": stage, "kind": kind, "layer_id": layer}]
        )
        pipeline = Pipeline(
            self.bundle_path,
            db_path,
            faults=faults,
            force_new=True,
        )
        with self.assertRaises(DeployError) as context:
            pipeline.run()
        return pipeline, context.exception

    def test_package_weight_truncation_is_attributed(self) -> None:
        pipeline, error = self._inject(
            "fault_package",
            "model_validation",
            "truncate_weight",
            "L02",
        )
        self.assertEqual(error.code, "E_PACKAGE_WEIGHT_TRUNCATED")
        self.assertEqual(error.stage, "model_validation")
        self.assertEqual(error.layer_id, "L02")
        self.assertTrue(error.injected)
        records = pipeline.store.recent_errors(3)
        self.assertEqual(records[0]["code"], "E_PACKAGE_WEIGHT_TRUNCATED")
        self.assertTrue(records[0]["injected"])
        self.assertEqual(pipeline.store.latest_version(), 0)

    def test_unknown_quant_dtype_is_attributed_to_quantization(self) -> None:
        pipeline, error = self._inject(
            "fault_quant",
            "quantization",
            "unknown_quant_dtype",
            "L01",
        )
        self.assertEqual(error.code, "E_QUANT_SCHEME")
        self.assertEqual(error.stage, "quantization")
        self.assertEqual(error.layer_id, "L01")
        self.assertEqual(error.field, "quant_dtype")
        self.assertTrue(error.injected)
        snapshots = pipeline.store.trace(limit=10)
        self.assertEqual([row["stage"] for row in reversed(snapshots)], ["model_validation"])

    def test_calibration_sample_mismatch_is_attributed(self) -> None:
        pipeline, error = self._inject(
            "fault_calib",
            "calibration",
            "calib_sample_mismatch",
            "L02",
        )
        self.assertEqual(error.code, "E_CALIB_SAMPLE_MISMATCH")
        self.assertEqual(error.stage, "calibration")
        self.assertEqual(error.layer_id, "L02")
        self.assertTrue(error.injected)
        stages = [
            row["stage"] for row in reversed(pipeline.store.trace(limit=10))
        ]
        self.assertEqual(stages, ["model_validation", "quantization"])

    def test_inference_dimension_mismatch_is_attributed(self) -> None:
        pipeline, error = self._inject(
            "fault_infer",
            "inference",
            "input_dim_mismatch",
            "L00",
        )
        self.assertEqual(error.code, "E_INFER_DIM_MISMATCH")
        self.assertEqual(error.stage, "inference")
        self.assertEqual(error.layer_id, "L00")
        self.assertTrue(error.injected)
        stages = [
            row["stage"] for row in reversed(pipeline.store.trace(limit=10))
        ]
        self.assertEqual(
            stages,
            ["model_validation", "quantization", "calibration"],
        )

    def test_unattributed_exception_is_counted(self) -> None:
        db_path = self.root / "unattributed.sqlite"
        pipeline = Pipeline(self.bundle_path, db_path, force_new=True)

        def explode(*_args, **_kwargs):
            raise RuntimeError("synthetic unattributed failure")

        pipeline._stage_validation = explode
        with self.assertRaises(DeployError) as context:
            pipeline.run()
        self.assertEqual(context.exception.code, E_UNATTRIBUTED)
        self.assertFalse(context.exception.injected)
        self.assertEqual(pipeline.store.unattributed_count(), 1)
        records = pipeline.store.recent_errors(2)
        self.assertFalse(records[0]["attributed"])


if __name__ == "__main__":
    unittest.main()

