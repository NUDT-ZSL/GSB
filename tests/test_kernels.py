from __future__ import annotations

import unittest

import numpy as np

from bmm_pipeline.config import PipelineConfig
from bmm_pipeline.errors import PipelineError
from bmm_pipeline.jobs import generate_sample_jobs, validate_job
from bmm_pipeline.kernels import (
    KernelStats,
    _sanitize,
    check_tolerance,
    compare_exact,
    compute_job,
    merge_partials,
)
from bmm_pipeline.strategy import choose_strategy

from tests.support import make_job


class KernelTests(unittest.TestCase):
    def test_sample_jobs_match_reference_and_partial_merge(self) -> None:
        config = PipelineConfig()
        for job in generate_sample_jobs():
            with self.subTest(job=job.job_id):
                plan = choose_strategy(job, config)
                run = compute_job(job, plan, config, collect_partials=True)
                self.assertEqual(run.diagnostics["mismatch_count"], 0)
                self.assertLess(run.diagnostics["max_abs_error"], 1e-12)
                merged = merge_partials(
                    job, plan, config, run.partials or []
                )
                self.assertTrue(compare_exact(merged, run.result))
                if job.accum_mode != "plain":
                    self.assertGreater(
                        run.stats.compensation_interventions, 0
                    )

    def test_tolerance_error_reports_index_and_deviation(self) -> None:
        reference = np.zeros((2, 3), dtype=np.float64)
        actual = reference.copy()
        actual[1, 2] = 0.25
        with self.assertRaises(PipelineError) as context:
            check_tolerance(
                actual,
                reference,
                job_id="job-a",
                atol=0.0,
                rtol=0.0,
            )
        exc = context.exception
        self.assertEqual(exc.code, "E_TOLERANCE_EXCEEDED")
        self.assertEqual(exc.job_id, "job-a")
        self.assertEqual(exc.field, "result")
        violation = exc.details["violations"][0]
        self.assertEqual(violation["index"], [1, 2])
        self.assertEqual(violation["abs_error"], 0.25)

    def test_gemm_and_batch_one_bmm_are_exact(self) -> None:
        config = PipelineConfig()
        gemm = make_job(
            matrix_shapes=[[13, 9], [9, 12]],
            layout="col",
            accum_mode="neumaier",
        )
        bmm = validate_job(
            {
                **gemm.to_dict(),
                "job_id": "job-bmm-one",
                "op_kind": "bmm",
                "matrix_shapes": [[1, 13, 9], [1, 9, 12]],
            }
        )
        gemm_run = compute_job(
            gemm, choose_strategy(gemm, config), config
        )
        bmm_run = compute_job(
            bmm, choose_strategy(bmm, config), config
        )
        self.assertTrue(compare_exact(gemm_run.result, bmm_run.result[0]))

    def test_subnormal_flush_protection(self) -> None:
        config = PipelineConfig(subnormal_mode="flush")
        stats = KernelStats()
        tiny = np.finfo(np.float64).tiny
        values = np.array([tiny / 2.0, -tiny / 2.0, 1.0], dtype=np.float64)
        sanitized = _sanitize(values, config, stats)
        self.assertEqual(sanitized[0], 0.0)
        self.assertEqual(sanitized[1], 0.0)
        self.assertEqual(sanitized[2], 1.0)
        self.assertEqual(stats.flushed_subnormals, 2)

    def test_directed_rounding_is_configurable(self) -> None:
        config = PipelineConfig(rounding_mode="up")
        job = make_job(block_size=3)
        run = compute_job(job, choose_strategy(job, config), config)
        self.assertGreater(run.stats.rounded_values, 0)
        self.assertEqual(run.diagnostics["mismatch_count"], 0)


if __name__ == "__main__":
    unittest.main()
