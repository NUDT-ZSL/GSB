from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from deploylab.bundle import write_bundle
from deploylab.config import LabConfig
from deploylab.webapp import AppContext


SPECS = [
    {
        "layer_id": "L00",
        "layer_shape": [8, 8],
        "quant_dtype": "int8",
        "scale_hint": "per_tensor:group=4",
        "calib_seed": 5101,
    },
    {
        "layer_id": "L01",
        "layer_shape": [6, 8],
        "quant_dtype": "int8",
        "scale_hint": "per_channel",
        "calib_seed": 5102,
    },
    {
        "layer_id": "L02",
        "layer_shape": [6, 6],
        "quant_dtype": "int4",
        "scale_hint": "per_channel:group=3",
        "calib_seed": 5103,
    },
    {
        "layer_id": "L03",
        "layer_shape": [4, 6],
        "quant_dtype": "int4",
        "scale_hint": "per_tensor",
        "calib_seed": 5104,
    },
]


class WebAppContextTests(unittest.TestCase):
    def test_startup_injection_busy_revision_and_calibration(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = root / "bundle"
            write_bundle(bundle, specs=SPECS, calib_samples=4)
            context = AppContext(
                bundle_path=bundle,
                db_path=root / "app.sqlite",
                config=LabConfig(calib_samples=4),
            )
            state = context.state()
            self.assertTrue(state["state"]["completed"])
            self.assertEqual(len(state["layers"]), 4)
            self.assertEqual(state["state"]["progress_version"], 4)
            injection = context.inject(
                {
                    "stage": "quantization",
                    "kind": "unknown_quant_dtype",
                    "layer_id": "L02",
                }
            )
            self.assertFalse(injection["ok"])
            self.assertEqual(
                injection["operation"]["error"]["code"], "E_QUANT_SCHEME"
            )
            busy = context.concurrency_probe()
            self.assertEqual(busy["observed"]["code"], "E_STAGE_BUSY")
            revision = context.revision({"layer_id": "L02"})
            self.assertTrue(revision["ok"])
            self.assertEqual(
                revision["result"]["invalidation"]["affected_layers"],
                ["L02", "L03"],
            )
            calibration = context.calibrate({"layer_id": "L01"})
            self.assertTrue(calibration["ok"])
            self.assertEqual(
                calibration["result"]["invalidation"]["reused_layers"],
                ["L00"],
            )
            state = context.state()
            self.assertEqual(state["state"]["generation"], 3)
            self.assertTrue(state["state"]["completed"])
            restarted = AppContext(
                bundle_path=bundle,
                db_path=root / "app.sqlite",
                config=LabConfig(calib_samples=4),
            )
            self.assertEqual(restarted.bundle_path, context.bundle_path)
            self.assertTrue(
                restarted.calibrate({"layer_id": "L01"})["ok"]
            )


if __name__ == "__main__":
    unittest.main()

