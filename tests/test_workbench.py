from __future__ import annotations

import json
import contextlib
import io
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from numeric_workbench.config import TrainingConfig
from numeric_workbench.data import (
    generate_deterministic_samples,
    load_dataset,
    make_stratified_split,
)
from numeric_workbench.errors import WorkbenchError
from numeric_workbench.reporting import resume_job, run_job
from numeric_workbench.training import train_prepared
from numeric_workbench.workbench import Workbench
from tests.helpers import config_with, make_dataset, prepare_for


class WorkbenchTests(unittest.TestCase):
    def test_complete_job_writes_report_snapshot_and_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sample_path = root / "samples" / "samples.jsonl"
            generate_deterministic_samples(sample_path, row_count=120)
            config = config_with(
                job_id="complete-job",
                max_iterations=5,
                convergence_tolerance=1e-9,
                min_improvement=0.0,
                chunk_size=24,
            )
            report = run_job(
                root=root,
                config=config,
                samples_path=sample_path,
                verify_recompute=False,
            )
            self.assertEqual(report["status"], "OK")
            self.assertTrue(
                report["workbench_checks"]["sample_indexes_consistent"]
            )
            self.assertTrue((root / "runs" / config.job_id / "report.json").is_file())
            self.assertTrue((root / "snapshots" / config.job_id / "snapshot.json").is_file())
            workbench = Workbench(
                root=root, config=config, samples_path=sample_path
            )
            rendered = workbench.render_all()
            self.assertIn("Loss trajectory comparison", rendered)
            self.assertIn("Dominant source", rendered)
            self.assertIn("recent_business_error_code=None", rendered)

    def test_tolerance_violation_returns_code_and_first_round(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sample_path = root / "samples" / "samples.jsonl"
            generate_deterministic_samples(sample_path, row_count=100)
            config = config_with(
                job_id="tolerance-job",
                max_iterations=3,
                consistency_tolerance=0.0,
            )
            report = run_job(
                root=root,
                config=config,
                samples_path=sample_path,
                verify_recompute=False,
            )
            self.assertEqual(report["status"], "ERROR")
            error = report["business_error"]
            self.assertEqual(error["code"], "E_TOLERANCE_EXCEEDED")
            self.assertEqual(error["details"]["round"], 1)
            self.assertIn(error["details"]["location"], {"loss", "parameter"})
            last_error = json.loads(
                (root / "cache" / "last_error.json").read_text(encoding="utf-8")
            )
            self.assertEqual(last_error["code"], "E_TOLERANCE_EXCEEDED")

    def test_duplicate_job_id_through_run_api(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sample_path = root / "samples" / "samples.jsonl"
            generate_deterministic_samples(sample_path, row_count=100)
            config = config_with(job_id="duplicate-job", max_iterations=2)
            run_job(
                root=root,
                config=config,
                samples_path=sample_path,
                verify_recompute=False,
            )
            with self.assertRaises(WorkbenchError) as context:
                run_job(
                    root=root,
                    config=config,
                    samples_path=sample_path,
                    verify_recompute=False,
                )
            self.assertEqual(context.exception.code, "E_DUPLICATE_JOB_ID")

    def test_interrupted_report_then_resume_matches_uninterrupted_training(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sample_path = root / "samples" / "samples.jsonl"
            generate_deterministic_samples(sample_path, row_count=120)
            config = config_with(
                job_id="resume-job",
                max_iterations=8,
                convergence_tolerance=1e-9,
                min_improvement=0.0,
                chunk_size=24,
            )
            partial = run_job(
                root=root,
                config=config,
                samples_path=sample_path,
                stop_after_rounds=3,
                verify_recompute=False,
            )
            self.assertEqual(partial["status"], "INTERRUPTED")
            self.assertEqual(
                partial["execution"]["checkpoint"]["committed_rounds"], [1, 2, 3]
            )
            resumed = resume_job(
                root=root,
                config=config,
                samples_path=sample_path,
                verify_recompute=False,
            )
            self.assertEqual(resumed["status"], "OK")
            self.assertEqual(
                resumed["execution"]["checkpoint"]["committed_before_invocation"],
                [1, 2, 3],
            )
            self.assertEqual(
                resumed["execution"]["checkpoint"]["rebuilt_rounds"], [4, 5, 6, 7, 8]
            )

            dataset = load_dataset(sample_path, config)
            split = make_stratified_split(dataset, config)
            prepared = prepare_for(dataset, split, config)
            uninterrupted = train_prepared(
                prepared,
                config,
                reduce_mode="chunked",
                run_id="resume-check",
                collect_chunk_details=False,
            )
            self.assertEqual(
                resumed["execution"]["primary_run"]["core_sha256"],
                uninterrupted.core_digest,
            )

    def test_interactive_loop_accepts_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sample_path = root / "samples" / "samples.jsonl"
            generate_deterministic_samples(sample_path, row_count=80)
            config = config_with(job_id="interactive-job", max_iterations=2)
            run_job(
                root=root,
                config=config,
                samples_path=sample_path,
                verify_recompute=False,
            )
            workbench = Workbench(root=root, config=config, samples_path=sample_path)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                with mock.patch(
                    "builtins.input", side_effect=["status", "switch float32", "quit"]
                ):
                    workbench.interactive()
            self.assertIn("job_id=interactive-job", output.getvalue())

    def test_workbench_reports_inconsistent_category(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sample_path = root / "samples" / "samples.jsonl"
            generate_deterministic_samples(sample_path, row_count=80)
            config = config_with(job_id="inconsistent-job", max_iterations=2)
            run_job(
                root=root,
                config=config,
                samples_path=sample_path,
                verify_recompute=False,
            )
            workbench = Workbench(root=root, config=config, samples_path=sample_path)
            workbench.report["workbench_checks"]["error_spectrum_recompute_match"] = False
            with self.assertRaises(WorkbenchError) as context:
                workbench.verify_consistency()
            self.assertEqual(context.exception.code, "E_INCONSISTENT")
            self.assertEqual(
                context.exception.details["category"],
                "error_spectrum_recompute_mismatch",
            )


if __name__ == "__main__":
    unittest.main()
