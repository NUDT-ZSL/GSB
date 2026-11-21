from __future__ import annotations

import copy
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

import numpy as np

from chunkdiag import adjudicate_deviations, conv2d, interpolate, run_task
from chunkdiag.cli import main as cli_main
from chunkdiag.serialization import canonical_json


def conv_task(job_id: str = "conv") -> dict:
    return {
        "job_id": job_id,
        "op_kind": "conv2d",
        "input_shape": [1, 8, 9, 2],
        "kernel_shape": [3, 2, 3, 3],
        "stride": [1, 1],
        "padding": 1,
        "seed": 101,
        "weight_seed": 202,
        "dtype": "float64",
        "block_policy": {
            "block_size": [3, 4],
            "candidates": [[3, 4], [2, 2]],
            "selection": "fixed",
        },
        "tolerance": {"atol": 1e-12, "rtol": 1e-9},
    }


def interp_task(mode: str) -> dict:
    return {
        "job_id": f"interp-{mode}",
        "op_kind": "interpolate",
        "interp_mode": mode,
        "input_shape": [1, 7, 8, 2],
        "scale": [1.7, 1.25],
        "stride": [1, 1],
        "padding": 0,
        "seed": 303,
        "dtype": "float64",
        "block_policy": {
            "block_size": [3, 3],
            "candidates": [[3, 3], [2, 2]],
            "selection": "fixed",
        },
        "tolerance": {"atol": 1e-12, "rtol": 1e-9},
    }


class TestNormalOperators(unittest.TestCase):
    def test_four_operator_families_match_reference(self) -> None:
        results = [
            conv2d(conv_task("conv-normal")),
            interpolate(interp_task("nearest")),
            interpolate(interp_task("bilinear")),
            interpolate(interp_task("bicubic")),
        ]
        for result in results:
            with self.subTest(job_id=result.job_id):
                self.assertEqual(result.status, "ok")
                self.assertIsNone(result.primary_code)
                self.assertTrue(result.report["comparison"]["within_tolerance"])
                self.assertEqual(
                    result.report["comparison"]["violation_count"], 0
                )
                self.assertLess(
                    result.report["comparison"]["max_abs_error"], 1e-12
                )
                self.assertTrue(result.report["adjudication"]["verified"])
                self.assertEqual(
                    result.report["execution"]["output_shape"],
                    result.report["reference"]["shape"],
                )
                self.assertEqual(result.output_source, "chunked")

    def test_reference_and_report_are_byte_reproducible(self) -> None:
        task = conv_task("conv-repeat")
        first = run_task(task)
        second = run_task(task)
        self.assertEqual(
            first.reference_output.tobytes(), second.reference_output.tobytes()
        )
        self.assertEqual(first.output.tobytes(), second.output.tobytes())
        self.assertEqual(
            first.report["reference"]["sha256"],
            second.report["reference"]["sha256"],
        )
        self.assertEqual(
            first.report["reproducibility"]["task_fingerprint_sha256"],
            second.report["reproducibility"]["task_fingerprint_sha256"],
        )
        self.assertEqual(
            canonical_json(first.report), canonical_json(second.report)
        )

    def test_float32_execution_uses_documented_tolerance_floor(self) -> None:
        task = conv_task("conv-f32")
        task["dtype"] = "float32"
        result = run_task(task)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.report["execution"]["effective_dtype"], "float32")
        comparison = result.report["comparison"]
        self.assertTrue(comparison["dtype_tolerance"]["dtype_floor_applied"])
        self.assertGreaterEqual(comparison["tolerance"]["atol"], 1e-5)
        self.assertGreaterEqual(comparison["tolerance"]["rtol"], 1e-4)
        self.assertTrue(comparison["within_tolerance"])


