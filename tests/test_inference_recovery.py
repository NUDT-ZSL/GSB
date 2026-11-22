from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from quantlab.errors import BusinessError, E_NO_CHECKPOINT
from quantlab.inference import NORMALIZATION_SCOPE, REDUCTION_ORDER
from quantlab.pipeline import ForcedTermination, Workbench

from tests.helpers import make_config


class InferenceRecoveryTests(unittest.TestCase):
    def test_two_fresh_runs_are_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = Workbench(make_config(root / "a")).run(clean=True)
            second = Workbench(make_config(root / "b")).run(clean=True)
            self.assertEqual(
                [layer["activation_checksum"] for layer in first["inference"]["layers"]],
                [layer["activation_checksum"] for layer in second["inference"]["layers"]],
            )
            self.assertEqual(
                first["inference"]["final_prediction_checksum"],
                second["inference"]["final_prediction_checksum"],
            )
            self.assertEqual(
                first["inference"]["final_prediction"],
                second["inference"]["final_prediction"],
            )
            self.assertIn("output_block_ascending", first["inference"]["reduction_order"])
            self.assertEqual(
                first["inference"]["reduction_order"],
                REDUCTION_ORDER,
            )
            self.assertEqual(
                first["inference"]["normalization_scope"],
                NORMALIZATION_SCOPE,
            )

    def test_resume_after_forced_termination_matches_full_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = make_config(root)
            full = Workbench(config).run(clean=True)
            with self.assertRaises(ForcedTermination):
                Workbench(config).run(
                    clean=True,
                    interrupt_after_inference_layer=1,
                )
            resumed = Workbench(config).run(resume=True)
            self.assertEqual(
                [layer["activation_checksum"] for layer in full["inference"]["layers"]],
                [layer["activation_checksum"] for layer in resumed["inference"]["layers"]],
            )
            self.assertEqual(
                full["inference"]["final_prediction"],
                resumed["inference"]["final_prediction"],
            )
            self.assertEqual(
                full["inference"]["final_prediction_checksum"],
                resumed["inference"]["final_prediction_checksum"],
            )
            self.assertEqual(
                resumed["execution"]["reused_layers_by_stage"]["inference"],
                ["layer_00", "layer_01"],
            )
            self.assertEqual(
                resumed["execution"]["recomputed_layers_by_stage"]["inference"],
                ["layer_02", "layer_03"],
            )
            self.assertEqual(resumed["recovery"]["mode"], "resume")
            self.assertTrue(resumed["consistency"]["ok"])

    def test_resume_without_checkpoint_returns_business_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaises(BusinessError) as captured:
                Workbench(make_config(root)).run(resume=True)
            self.assertEqual(captured.exception.code, E_NO_CHECKPOINT)
            self.assertEqual(captured.exception.stage, "resume")

    def test_completed_snapshot_resume_reuses_all_layers(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = make_config(root)
            full = Workbench(config).run(clean=True)
            resumed = Workbench(config).run(resume=True)
            self.assertEqual(
                full["inference"]["final_prediction_checksum"],
                resumed["inference"]["final_prediction_checksum"],
            )
            self.assertEqual(
                resumed["execution"]["recomputed_layers_by_stage"]["inference"],
                [],
            )
            self.assertEqual(
                resumed["execution"]["reused_layers_by_stage"]["inference"],
                ["layer_00", "layer_01", "layer_02", "layer_03"],
            )


if __name__ == "__main__":
    unittest.main()
