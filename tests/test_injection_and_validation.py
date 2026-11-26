from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from diagnostic_engine.blocking import decide_block_plan, whole_block_plan
from diagnostic_engine.data import load_jsonl_dataset, prepare_features
from diagnostic_engine.engine import run_engine, train_precision
from diagnostic_engine.errors import DiagnosticError
from diagnostic_engine.generator import generate_dataset
from diagnostic_engine.injection import InjectionController
from diagnostic_engine.solver import PathRun
from diagnostic_engine.snapshots import parse_v1
from diagnostic_engine.validation import validate_results


class InjectionAndValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.generated = generate_dataset(cls.root / "data", sample_count=112)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def test_four_stage_injection_degrades_and_validates(self) -> None:
        report = run_engine(
            data_path=self.generated.samples_path,
            snapshot_path=self.generated.v2_path,
            output_directory=self.root / "injected",
            precisions=("float64", "float32", "fixed"),
            injections=[
                "feature_standardization:row_index=3,column=region,value=__OUT_OF_VOCAB__",
                "path_solve:index=2,value=0,fallback=0.01",
                "block_reduce:value=0",
                "result_validation:value=0",
            ],
        )
        self.assertEqual(report["status"], "degraded")
        self.assertEqual(report["validation"]["status"], "passed")
        self.assertEqual(
            set(report["degradation_categories"]),
            {
                "unknown_category_zero_fill",
                "regularization_strength_clamped",
                "auto_block_plan",
                "default_validation_tolerance",
            },
        )
        expected = {
            "feature_standardization": "E_CATEGORY_OUT_OF_RANGE",
            "path_solve": "E_REG_STRENGTH_NONPOSITIVE",
            "block_reduce": "E_BLOCK_SIZE_ZERO",
            "result_validation": "E_TOLERANCE_NONPOSITIVE",
        }
        seen = {}
        for issue in report["injections"]:
            self.assertIn(issue["stage"], expected)
            self.assertIn("degrade_category", issue)
            seen[issue["stage"]] = issue["code"]
        self.assertEqual(seen, expected)
        reference_flags = [
            step["converged"]
            for step in report["reference_whole"]["run"]["steps"]
        ]
        for precision in ("float64", "float32", "fixed"):
            steps = report["precisions"][precision]["run"]["steps"]
            self.assertEqual([step["converged"] for step in steps], reference_flags)

    def test_parameter_triggered_degradation(self) -> None:
        source = json.loads(Path(self.generated.v1_path).read_text(encoding="utf-8"))
        source["block_size"] = 0
        source["tolerance"] = 0
        source["strength_path"][1] = -0.25
        modified = self.root / "parameter-injected.json"
        modified.write_text(json.dumps(source), encoding="utf-8")
        report = run_engine(
            data_path=self.generated.samples_path,
            snapshot_path=modified,
            output_directory=self.root / "parameter-injected",
            precisions=("float64", "fixed"),
        )
        self.assertEqual(report["status"], "degraded")
        self.assertIn("auto_block_plan", report["degradation_categories"])
        self.assertIn(
            "regularization_strength_clamped", report["degradation_categories"]
        )
        self.assertIn(
            "default_validation_tolerance", report["degradation_categories"]
        )
        self.assertEqual(report["validation"]["status"], "passed")

    def test_inconsistent_result_reports_first_divergence(self) -> None:
        config = parse_v1(self.generated.v1_path)
        dataset = load_jsonl_dataset(self.generated.samples_path, config)
        controller = InjectionController()
        prepared = prepare_features(dataset, config, controller)
        block_plan = decide_block_plan(
            sample_count=dataset.sample_count,
            feature_count=prepared.matrix.shape[1],
            config=config,
            injector=controller,
        )
        whole_plan = whole_block_plan(
            sample_count=dataset.sample_count,
            feature_count=prepared.matrix.shape[1],
            config=config,
        )
        strengths = list(config.strength_path)
        reference = train_precision(
            matrix=prepared.matrix,
            labels=dataset.labels,
            config=config,
            precision="float64",
            block_plan=whole_plan,
            whole_plan=whole_plan,
            strengths=strengths,
            data_sha256="test-data",
            resume=False,
        )
        observed = train_precision(
            matrix=prepared.matrix,
            labels=dataset.labels,
            config=config,
            precision="float64",
            block_plan=block_plan,
            whole_plan=whole_plan,
            strengths=strengths,
            data_sha256="test-data",
            resume=False,
        )
        bad_steps = list(observed.run.steps)
        bad_steps[1] = replace(
            bad_steps[1], objective=bad_steps[1].objective + 1.0
        )
        bad_run = PathRun(
            precision=observed.run.precision,
            steps=bad_steps,
            coefficients=observed.run.coefficients,
            intercept=observed.run.intercept,
            matrix=observed.run.matrix,
            integer_matrix=observed.run.integer_matrix,
            block_plan=observed.run.block_plan,
            whole_plan=observed.run.whole_plan,
        )
        with self.assertRaises(DiagnosticError) as context:
            validate_results(
                config=config,
                dataset=dataset,
                prepared=prepared,
                labels=dataset.labels,
                runs={"float64": bad_run},
                reference=reference.run,
                block_plan=block_plan,
                injection_issues=[],
                tolerance=config.objective_tolerance,
            )
        error = context.exception
        self.assertEqual(error.code, "E_INCONSISTENT")
        self.assertIsNotNone(error.first_divergence)
        self.assertEqual(
            error.first_divergence["path_index"],
            1,
        )
        self.assertIn("field", error.first_divergence)


if __name__ == "__main__":
    unittest.main()