class TestValidationCodes(unittest.TestCase):
    def assert_stage_error(
        self, task: dict, code: str, field: str, job_id: str | None = None
    ) -> None:
        result = run_task(task)
        self.assertEqual(result.status, "error")
        self.assertEqual(result.primary_code, code)
        self.assertEqual(result.report["business_codes"][0]["code"], code)
        self.assertEqual(result.report["business_codes"][0]["field"], field)
        self.assertEqual(
            result.report["business_codes"][0]["job_id"],
            job_id or task.get("job_id"),
        )
        self.assertEqual(len(result.report["stage_records"]), 4)

    def test_unknown_op_kind(self) -> None:
        task = {"job_id": "bad-op", "op_kind": "matmul"}
        self.assert_stage_error(task, "E_OP_UNSUPPORTED", "op_kind")

    def test_shape_rank(self) -> None:
        task = conv_task("bad-shape")
        task["input_shape"] = [1, 8, 2]
        self.assert_stage_error(task, "E_SHAPE_INVALID", "input_shape")

    def test_convolution_channel_mismatch(self) -> None:
        task = conv_task("bad-channel")
        task["kernel_shape"] = [3, 4, 3, 3]
        self.assert_stage_error(task, "E_SHAPE_INVALID", "kernel_shape[1]")

    def test_nonpositive_stride(self) -> None:
        task = conv_task("bad-stride")
        task["stride"] = [0, 1]
        self.assert_stage_error(task, "E_PARAM_INVALID", "stride")

    def test_padding_out_of_declared_range(self) -> None:
        task = conv_task("bad-padding")
        task["padding"] = 3
        self.assert_stage_error(task, "E_PARAM_INVALID", "padding")

    def test_unsupported_dtype(self) -> None:
        task = conv_task("bad-dtype")
        task["dtype"] = "float16"
        self.assert_stage_error(task, "E_DTYPE_UNSUPPORTED", "dtype")

    def test_unknown_interp_mode(self) -> None:
        task = interp_task("nearest")
        task["interp_mode"] = "lanczos"
        self.assert_stage_error(task, "E_OP_UNSUPPORTED", "interp_mode")

    def test_unattributed_exception_is_reported(self) -> None:
        task = conv_task("bad-unattributed")
        with mock.patch(
            "chunkdiag.engine.reference_output",
            side_effect=RuntimeError("reference failure for test"),
        ):
            result = run_task(task)
        self.assertEqual(result.status, "error")
        self.assertEqual(result.primary_code, "E_INTERNAL_UNATTRIBUTED")
        self.assertEqual(
            len(result.report["unattributed_exceptions"]), 1
        )
        self.assertIn("RuntimeError", result.report["unattributed_exceptions"][0]["type"])


