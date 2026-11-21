from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from bmm_pipeline.config import PipelineConfig
from bmm_pipeline.engine import PipelineEngine
from bmm_pipeline.errors import PipelineError
from bmm_pipeline.jobs import generate_sample_jobs, write_jobs_jsonl
from bmm_pipeline.status import build_status
from bmm_pipeline.verify import verify_run

from tests.support import base_job, load_output_arrays, write_jsonl


class EngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def sample_jobs(self, name: str = "jobs.jsonl") -> Path:
        path = self.root / name
        write_jobs_jsonl(path, generate_sample_jobs())
        return path

    def test_full_run_status_and_verify(self) -> None:
        jobs = self.sample_jobs()
        run_dir = self.root / "run"
        report = PipelineEngine(run_dir).run(jobs)
        self.assertEqual(report.status, "complete")
        self.assertEqual(report.snapshot_version, 4)
        self.assertEqual(
            [snapshot["stage"] for snapshot in report.committed_stages],
            [],
        )
        self.assertEqual(
            report.executed_stages,
            ["validation", "planning", "accumulation", "aggregation"],
        )
        status = build_status(run_dir)
        self.assertEqual(status["current_stage"], "aggregation")
        self.assertEqual(status["snapshot_version"], 4)
        self.assertEqual(
            status["completed_count_by_stage"],
            {
                "validation": 8,
                "planning": 8,
                "accumulation": 8,
                "aggregation": 8,
            },
        )
        self.assertGreater(status["compensation_interventions"], 0)
        self.assertEqual(len(status["deviation_spectrum"]), 8)
        verification = verify_run(run_dir)
        self.assertTrue(verification.ok)
        self.assertGreater(verification.checks_run, 50)

    def test_forced_interruption_recovery_is_elementwise_identical(self) -> None:
        jobs = self.sample_jobs()
        baseline_dir = self.root / "baseline"
        restarted_dir = self.root / "restarted"
        PipelineEngine(baseline_dir).run(jobs)

        def crash(event: dict) -> None:
            if event["blocks_done"] == 20:
                raise RuntimeError("simulated forced stop")

        with self.assertRaises(RuntimeError):
            PipelineEngine(restarted_dir).run(jobs, fault_hook=crash)
        self.assertEqual(
            len(list((restarted_dir / "snapshots").glob("*.json"))), 2
        )
        progress = json.loads(
            (restarted_dir / "progress.json").read_text(encoding="utf-8")
        )
        self.assertEqual(progress["stage"], "accumulation")
        self.assertEqual(progress["blocks_done"], 20)

        resumed = PipelineEngine(restarted_dir).run(jobs)
        self.assertEqual(resumed.status, "complete")
        self.assertEqual(resumed.resumed_from_stage, "planning")
        self.assertEqual(
            resumed.rebuilt_stages, ["accumulation", "aggregation"]
        )
        self.assertEqual(
            resumed.executed_stages, ["accumulation", "aggregation"]
        )
        self.assertEqual(resumed.discarded_blocks, 20)
        baseline_arrays = load_output_arrays(baseline_dir)
        restarted_arrays = load_output_arrays(restarted_dir)
        self.assertEqual(set(baseline_arrays), set(restarted_arrays))
        for job_id, baseline in baseline_arrays.items():
            self.assertTrue(
                np.array_equal(baseline, restarted_arrays[job_id], equal_nan=True),
                job_id,
            )
        self.assertEqual(
            (baseline_dir / "final_results.json").read_bytes(),
            (restarted_dir / "final_results.json").read_bytes(),
        )
        self.assertTrue(verify_run(restarted_dir).ok)

    def test_resume_from_committed_accumulation_only_rebuilds_aggregation(self) -> None:
        jobs = self.sample_jobs()
        baseline_dir = self.root / "baseline"
        resumed_dir = self.root / "resume-agg"
        PipelineEngine(baseline_dir).run(jobs)
        PipelineEngine(resumed_dir).run(jobs)
        (resumed_dir / "snapshots" / "0004-aggregation.json").unlink()
        (resumed_dir / "CURRENT.json").unlink()
        report = PipelineEngine(resumed_dir).run(jobs)
        self.assertEqual(report.resumed_from_stage, "accumulation")
        self.assertEqual(report.executed_stages, ["aggregation"])
        self.assertEqual(report.discarded_blocks, 0)
        for job_id, baseline in load_output_arrays(baseline_dir).items():
            self.assertTrue(
                np.array_equal(
                    baseline, load_output_arrays(resumed_dir)[job_id], equal_nan=True
                )
            )

    def test_complete_run_repairs_stale_current_pointer_without_recompute(self) -> None:
        jobs_path = self.root / "jobs.jsonl"
        write_jsonl(jobs_path, [base_job()])
        run_dir = self.root / "run"
        PipelineEngine(run_dir).run(jobs_path)
        (run_dir / "CURRENT.json").unlink()
        report = PipelineEngine(run_dir).run(jobs_path)
        self.assertEqual(report.executed_stages, [])
        pointer = json.loads(
            (run_dir / "CURRENT.json").read_text(encoding="utf-8")
        )
        self.assertEqual(pointer["stage"], "aggregation")
        self.assertEqual(pointer["version"], 4)

    def test_required_errors_from_engine_inputs(self) -> None:
        cases = [
            ({"matrix_shapes": [[9, 7], [6, 8]]}, "E_SHAPE_INVALID"),
            (
                {
                    "op_kind": "bmm",
                    "matrix_shapes": [[2, 3, 4], [3, 4, 5]],
                },
                "E_BATCH_MISMATCH",
            ),
            ({"dtype": "int32"}, "E_DTYPE_UNSUPPORTED"),
            ({"block_size": -1}, "E_BLOCK_SIZE_INVALID"),
            ({"accum_mode": "unknown"}, "E_ACCUM_MODE_UNKNOWN"),
        ]
        for index, (overrides, code) in enumerate(cases):
            with self.subTest(code=code):
                jobs_path = self.root / f"bad-{index}.jsonl"
                write_jsonl(jobs_path, [base_job(**overrides)])
                run_dir = self.root / f"bad-run-{index}"
                with self.assertRaises(PipelineError) as context:
                    PipelineEngine(run_dir).run(jobs_path)
                self.assertEqual(context.exception.code, code)
                self.assertEqual(context.exception.job_id, "job-a")
                self.assertIsNotNone(context.exception.field)
                errors = build_status(run_dir)["recent_errors"]
                self.assertEqual(errors[-1]["code"], code)

    def test_tolerance_exceeded_has_element_location(self) -> None:
        jobs_path = self.root / "tight.jsonl"
        write_jsonl(jobs_path, [base_job(block_size=2)])
        run_dir = self.root / "tight-run"
        with self.assertRaises(PipelineError) as context:
            PipelineEngine(
                run_dir, PipelineConfig(atol=0.0, rtol=0.0)
            ).run(jobs_path)
        exc = context.exception
        self.assertEqual(exc.code, "E_TOLERANCE_EXCEEDED")
        self.assertEqual(exc.job_id, "job-a")
        self.assertGreater(exc.details["mismatch_count"], 0)
        self.assertTrue(exc.details["violations"])
        self.assertIn("index", exc.details["violations"][0])
        self.assertIn("abs_error", exc.details["violations"][0])

    def test_config_or_source_change_blocks_resume(self) -> None:
        jobs_path = self.root / "jobs.jsonl"
        write_jsonl(jobs_path, [base_job()])
        run_dir = self.root / "run"
        PipelineEngine(run_dir).run(jobs_path)
        with self.assertRaises(PipelineError) as context:
            PipelineEngine(
                run_dir, PipelineConfig(max_block_count=50_000)
            ).run(jobs_path)
        self.assertEqual(context.exception.code, "E_RESUME_MISMATCH")

        source_dir = self.root / "run-source"
        PipelineEngine(source_dir).run(jobs_path)
        jobs_path.write_text(jobs_path.read_text() + "\n", encoding="utf-8")
        with self.assertRaises(PipelineError) as context:
            PipelineEngine(source_dir).run(jobs_path)
        self.assertEqual(context.exception.code, "E_RESUME_MISMATCH")

    def test_stage_job_limit_returns_resource_limit(self) -> None:
        jobs_path = self.root / "two.jsonl"
        write_jsonl(
            jobs_path,
            [base_job(job_id="a"), base_job(job_id="b", seed=2)],
        )
        with self.assertRaises(PipelineError) as context:
            PipelineEngine(
                self.root / "limited",
                PipelineConfig(max_stage_jobs=1),
            ).run(jobs_path)
        self.assertEqual(context.exception.code, "E_RESOURCE_LIMIT")
        self.assertEqual(context.exception.field, "max_stage_jobs")

    def test_resource_downgrade_is_visible_in_results(self) -> None:
        jobs_path = self.root / "large.jsonl"
        write_jsonl(
            jobs_path,
            [
                base_job(
                    job_id="large-bmm",
                    op_kind="bmm",
                    matrix_shapes=[[2, 64, 64], [2, 64, 64]],
                    block_size=16,
                    accum_mode="kahan",
                )
            ],
        )
        run_dir = self.root / "degraded"
        report = PipelineEngine(
            run_dir,
            PipelineConfig(max_accum_bytes=5_000, max_block_count=200_000),
        ).run(jobs_path)
        diagnostics = report.summaries[0]["diagnostics"]
        self.assertIn(
            "BATCH_AGGREGATION_DISABLED",
            diagnostics["degradation_categories"],
        )
        self.assertIn("SMALLER_BLOCKS", diagnostics["degradation_categories"])
        self.assertTrue(verify_run(run_dir).ok)

    def test_verify_detects_tampered_output(self) -> None:
        jobs_path = self.root / "jobs.jsonl"
        write_jsonl(
            jobs_path,
            [
                base_job(job_id="a"),
                base_job(job_id="b", seed=7, layout="col"),
            ],
        )
        run_dir = self.root / "run"
        PipelineEngine(run_dir).run(jobs_path)
        summary = json.loads(
            (run_dir / "final_results.json").read_text(encoding="utf-8")
        )
        target = summary["jobs"][0]
        output_path = run_dir / "results" / target["output_file"]
        tampered = np.load(output_path, allow_pickle=False)
        tampered.flat[0] += 1.0
        np.save(output_path, tampered)
        report = verify_run(run_dir)
        self.assertFalse(report.ok)
        categories = {issue["category"] for issue in report.issues}
        self.assertIn("output_file_mismatch", categories)
        self.assertEqual(report.issues[0]["job_id"], target["job_id"])
        with self.assertRaises(PipelineError) as context:
            report.raise_if_failed()
        self.assertEqual(context.exception.code, "E_INCONSISTENT")
        self.assertIn("category", context.exception.details)


if __name__ == "__main__":
    unittest.main()
