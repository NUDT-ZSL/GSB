from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from blockops import (
    ExecutionOptions,
    FaultParameters,
    OperatorTask,
    ResourceLimits,
    ToleranceConfig,
    execute_job,
    injection_diagnostics,
    layernorm,
    softmax,
)
from blockops.errors import (
    E_AXIS_INVALID,
    E_BLOCK_SIZE_INVALID,
    E_DTYPE_UNSUPPORTED,
    E_EPS_INVALID,
    E_INCONSISTENT,
    E_OP_UNKNOWN,
    E_RESOURCE_LIMIT,
    E_SHAPE_INVALID,
    E_STAT_OVERFLOW,
    E_TOLERANCE_EXCEEDED,
    E_UNATTRIBUTED,
    E_WRITE_INDEX,
    STAGE_BLOCK_STATS,
    STAGE_INPUT,
    STAGE_NORMALIZATION,
    STAGE_WRITEBACK,
)
from blockops.util import array_bytes, canonical_json_bytes


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def load_samples() -> tuple[dict[str, OperatorTask], dict[str, np.ndarray]]:
    tasks: dict[str, OperatorTask] = {}
    with open(SAMPLES / "tasks.jsonl", "r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                value = json.loads(line)
                tasks[value["job_id"]] = OperatorTask.from_mapping(value)
    tensors = {
        job_id: np.load(SAMPLES / "tensors" / f"{job_id}.npy", allow_pickle=False)
        for job_id in tasks
    }
    return tasks, tensors


class OperatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tasks, cls.tensors = load_samples()

    def execute(self, job_id: str, options: ExecutionOptions | None = None):
        return execute_job(self.tasks[job_id], self.tensors[job_id], options)

    def test_normal_jobs_match_reference_and_cover_all_indices(self) -> None:
        for job_id, task in self.tasks.items():
            with self.subTest(job_id=job_id):
                result = self.execute(job_id)
                self.assertEqual(result.status, "ok")
                self.assertEqual(result.business_codes(), [])
                self.assertTrue(result.tolerance_report["within_tolerance"])
                self.assertEqual(result.self_check["status"], "passed")
                all_indices = set(range(self.tensors[job_id].size))
                block_indices = {
                    index
                    for block in result.blocks
                    for index in block.element_indices
                }
                self.assertEqual(block_indices, all_indices)
                self.assertEqual(
                    [block.axis_start for block in result.blocks],
                    list(
                        range(
                            0,
                            int(task.input_shape[int(task.axis) % len(task.input_shape)]),
                            result.plan["effective_block_size"],
                        )
                    ),
                )
                self.assertEqual(result.plan["block_ranges"][0][0], 0)
                self.assertEqual(
                    result.plan["block_ranges"][-1][1],
                    int(task.input_shape[int(task.axis) % len(task.input_shape)]),
                )
                self.assertLessEqual(
                    result.tolerance_report["max_absolute_deviation"],
                    options_tolerance(result),
                )

    def test_softmax_equal_maximum_uses_smallest_element_index(self) -> None:
        result = self.execute("softmax_2d_f64")
        row = np.moveaxis(self.tensors["softmax_2d_f64"], -1, -1).reshape(-1, 10)[0]
        expected = int(np.flatnonzero(row == np.max(row))[0])
        self.assertEqual(expected, 0)
        self.assertEqual(
            result.blocks[-1].rows[0]["running_maximum_flat_index_after"],
            expected,
        )
        self.assertEqual(result.blocks[0].rows[0]["first_maximum_axis_index"], 0)
        self.assertEqual(result.blocks[1].rows[0]["first_maximum_axis_index"], 4)

    def test_softmax_online_statistics_are_numerically_exact_to_reference(self) -> None:
        result = self.execute("softmax_3d_f32_axis1")
        self.assertTrue(result.tolerance_report["within_tolerance"])
        for block in result.blocks:
            self.assertIn("running_denominator_after", block.statistics)
            self.assertIn("first_maximum_axis_index", block.statistics)
            self.assertIn("online_rescale_factor", block.statistics)

    def test_layernorm_cross_block_statistics_are_recorded(self) -> None:
        result = self.execute("layernorm_2d_f64")
        self.assertTrue(result.tolerance_report["within_tolerance"])
        for block in result.blocks:
            self.assertIn("block_mean", block.statistics)
            self.assertIn("block_variance", block.statistics)
            self.assertIn("running_mean_after", block.statistics)
            self.assertIn("running_m2_after", block.statistics)
        self.assertIn("Chan/Welford", result.blocks[0].statistics["reduction_rule"])

    def test_unified_validation_codes_and_fields(self) -> None:
        base = dict(self.tasks["softmax_2d_f64"].raw)
        cases = [
            ("op_kind", "mystery", E_OP_UNKNOWN, "op_kind"),
            ("axis", 99, E_AXIS_INVALID, "axis"),
            ("input_shape", [999, 10], E_SHAPE_INVALID, "input_shape"),
            ("dtype", "float16", E_DTYPE_UNSUPPORTED, "dtype"),
        ]
        for field, value, code, expected_field in cases:
            with self.subTest(field=field):
                raw = dict(base)
                raw[field] = value
                raw["job_id"] = f"validation:{field}"
                result = execute_job(
                    OperatorTask.from_mapping(raw),
                    self.tensors["softmax_2d_f64"],
                )
                self.assertEqual(result.status, "failed")
                self.assertEqual(result.business_codes()[0], code)
                self.assertEqual(result.diagnostics[0].field, expected_field)
                self.assertEqual(result.diagnostics[0].job_id, f"validation:{field}")
                self.assertEqual(result.degradations[0]["category"], "ABORT_JOB")

        wrapper_axis_error = softmax(
            self.tensors["softmax_2d_f64"],
            axis=99,
            job_id="validation:wrapper-axis",
        )
        self.assertEqual(wrapper_axis_error.business_codes(), [E_AXIS_INVALID])
        self.assertEqual(wrapper_axis_error.diagnostics[0].field, "axis")

    def test_input_and_block_size_injections_degrade_with_traceable_chain(self) -> None:
        eps_raw = dict(self.tasks["layernorm_2d_f64"].raw)
        eps_raw["job_id"] = "inject:eps"
        eps_raw["eps"] = -1.0
        eps_result = execute_job(
            OperatorTask.from_mapping(eps_raw),
            self.tensors["layernorm_2d_f64"],
        )
        self.assertEqual(eps_result.status, "degraded")
        self.assertIn(E_EPS_INVALID, eps_result.business_codes())
        eps_diag = next(
            item for item in eps_result.diagnostics if item.code == E_EPS_INVALID
        )
        self.assertEqual(eps_diag.stage, STAGE_INPUT)
        self.assertEqual(eps_diag.field, "eps")
        self.assertIsNotNone(eps_diag.to_dict()["params_digest"])
        eps_degradation = eps_result.degradations[0]
        self.assertEqual(eps_degradation["category"], "EPS_FLOOR")
        self.assertEqual(eps_degradation["trigger_code"], E_EPS_INVALID)
        self.assertEqual(
            eps_degradation["affected_blocks"],
            [block.block_id for block in eps_result.blocks],
        )
        self.assertEqual(eps_result.self_check["status"], "passed")
        self.assertEqual(
            [item["stage"] for item in injection_diagnostics(eps_result)],
            [STAGE_INPUT],
        )

        block_raw = dict(self.tasks["softmax_2d_f64"].raw)
        block_raw["job_id"] = "inject:block"
        block_raw["block_size"] = 0
        block_result = execute_job(
            OperatorTask.from_mapping(block_raw),
            self.tensors["softmax_2d_f64"],
        )
        self.assertEqual(block_result.status, "degraded")
        self.assertIn(E_BLOCK_SIZE_INVALID, block_result.business_codes())
        self.assertEqual(block_result.plan["effective_block_size"], 1)
        self.assertEqual(block_result.degradations[0]["category"], "BLOCK_SIZE_FALLBACK")
        self.assertEqual(block_result.self_check["status"], "passed")

    def test_normalization_and_writeback_injections(self) -> None:
        overflow = self.execute(
            "layernorm_2d_f64",
            ExecutionOptions(
                faults=FaultParameters(stat_overflow_scale=1e308)
            ),
        )
        self.assertIn(E_STAT_OVERFLOW, overflow.business_codes())
        overflow_diag = next(
            item for item in overflow.diagnostics if item.code == E_STAT_OVERFLOW
        )
        self.assertEqual(overflow_diag.stage, STAGE_NORMALIZATION)
        self.assertEqual(overflow.compute_dtype, "float32")
        self.assertEqual(overflow.degradations[0]["category"], "FLOAT32_NORMALIZATION")
        self.assertEqual(overflow.self_check["status"], "passed")

        writeback = self.execute(
            "softmax_2d_f64",
            ExecutionOptions(
                faults=FaultParameters(write_index_offset=1_000_000_000)
            ),
        )
        self.assertIn(E_WRITE_INDEX, writeback.business_codes())
        write_diag = next(
            item for item in writeback.diagnostics if item.code == E_WRITE_INDEX
        )
        self.assertEqual(write_diag.stage, STAGE_WRITEBACK)
        self.assertEqual(writeback.degradations[0]["category"], "SAFE_WRITEBACK")
        self.assertTrue(writeback.tolerance_report["within_tolerance"])
        self.assertEqual(writeback.self_check["status"], "passed")

    def test_zero_tolerance_exposes_real_roundoff_without_skipping(self) -> None:
        result = self.execute(
            "softmax_2d_f64",
            ExecutionOptions(tolerance=ToleranceConfig(0.0, 0.0)),
        )
        self.assertEqual(result.status, "failed")
        self.assertIn(E_TOLERANCE_EXCEEDED, result.business_codes())
        self.assertGreater(result.tolerance_report["violation_count"], 0)
        self.assertGreater(result.tolerance_report["max_absolute_deviation"], 0.0)
        self.assertTrue(result.tolerance_report["violations"])
        first = result.tolerance_report["violations"][0]
        self.assertIn("flat_index", first)
        self.assertIn("absolute_deviation", first)
        self.assertIn("relative_deviation", first)
        self.assertEqual(result.self_check["status"], "passed")

    def test_resource_limits_shrink_abort_and_float32_path(self) -> None:
        shrink = self.execute(
            "softmax_2d_f64",
            ExecutionOptions(
                limits=ResourceLimits(max_block_elements=3, max_blocks=100)
            ),
        )
        self.assertEqual(shrink.status, "degraded")
        self.assertIn(E_RESOURCE_LIMIT, shrink.business_codes())
        self.assertEqual(shrink.degradations[0]["category"], "BLOCK_SHRINK")
        self.assertEqual(shrink.plan["effective_block_size"], 1)
        self.assertTrue(shrink.tolerance_report["within_tolerance"])
        self.assertEqual(shrink.self_check["status"], "passed")

        aborted = self.execute(
            "softmax_2d_f64",
            ExecutionOptions(limits=ResourceLimits(max_blocks=1)),
        )
        self.assertEqual(aborted.status, "failed")
        self.assertEqual(aborted.output, None)
        self.assertEqual(aborted.business_codes(), [E_RESOURCE_LIMIT])
        self.assertEqual(aborted.degradations[0]["category"], "ABORT_JOB")

        block_too_small = self.execute(
            "softmax_2d_f64",
            ExecutionOptions(limits=ResourceLimits(max_block_elements=1)),
        )
        self.assertEqual(block_too_small.status, "failed")
        self.assertEqual(block_too_small.business_codes(), [E_RESOURCE_LIMIT])
        self.assertIn(
            "one element per outer slice",
            block_too_small.diagnostics[0].message,
        )

        float32_stats = self.execute(
            "layernorm_2d_f64",
            ExecutionOptions(
                limits=ResourceLimits(max_stat_bytes=400, max_blocks=100)
            ),
        )
        self.assertIn(E_RESOURCE_LIMIT, float32_stats.business_codes())
        self.assertEqual(float32_stats.plan["stats_dtype"], "float32")
        self.assertEqual(float32_stats.compute_dtype, "float32")
        self.assertEqual(float32_stats.degradations[0]["category"], "FLOAT32_STATS")
        self.assertIn(E_TOLERANCE_EXCEEDED, float32_stats.business_codes())

    def test_unattributed_exception_is_returned_not_silently_dropped(self) -> None:
        with mock.patch(
            "blockops.operators._softmax_pipeline",
            side_effect=RuntimeError("synthetic unclaimed error"),
        ):
            result = softmax(
                self.tensors["softmax_2d_f64"],
                axis=-1,
                block_size=4,
                job_id="unattributed",
            )
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.business_codes(), [E_UNATTRIBUTED])
        self.assertEqual(len(result.unattributed_errors), 1)
        self.assertEqual(result.unattributed_errors[0]["type"], "RuntimeError")
        self.assertIn("synthetic unclaimed error", result.unattributed_errors[0]["message"])

    def test_self_check_failure_returns_inconsistent_category(self) -> None:
        failed_check = {
            "status": "failed",
            "failed_categories": ["deviation_spectrum_recompute"],
        }
        with mock.patch(
            "blockops.operators.run_self_check",
            return_value=failed_check,
        ):
            result = self.execute("softmax_2d_f64")
        self.assertEqual(result.status, "failed")
        self.assertIn(E_INCONSISTENT, result.business_codes())
        inconsistent = next(
            item for item in result.diagnostics if item.code == E_INCONSISTENT
        )
        self.assertEqual(inconsistent.stage, "cross_module_self_check")
        self.assertEqual(
            inconsistent.details["categories"],
            ["deviation_spectrum_recompute"],
        )

    def test_rerun_bytes_are_identical_for_stats_and_output(self) -> None:
        for job_id in self.tasks:
            with self.subTest(job_id=job_id):
                first = self.execute(job_id)
                second = self.execute(job_id)
                self.assertEqual(first.status, second.status)
                self.assertEqual(
                    canonical_json_bytes([block.to_dict() for block in first.blocks]),
                    canonical_json_bytes([block.to_dict() for block in second.blocks]),
                )
                self.assertEqual(
                    array_bytes(first.output),
                    array_bytes(second.output),
                )
                self.assertEqual(
                    canonical_json_bytes(first.tolerance_report),
                    canonical_json_bytes(second.tolerance_report),
                )
                self.assertEqual(
                    canonical_json_bytes(first.self_check),
                    canonical_json_bytes(second.self_check),
                )


def options_tolerance(result) -> float:
    return (
        result.tolerance_report["abs_tolerance"]
        + result.tolerance_report["rel_tolerance"]
    )


if __name__ == "__main__":
    unittest.main()
