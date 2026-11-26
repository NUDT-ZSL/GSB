from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from diagnostic_engine.engine import run_engine
from diagnostic_engine.generator import generate_dataset
from diagnostic_engine.utils import canonical_json_bytes


class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.generated = generate_dataset(cls.root / "data", sample_count=128)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def _run(self, name: str, **kwargs: object) -> dict:
        return run_engine(
            data_path=self.generated.samples_path,
            snapshot_path=self.generated.v2_path,
            output_directory=self.root / name,
            precisions=("float64", "float32", "fixed"),
            **kwargs,
        )

    def test_three_precision_paths_and_diagnostics(self) -> None:
        report = self._run("normal")
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["validation"]["status"], "passed")
        reference_steps = report["reference_whole"]["run"]["steps"]
        self.assertEqual(len(reference_steps), 5)
        strengths = [step["strength"] for step in reference_steps]
        self.assertEqual(strengths, sorted(strengths, reverse=True))
        flags = set()
        for precision in ("float64", "float32", "fixed"):
            payload = report["precisions"][precision]
            steps = payload["run"]["steps"]
            self.assertEqual(len(steps), 5)
            self.assertEqual(
                payload["run"]["block_plan"]["decision_sha256"],
                report["blocking"]["decision"]["decision_sha256"],
            )
            for step in steps:
                self.assertLessEqual(step["objective_gap"], 3e-2)
            flags.update(step["converged"] for step in steps)
            precision_info = payload["run"]["precision_config"]
            if precision == "fixed":
                self.assertEqual(precision_info["storage_dtype"], "int64")
                self.assertEqual(precision_info["fixed_scale"], 8192)
                self.assertEqual(
                    precision_info["rounding_mode"], "nearest_even"
                )
                self.assertIsNotNone(
                    payload["run"]["integer_matrix_sha256"]
                )
        self.assertLessEqual(len(flags), 1)
        self.assertEqual(
            {step["converged"] for step in reference_steps}, flags
        )
        for precision in ("float64", "float32", "fixed"):
            diagnostic = report["diagnostics"][precision]
            self.assertEqual(
                [level["level"] for level in diagnostic["levels"]],
                [
                    "feature_scaling",
                    "block_accumulation",
                    "fixed_quantization",
                    "gradient_update",
                ],
            )
            self.assertIn(
                diagnostic["dominant_level"],
                [level["level"] for level in diagnostic["levels"]],
            )
            self.assertEqual(
                diagnostic["block_decision_sha256"],
                report["blocking"]["decision"]["decision_sha256"],
            )
        predictions = Path(report["inference"]["predictions_path"])
        self.assertTrue(predictions.exists())
        self.assertEqual(
            len(predictions.read_text(encoding="utf-8").splitlines()),
            128,
        )

    def test_run_is_replayable_and_resume_reuses_steps(self) -> None:
        first = self._run("replay")
        second = run_engine(
            data_path=self.generated.samples_path,
            snapshot_path=self.generated.v2_path,
            output_directory=self.root / "replay",
            precisions=("float64", "float32", "fixed"),
        )
        self.assertEqual(first["run_id"], second["run_id"])
        for precision in ("float64", "float32", "fixed"):
            self.assertEqual(second["precisions"][precision]["resume"]["reused_steps"], 5)
            self.assertEqual(
                second["precisions"][precision]["resume"]["recomputed_steps"], 0
            )
        blocking_first = canonical_json_bytes(first["blocking"]["decision"])
        blocking_second = canonical_json_bytes(second["blocking"]["decision"])
        self.assertEqual(blocking_first, blocking_second)

    def test_snapshot_v1_and_v2_produce_same_run_id(self) -> None:
        v2 = self._run("v2-run")
        v1 = run_engine(
            data_path=self.generated.samples_path,
            snapshot_path=self.generated.v1_path,
            output_directory=self.root / "v1-run",
            precisions=("float64", "float32", "fixed"),
        )
        self.assertEqual(v1["run_id"], v2["run_id"])
        self.assertEqual(
            v1["precisions"]["fixed"]["run"]["steps"][-1]["objective"],
            v2["precisions"]["fixed"]["run"]["steps"][-1]["objective"],
        )


if __name__ == "__main__":
    unittest.main()

