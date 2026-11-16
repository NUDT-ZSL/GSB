from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from regularization_lab.config import load_experiment
from regularization_lab.lab import ExperimentSession

from tests.helpers import ROOT


class NumericDriftTests(unittest.TestCase):
    def test_reference_difference_emits_numeric_drift_business_code(self) -> None:
        base = load_experiment(ROOT / "configs" / "experiment.yaml")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(
                base,
                optimizer=replace(base.optimizer, max_iterations=4, tolerance=1.0e9),
                runtime=replace(
                    base.runtime,
                    state_dir=root / "state",
                    report_path=root / "report.json",
                ),
                regularizers=[base.regularizers[0]],
            )
            first = ExperimentSession(config)
            result = first.run_scheme("ridge")
            assert result is not None
            self.assertTrue(result["cache_decision"]["cache_written"])

            reference_path = (
                root / "state" / "numeric_references" / f"{result['training_fingerprint']}.json"
            )
            reference = json.loads(reference_path.read_text(encoding="utf-8"))
            reference["result"]["loss_trajectory"][0]["total_loss"] += 0.01
            reference_path.write_text(json.dumps(reference), encoding="utf-8")
            for cache_file in (root / "state" / "cache").glob("*.json"):
                cache_file.unlink()

            second = ExperimentSession(config)
            replayed = second.run_scheme("ridge")
            assert replayed is not None
            self.assertIn("E_NUMERIC_DRIFT", {item["business_code"] for item in second.failures})
            self.assertFalse(replayed["numeric_check"]["within_tolerance"])
            report = second.write_report()
            self.assertIn("E_NUMERIC_DRIFT", report["business_codes"])


if __name__ == "__main__":
    unittest.main()
