from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from quantlab.pipeline import ForcedTermination, Workbench

from tests.helpers import make_config


class InvalidationTests(unittest.TestCase):
    def test_revision_change_recomputes_only_downstream_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = make_config(root)
            first = Workbench(base).run(clean=True)
            old_prefix = first["inference"]["layers"][0]["activation_checksum"]
            revision_config = base.clone(revision_overrides={1: 2})
            updated = Workbench(revision_config).run(reuse_previous=True)
            invalidation = updated["execution"]["invalidation"]
            self.assertTrue(invalidation["triggers"]["weight_revision"])
            self.assertEqual(
                invalidation["affected_layers"],
                ["layer_01", "layer_02", "layer_03"],
            )
            self.assertEqual(
                invalidation["recomputed_layers"],
                ["layer_01", "layer_02", "layer_03"],
            )
            self.assertEqual(invalidation["reused_layers"], ["layer_00"])
            self.assertTrue(invalidation["disjoint"])
            self.assertTrue(invalidation["union_equals_all_layers"])
            self.assertEqual(
                updated["execution"]["reused_layers_by_stage"]["quantization"],
                ["layer_00"],
            )
            self.assertEqual(
                updated["execution"]["recomputed_layers_by_stage"]["quantization"],
                ["layer_01", "layer_02", "layer_03"],
            )
            self.assertEqual(
                updated["inference"]["layers"][0]["activation_checksum"],
                old_prefix,
            )
            self.assertGreater(
                updated["progress"]["latest"]["version"],
                first["progress"]["latest"]["version"],
            )
            self.assertTrue(updated["consistency"]["ok"])

    def test_calibration_seed_change_reuses_weights_but_recomputes_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = make_config(root)
            first = Workbench(base).run(clean=True)
            old_quant = [
                item["dequantized_checksum"] for item in first["quantization"]["layers"]
            ]
            changed = Workbench(
                base.clone(calib_seed_overrides={2: 999_001})
            ).run(reuse_previous=True)
            invalidation = changed["execution"]["invalidation"]
            self.assertFalse(invalidation["triggers"]["weight_revision"])
            self.assertTrue(invalidation["triggers"]["calibration"])
            self.assertEqual(invalidation["affected_layers"], ["layer_02", "layer_03"])
            self.assertEqual(
                changed["execution"]["recomputed_layers_by_stage"]["quantization"],
                [],
            )
            self.assertEqual(
                [item["dequantized_checksum"] for item in changed["quantization"]["layers"]],
                old_quant,
            )
            self.assertEqual(
                changed["execution"]["recomputed_layers_by_stage"]["calibration"],
                ["layer_02", "layer_03"],
            )
            self.assertEqual(
                changed["execution"]["reused_layers_by_stage"]["calibration"],
                ["layer_00", "layer_01"],
            )

    def test_invalidation_after_partial_inference_discards_unavailable_tail(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = make_config(root)
            with self.assertRaises(ForcedTermination):
                Workbench(base).run(
                    clean=True,
                    interrupt_after_inference_layer=1,
                )
            updated = Workbench(
                base.clone(revision_overrides={3: 2})
            ).run(reuse_previous=True)
            invalidation = updated["execution"]["invalidation"]
            self.assertTrue(invalidation["disjoint"])
            self.assertTrue(invalidation["union_equals_all_layers"])
            self.assertEqual(
                updated["execution"]["reused_layers_by_stage"]["inference"],
                ["layer_00", "layer_01"],
            )
            self.assertEqual(
                updated["execution"]["recomputed_layers_by_stage"]["inference"],
                ["layer_02", "layer_03"],
            )
            self.assertTrue(updated["consistency"]["ok"])


if __name__ == "__main__":
    unittest.main()
