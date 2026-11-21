from __future__ import annotations

import json
import unittest

from bmm_pipeline.config import PipelineConfig
from bmm_pipeline.errors import PipelineError
from bmm_pipeline.jobs import read_job_records, validate_job
from bmm_pipeline.strategy import choose_strategy

from tests.support import base_job, make_job


class ValidationTests(unittest.TestCase):
    def assert_business_error(
        self,
        record: dict,
        code: str,
        field: str,
        job_id: str | None = "job-a",
    ) -> PipelineError:
        with self.assertRaises(PipelineError) as context:
            validate_job(record)
        self.assertEqual(context.exception.code, code)
        self.assertEqual(context.exception.field, field)
        self.assertEqual(context.exception.job_id, job_id)
        return context.exception

    def test_required_business_codes_and_fields(self) -> None:
        self.assert_business_error(
            base_job(matrix_shapes=[[9, 7], [6, 8]]),
            "E_SHAPE_INVALID",
            "matrix_shapes",
        )
        self.assert_business_error(
            base_job(
                op_kind="bmm",
                matrix_shapes=[[2, 3, 4], [3, 4, 5]],
            ),
            "E_BATCH_MISMATCH",
            "matrix_shapes.batch",
        )
        self.assert_business_error(
            base_job(dtype="int64"),
            "E_DTYPE_UNSUPPORTED",
            "dtype",
        )
        self.assert_business_error(
            base_job(block_size=0),
            "E_BLOCK_SIZE_INVALID",
            "block_size",
        )
        self.assert_business_error(
            base_job(accum_mode="mystery"),
            "E_ACCUM_MODE_UNKNOWN",
            "accum_mode",
        )

    def test_bmm_shape_rank_and_inner_dimension(self) -> None:
        self.assert_business_error(
            base_job(op_kind="bmm", matrix_shapes=[[2, 3], [2, 3]]),
            "E_SHAPE_INVALID",
            "matrix_shapes",
        )
        self.assert_business_error(
            base_job(
                op_kind="bmm",
                matrix_shapes=[[2, 3, 4], [2, 5, 6]],
            ),
            "E_SHAPE_INVALID",
            "matrix_shapes",
        )

    def test_jsonl_parse_error_has_record_field(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bad.jsonl"
            path.write_text('{"job_id":\n', encoding="utf-8")
            with self.assertRaises(PipelineError) as context:
                read_job_records(path)
            self.assertEqual(context.exception.code, "E_SHAPE_INVALID")
            self.assertEqual(context.exception.field, "record")


class StrategyTests(unittest.TestCase):
    def test_strategy_bytes_identical_and_covers_all_tiles(self) -> None:
        job = make_job(
            matrix_shapes=[[11, 13], [13, 17]],
            block_size=5,
        )
        config = PipelineConfig()
        first = choose_strategy(job, config)
        second = choose_strategy(job, config)
        self.assertEqual(first.canonical_json(), second.canonical_json())
        self.assertEqual(first.checksum(), second.checksum())
        self.assertTrue(first.covers())
        self.assertEqual(first.block_size, {"m": 5, "n": 5, "k": 5})
        for size, block in (
            (job.m, first.block_size["m"]),
            (job.n, first.block_size["n"]),
            (job.k, first.block_size["k"]),
        ):
            covered = []
            for start in range(0, size, block):
                covered.extend(range(start, min(size, start + block)))
            self.assertEqual(covered, list(range(size)))

    def test_resource_downgrade_is_marked(self) -> None:
        job = make_job(
            job_id="large-bmm",
            op_kind="bmm",
            matrix_shapes=[[2, 64, 64], [2, 64, 64]],
            block_size=16,
            accum_mode="kahan",
        )
        plan = choose_strategy(
            job,
            PipelineConfig(max_accum_bytes=5_000, max_block_count=200_000),
        )
        self.assertTrue(plan.degraded)
        self.assertIn("BATCH_AGGREGATION_DISABLED", plan.degradation_categories)
        self.assertIn("SMALLER_BLOCKS", plan.degradation_categories)
        self.assertLessEqual(plan.resident_bytes, 5_000)
        self.assertEqual(plan.batch_path, "per_batch")

    def test_block_count_limit_returns_resource_limit(self) -> None:
        job = make_job(
            job_id="limit-job",
            op_kind="bmm",
            matrix_shapes=[[2, 32, 32], [2, 32, 32]],
            block_size=16,
        )
        with self.assertRaises(PipelineError) as context:
            choose_strategy(
                job,
                PipelineConfig(max_accum_bytes=1_000, max_block_count=10),
            )
        self.assertEqual(context.exception.code, "E_RESOURCE_LIMIT")
        self.assertEqual(context.exception.field, "block_size")
        self.assertEqual(context.exception.job_id, "limit-job")

    def test_plan_record_has_cost_basis(self) -> None:
        plan = choose_strategy(make_job(), PipelineConfig())
        payload = json.loads(plan.canonical_json())
        self.assertIn("dimensions", payload)
        self.assertIn("estimated_flops", payload)
        self.assertIn("formula", payload["decision_basis"])


if __name__ == "__main__":
    unittest.main()