class TestInjectionDegradation(unittest.TestCase):
    def test_parameter_validation_injection(self) -> None:
        task = conv_task("inject-validation")
        task["injection"] = {"parameter_validation": "nonfinite_weight"}
        result = run_task(task)
        self.assertEqual(result.status, "error")
        self.assertEqual(result.primary_code, "E_PARAM_INVALID")
        self.assertEqual(
            result.report["injection_records"][0]["stage"],
            "parameter_validation",
        )
        self.assertEqual(
            result.report["injection_records"][0]["code"], "E_PARAM_INVALID"
        )
        self.assertEqual(
            result.report["injection_records"][0]["job_id"],
            "inject-validation",
        )
        self.assertEqual(
            result.report["degradation_chain"][0]["category"],
            "REJECT_INVALID_INPUT",
        )
        self.assertEqual(
            result.report["degradation_chain"][0]["upstream_code"],
            "E_PARAM_INVALID",
        )

    def test_padding_overflow_injection(self) -> None:
        task = conv_task("inject-padding")
        task["injection"] = {"parameter_validation": "padding_overflow"}
        result = run_task(task)
        self.assertEqual(result.primary_code, "E_PARAM_INVALID")
        self.assertEqual(
            result.report["business_codes"][0]["field"], "padding"
        )
        self.assertEqual(
            result.report["injection_records"][0]["trigger"],
            "padding_overflow",
        )

    def test_block_partition_zero_falls_back_to_single_block(self) -> None:
        task = conv_task("inject-block")
        task["injection"] = {"block_partition": "zero_block_size"}
        result = run_task(task)
        self.assertEqual(result.status, "degraded")
        self.assertEqual(result.primary_code, "E_PARAM_INVALID")
        self.assertEqual(
            result.report["degradation_chain"][0]["category"],
            "SINGLE_BLOCK_FALLBACK",
        )
        self.assertEqual(
            result.report["degradation_chain"][0]["upstream_code"],
            "E_PARAM_INVALID",
        )
        self.assertEqual(result.report["policy_record"]["layout"]["block_count"], 1)
        self.assertTrue(result.report["comparison"]["within_tolerance"])
        self.assertEqual(
            result.report["injection_records"][0]["trigger"],
            "zero_block_size",
        )

    def test_numeric_nonfinite_uses_reference_fallback(self) -> None:
        task = conv_task("inject-numeric")
        task["injection"] = {"numeric_compute": "nonfinite_output"}
        result = run_task(task)
        self.assertEqual(result.status, "degraded")
        self.assertEqual(result.primary_code, "E_NUMERIC_NONFINITE")
        self.assertEqual(result.output_source, "reference_fallback")
        self.assertEqual(
            result.report["execution"]["attempted_nonfinite_count"], 1
        )
        self.assertEqual(
            result.report["degradation_chain"][0]["category"],
            "REFERENCE_FALLBACK",
        )
        self.assertEqual(
            result.report["degradation_chain"][0]["upstream_code"],
            "E_NUMERIC_NONFINITE",
        )
        self.assertTrue(result.report["comparison"]["within_tolerance"])

    def test_invalid_tolerance_falls_back_to_default(self) -> None:
        task = conv_task("inject-tolerance")
        task["injection"] = {"deviation_compare": "nonpositive_tolerance"}
        result = run_task(task)
        self.assertEqual(result.status, "degraded")
        self.assertEqual(result.primary_code, "E_PARAM_INVALID")
        self.assertEqual(
            result.report["degradation_chain"][0]["category"],
            "DEFAULT_TOLERANCE",
        )
        self.assertEqual(
            result.report["comparison"]["tolerance_source"],
            "default_after_invalid_task_tolerance",
        )
        self.assertEqual(
            result.report["comparison"]["tolerance"],
            {"atol": 1e-12, "rtol": 1e-9},
        )

    def test_four_distinct_injection_stages_are_recorded(self) -> None:
        tasks = []
        for stage, trigger in (
            ("parameter_validation", "nonfinite_weight"),
            ("block_partition", "zero_block_size"),
            ("numeric_compute", "nonfinite_output"),
            ("deviation_compare", "nonpositive_tolerance"),
        ):
            task = conv_task(f"stage-{stage}")
            task["injection"] = {stage: trigger}
            tasks.append(task)
        stages = set()
        for task in tasks:
            result = run_task(task)
            self.assertTrue(result.report["injection_records"])
            record = result.report["injection_records"][0]
            self.assertEqual(record["job_id"], task["job_id"])
            self.assertIn("code", record)
            stages.add(record["stage"])
        self.assertEqual(
            stages,
            {
                "parameter_validation",
                "block_partition",
                "numeric_compute",
                "deviation_compare",
            },
        )


