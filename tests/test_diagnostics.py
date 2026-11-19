from __future__ import annotations

import tempfile
import unittest

from numeric_workbench.diagnostics import REQUIRED_STAGE_NAMES, build_diagnostics
from tests.helpers import config_with, make_dataset


class DiagnosticsTests(unittest.TestCase):
    def test_error_spectrum_has_stage_metrics_and_chunk_round_details(self):
        with tempfile.TemporaryDirectory() as directory:
            _, dataset, split = make_dataset(directory, rows=120)
            config = config_with(
                max_iterations=5,
                convergence_tolerance=1e-9,
                min_improvement=0.0,
                chunk_size=24,
            )
            result = build_diagnostics(dataset, split, config, verify_recompute=True)
            spectra = result.payload["spectra"]
            for precision in ("float64", "float32", "fixed_point"):
                stages = spectra[precision]["stages"]
                for stage_name in REQUIRED_STAGE_NAMES:
                    self.assertIn(stage_name, stages)
                for stage_name, stage in stages.items():
                    if not stage["applicable"]:
                        continue
                    metrics = stage["metrics_vs_reference"]
                    self.assertIn("absolute", metrics)
                    self.assertIn("relative", metrics)
                    self.assertIn("error_amplification_ratio", metrics)
                    self.assertTrue(stage["round_chunk_details"])
                    for detail in stage["round_chunk_details"]:
                        self.assertIn("round", detail)
                        self.assertIn("chunk_index", detail)
                        self.assertIn("row_ids", detail)
                        self.assertIn("gradient_abs_deviation_by_parameter", detail)
            dominant = result.payload["dominant_source"]
            self.assertIn(dominant["stage"], REQUIRED_STAGE_NAMES + ("arithmetic_precision",))
            self.assertIn("detail_location", dominant)
            self.assertGreater(dominant["detail_count"], 0)
            self.assertTrue(result.payload["sample_index_checks"]["consistent"])
            self.assertTrue(
                result.payload["verification"]["trajectory_recompute_match"]
            )
            self.assertTrue(
                result.payload["verification"]["error_spectrum_recompute_match"]
            )
            repeatability = result.payload["verification"]["repeatability_by_precision"]
            for tier in ("float64", "float32", "fixed_point"):
                self.assertTrue(repeatability[tier]["chunked"]["byte_identical"])
                self.assertTrue(repeatability[tier]["full"]["byte_identical"])
            self.assertTrue(
                repeatability["fixed_point"]["fixed_replay_recomputable"]
            )
            contract = result.payload["precision_switch"]["contract"]
            self.assertTrue(contract["chunk_plan_identical"])
            self.assertTrue(contract["train_indexes_identical"])
            self.assertTrue(contract["validation_indexes_identical"])


if __name__ == "__main__":
    unittest.main()
