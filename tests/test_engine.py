from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np

from generate_sample import write_sample
from image_diag.cache import CacheHooks
from image_diag.deterministic import png_encode, to_uint8
from image_diag.graph import (
    ControlPlane,
    RunArtifacts,
    assert_block_match,
    error_report,
    execute_block,
    execute_whole,
)
from image_diag.models import Sample, StyleParams
from image_diag.precision import Executor, PrecisionSpec
from image_diag.service import ImageDiagService


class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory(prefix="image-diag-engine-")
        cls.sample_dir = Path(cls.temp.name) / "sample"
        write_sample(cls.sample_dir)
        cls.sample = Sample.load(cls.sample_dir)
        cls.control = ControlPlane(cls.sample, cls.sample.style)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def test_sample_generation_is_byte_deterministic(self) -> None:
        with tempfile.TemporaryDirectory(prefix="image-diag-sample-") as temporary:
            second = Path(temporary) / "sample"
            write_sample(second)
            for name in ("base.npy", "mask.npy", "sample.json", "style.json", "regions.json"):
                self.assertEqual(
                    (self.sample_dir / name).read_bytes(),
                    (second / name).read_bytes(),
                    name,
                )

    def test_three_precisions_match_block_and_whole_at_every_stage(self) -> None:
        for precision in ("float64", "float32", "fixed"):
            with self.subTest(precision=precision):
                executor = Executor(
                    PrecisionSpec.create(
                        precision,
                        self.sample.style.fixed_scale,
                        self.sample.style.fixed_rounding,
                    )
                )
                whole = (
                    self.control.reference
                    if precision == "float64"
                    else execute_whole(self.control, executor)
                )
                block = execute_block(
                    self.control, executor, stats=whole.stats
                )
                self.assertTrue(
                    np.array_equal(whole.fused, block.fused),
                    f"{precision} final arrays differ",
                )
                whole_hashes = {
                    record["stage"]: record["output_hash"]
                    for record in whole.stage_records
                }
                block_hashes = {
                    record["stage"]: record["output_hash"]
                    for record in block.stage_records
                }
                self.assertEqual(whole_hashes, block_hashes)

    def test_float64_repeat_is_byte_identical(self) -> None:
        first = execute_whole(self.control, self.control.executor)
        second = execute_whole(self.control, self.control.executor)
        self.assertEqual(first.final_hash, second.final_hash)
        self.assertEqual(first.stage_records, second.stage_records)
        self.assertEqual(
            png_encode(to_uint8(first.fused)),
            png_encode(to_uint8(second.fused)),
        )

    def test_mismatch_reports_first_pixel_region_and_magnitude(self) -> None:
        executor = Executor(PrecisionSpec.create("float64"))
        whole = execute_whole(self.control, executor)
        block = execute_block(self.control, executor, stats=whole.stats)
        corrupted = block.fused.copy()
        y, x = np.argwhere(self.control.labels > 0)[0]
        corrupted[int(y), int(x), 0] += 0.125
        broken = replace(block, fused=corrupted)
        with self.assertRaises(Exception) as caught:
            assert_block_match(whole, broken, self.control)
        error = caught.exception
        self.assertEqual(error.code, "E_BLOCK_MISMATCH")
        self.assertEqual(error.details["pixel"], [int(y), int(x)])
        self.assertIsNotNone(error.details["region"])
        self.assertGreater(error.details["magnitude"], 0.12)

    def test_error_cascade_and_dominant_source(self) -> None:
        fixed = Executor(PrecisionSpec.create("fixed"))
        block = execute_block(self.control, fixed)
        report = error_report(
            self.control.reference, block, tolerance=0.001
        )
        self.assertFalse(report["within_tolerance"])
        self.assertEqual(report["error_code"], "E_TOLERANCE_EXCEEDED")
        self.assertIn(
            report["dominant_source"],
            {"boundary_sampling", "block_completion", "style_coloring", "fixed_quantization", "feather_fusion"},
        )
        self.assertEqual(
            [item["level"] for item in report["cascade"]],
            [
                "boundary_sampling",
                "block_completion",
                "style_coloring",
                "fixed_quantization",
                "feather_fusion",
            ],
        )
        self.assertTrue(all("amplification_ratio" in row for row in report["cascade"]))

    def test_threshold_decisions_do_not_change_with_precision(self) -> None:
        hashes = set()
        for precision in ("float64", "float32", "fixed"):
            executor = Executor(PrecisionSpec.create(precision))
            whole = execute_whole(self.control, executor)
            block = execute_block(self.control, executor, stats=whole.stats)
            hashes.add(
                (
                    whole.threshold_audit.style_decision_hash,
                    whole.threshold_audit.boundary_decision_hash,
                    block.threshold_audit.style_decision_hash,
                    block.threshold_audit.boundary_decision_hash,
                )
            )
        self.assertEqual(len(hashes), 1)

    def test_invalid_threshold_configuration(self) -> None:
        raw = self.sample.style.as_dict()
        raw["threshold_policy"] = "invalid"
        with self.assertRaises(Exception) as caught:
            StyleParams.from_dict(raw)
        self.assertEqual(caught.exception.code, "E_THRESHOLD_INVALID")

        raw = self.sample.style.as_dict()
        raw["threshold_tolerance"] = -0.1
        with self.assertRaises(Exception) as caught:
            StyleParams.from_dict(raw)
        self.assertEqual(caught.exception.code, "E_THRESHOLD_INVALID")


class ServiceEngineTests(unittest.TestCase):
    def test_service_precision_run_and_tolerance_error(self) -> None:
        with tempfile.TemporaryDirectory(prefix="image-diag-service-") as temporary:
            sample_dir = Path(temporary) / "sample"
            write_sample(sample_dir)
            service = ImageDiagService(Path(temporary) / "runtime", sample_dir)
            reference = service.run_compare(precision="float64")
            self.assertTrue(reference["block_match"]["bitwise_equal"])
            self.assertEqual(reference["diagnostics"]["dominant_source"], "none")
            fixed = service.run_compare(
                precision="fixed", error_tolerance=0.001
            )
            self.assertIsNotNone(fixed["error"])
            self.assertEqual(fixed["error"]["code"], "E_TOLERANCE_EXCEEDED")

    def test_cache_is_scoped_by_precision(self) -> None:
        with tempfile.TemporaryDirectory(prefix="image-diag-cache-precision-") as temporary:
            sample_dir = Path(temporary) / "sample"
            write_sample(sample_dir)
            service = ImageDiagService(Path(temporary) / "runtime", sample_dir)
            key64 = service.run_key_for(
                service.sample,
                precision="float64",
                focus_path="block",
                tolerance=service.sample.style.error_tolerance,
            )
            hooks64 = CacheHooks(
                service.cache, service.checkpoints, run_key=key64
            )
            service.run_compare(precision="float64", hooks=hooks64)
            key32 = service.run_key_for(
                service.sample,
                precision="float32",
                focus_path="block",
                tolerance=service.sample.style.error_tolerance,
            )
            hooks32 = CacheHooks(
                service.cache, service.checkpoints, run_key=key32
            )
            result = service.run_compare(precision="float32", hooks=hooks32)
            self.assertEqual(result["cache_events"]["recomputed"], 15)
            self.assertEqual(result["cache_events"]["reused"], 0)


if __name__ == "__main__":
    unittest.main()