class TestResourceRouting(unittest.TestCase):
    def budget_task(self, job_id: str = "budget") -> dict:
        task = conv_task(job_id)
        task["input_shape"] = [1, 32, 32, 4]
        task["kernel_shape"] = [4, 4, 3, 3]
        task["seed"] = 404
        task["weight_seed"] = 505
        task["block_policy"] = {
            "block_size": [16, 16],
            "candidates": [[16, 16], [8, 8], [4, 4]],
        }
        task["resource_limits"] = {
            "max_resident_bytes": 80000,
            "max_blocks": 1000,
            "max_live_intermediates": 3,
            "allow_smaller_blocks": True,
            "allow_dtype_downgrade": True,
        }
        return task

    def test_budget_downgrades_dtype_and_block_size(self) -> None:
        result = run_task(self.budget_task())
        self.assertEqual(result.status, "degraded")
        self.assertEqual(result.primary_code, "E_RESOURCE_LIMIT")
        self.assertEqual(result.report["execution"]["effective_dtype"], "float32")
        self.assertNotEqual(
            result.report["resource"]["block_size"], [16, 16]
        )
        categories = {
            item["category"] for item in result.report["degradation_chain"]
        }
        self.assertIn("DTYPE_DOWNGRADE", categories)
        self.assertIn("SMALLER_BLOCK", categories)
        for item in result.report["degradation_chain"]:
            self.assertEqual(item["upstream_code"], "E_RESOURCE_LIMIT")
        self.assertTrue(result.report["comparison"]["within_tolerance"])
        self.assertTrue(
            result.report["comparison"]["dtype_tolerance"][
                "dtype_floor_applied"
            ]
        )

    def test_impossible_budget_returns_resource_limit(self) -> None:
        task = self.budget_task("budget-impossible")
        task["resource_limits"]["max_resident_bytes"] = 1000
        result = run_task(task)
        self.assertEqual(result.status, "error")
        self.assertEqual(result.primary_code, "E_RESOURCE_LIMIT")
        self.assertEqual(
            result.report["degradation_chain"][0]["category"],
            "REJECT_RESOURCE_LIMIT",
        )
        self.assertIsNone(result.output)

    def test_block_count_limit_can_enlarge_block(self) -> None:
        task = conv_task("budget-block-count")
        task["block_policy"] = {"block_size": [2, 2], "selection": "fixed"}
        task["resource_limits"] = {
            "max_resident_bytes": 1024 * 1024,
            "max_blocks": 1,
            "max_live_intermediates": 3,
            "allow_smaller_blocks": True,
            "allow_dtype_downgrade": True,
        }
        result = run_task(task)
        self.assertEqual(result.status, "degraded")
        self.assertEqual(result.report["policy_record"]["layout"]["block_count"], 1)
        self.assertIn(
            "LARGER_BLOCK",
            {
                item["category"]
                for item in result.report["degradation_chain"]
            },
        )
        self.assertTrue(result.report["comparison"]["within_tolerance"])


class TestSpectrumAdjudication(unittest.TestCase):
    def test_adjudication_recomputes_from_detail_values(self) -> None:
        result = conv2d(conv_task("spectrum"))
        adjudication = adjudicate_deviations(result)
        self.assertTrue(adjudication["verified"])
        self.assertEqual(
            adjudication["declared_dominant_source"],
            result.report["deviation_spectrum"]["dominant_source"],
        )

    def test_tampered_detail_is_detected(self) -> None:
        original = conv2d(conv_task("spectrum-tamper")).to_dict()
        tampered = copy.deepcopy(original)
        spectrum = tampered["deviation_spectrum"]
        first_source = next(iter(spectrum["sources"].values()))
        if not first_source["details"]["values"]:
            self.skipTest("source detail was empty")
        first_source["details"]["values"][0] += 1.0
        adjudication = adjudicate_deviations(spectrum)
        self.assertFalse(adjudication["verified"])
        self.assertTrue(adjudication["mismatches"])


class TestCLIDemo(unittest.TestCase):
    def test_demo_writes_deterministic_report_with_all_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as first_dir, tempfile.TemporaryDirectory() as second_dir:
            with redirect_stdout(StringIO()):
                code_one = cli_main(["demo", "--out-dir", first_dir])
                code_two = cli_main(["demo", "--out-dir", second_dir])
            self.assertEqual(code_one, 0)
            self.assertEqual(code_two, 0)

            first_tasks = Path(first_dir) / "demo_tasks.jsonl"
            first_report = Path(first_dir) / "report.json"
            second_report = Path(second_dir) / "report.json"
            self.assertTrue(first_tasks.is_file())
            self.assertTrue(first_report.is_file())
            self.assertEqual(
                first_report.read_bytes(), second_report.read_bytes()
            )

            report = json.loads(first_report.read_text(encoding="utf-8"))
            self.assertEqual(report["task_count"], 9)
            self.assertEqual(
                set(report["summary"]["injection_stage_counts"]),
                {
                    "parameter_validation",
                    "block_partition",
                    "numeric_compute",
                    "deviation_compare",
                },
            )
            self.assertIn(
                "DTYPE_DOWNGRADE",
                report["summary"]["degradation_category_counts"],
            )
            for result in report["results"]:
                self.assertIn(
                    result["status"], {"ok", "degraded", "error"}
                )
                if result["deviation_spectrum"]:
                    self.assertTrue(result["adjudication"]["verified"])


if __name__ == "__main__":
    unittest.main()
