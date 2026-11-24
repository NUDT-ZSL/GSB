from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from halftone import cli
from halftone.config import validate_job
from halftone.engine import (
    MemoryBudget,
    SourceLibrary,
    TerminationToken,
    process_validated_job,
    run_batch,
)
from halftone.errors import BusinessError, TerminationRequested
from halftone.imaging import (
    first_consistency_mismatch,
    rational_resample,
    render_halftone,
    require_consistent,
)
from halftone.pngio import read_png
from halftone.seed import generate_samples
from halftone.state import StateStore


def base_job(**overrides):
    raw = {
        "job_id": "unit_job",
        "source_id": "src",
        "output_width": 48,
        "output_height": 32,
        "dpi": 240,
        "lpi": 20,
        "angle_deg": 18.37,
        "dot_shape": "circle",
        "ink_tone": "#123456",
        "ink_coverage_max": 0.95,
        "levels": 4,
        "tile_cols": 3,
        "tile_rows": 2,
    }
    raw.update(overrides)
    return validate_job(raw)


def sample_gray(width: int = 48, height: int = 32) -> np.ndarray:
    x = np.linspace(0, 255, width, dtype=np.float64)[None, :]
    y = np.linspace(0, 120, height, dtype=np.float64)[:, None]
    gray = (x + y + 30 * np.sin(x / 7.0)) % 256
    return np.floor(gray).astype(np.uint8)


class ValidationTests(unittest.TestCase):
    def expect_code(self, code: str, **overrides) -> None:
        with self.assertRaises(BusinessError) as context:
            base_job(**overrides)
        self.assertEqual(context.exception.code, code)

    def test_field_business_codes(self):
        self.expect_code("E_ANGLE", angle_deg=90)
        self.expect_code("E_ANGLE", angle_deg=-0.1)
        self.expect_code("E_GRID", tile_cols=5)
        self.expect_code("E_GRID", tile_rows=3)
        self.expect_code("E_SHAPE", dot_shape=None)
        self.expect_code("E_SHAPE", dot_shape="diamond")
        self.expect_code("E_COVERAGE", ink_coverage_max=1.01)
        self.expect_code("E_COVERAGE", ink_coverage_max=-0.01)
        self.expect_code("E_LPI", lpi=0)
        self.expect_code("E_LPI", lpi=float("nan"))
        self.expect_code("E_RESOLUTION", output_width=0)
        self.expect_code("E_RESOLUTION", output_height=-4)


class HalftoneEquivalenceTests(unittest.TestCase):
    def test_pitch_formula_and_determinism(self):
        job = base_job(dpi=300, lpi=25)
        self.assertEqual(job.mesh_pitch_px, 12.0)
        gray = sample_gray()
        first = render_halftone(gray, job)
        second = render_halftone(gray, job)
        np.testing.assert_array_equal(first, second)

    def test_tiles_equal_full_for_all_shapes_and_levels(self):
        gray = sample_gray()
        for shape in ("circle", "ellipse", "square"):
            for levels in (2, 6):
                with self.subTest(shape=shape, levels=levels):
                    job = base_job(dot_shape=shape, levels=levels)
                    full = render_halftone(gray, job)
                    assembled = np.full_like(full, 255)
                    for tile_y in range(job.tile_rows):
                        for tile_x in range(job.tile_cols):
                            x0 = tile_x * job.tile_width
                            y0 = tile_y * job.tile_height
                            tile = render_halftone(
                                gray[
                                    y0 : y0 + job.tile_height,
                                    x0 : x0 + job.tile_width,
                                ],
                                job,
                                origin_x=x0,
                                origin_y=y0,
                            )
                            assembled[
                                y0 : y0 + job.tile_height,
                                x0 : x0 + job.tile_width,
                            ] = tile
                    np.testing.assert_array_equal(assembled, full)
                    self.assertLessEqual(len(np.unique(full)), levels)

    def test_single_pixel_global_origin_matches_full(self):
        gray = sample_gray()
        job = base_job(dot_shape="square", levels=2)
        full = render_halftone(gray, job)
        for y, x in ((0, 0), (7, 13), (15, 31), (31, 47)):
            with self.subTest(x=x, y=y):
                pixel = render_halftone(
                    gray[y : y + 1, x : x + 1],
                    job,
                    origin_x=x,
                    origin_y=y,
                )
                self.assertEqual(int(pixel[0, 0]), int(full[y, x]))

    def test_zero_coverage_is_paper_white(self):
        job = base_job(ink_coverage_max=0.0)
        result = render_halftone(np.zeros((8, 8), dtype=np.uint8), job)
        np.testing.assert_array_equal(result, np.full((8, 8), 255, np.uint8))


class ConsistencyTests(unittest.TestCase):
    def test_first_mismatch_details_and_code(self):
        job = base_job()
        reference = render_halftone(sample_gray(), job)
        assembled = reference.copy().astype(np.int16)
        replacement = 0 if reference[5, 7] != 0 else 255
        assembled[5, 7] = replacement
        mismatch = first_consistency_mismatch(assembled, reference, job)
        self.assertIsNotNone(mismatch)
        assert mismatch is not None
        self.assertEqual((mismatch["x"], mismatch["y"]), (7, 5))
        self.assertEqual((mismatch["tile_x"], mismatch["tile_y"]), (0, 0))
        self.assertEqual(mismatch["tile_index"], 0)
        self.assertEqual(mismatch["tile_value"], replacement)
        self.assertEqual(mismatch["full_value"], int(reference[5, 7]))
        with self.assertRaises(BusinessError) as context:
            require_consistent(assembled, reference, job)
        self.assertEqual(context.exception.code, "E_CONSIST")
        self.assertEqual(
            context.exception.details["first_mismatch"]["x"], 7
        )


class ResampleTests(unittest.TestCase):
    def test_rational_resample_is_deterministic(self):
        source = sample_gray(37, 29)
        first = rational_resample(source, 64, 48)
        second = rational_resample(source, 64, 48)
        self.assertEqual(first.shape, (48, 64))
        np.testing.assert_array_equal(first, second)


class StateAndSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = StateStore(self.root / "state")
        self.out = self.root / "out"
        self.gray = sample_gray(64, 64)
        self.job = base_job(
            job_id="state_job",
            output_width=64,
            output_height=64,
            tile_cols=2,
            tile_rows=2,
        )

    def tearDown(self):
        self.temp.cleanup()

    def process(self, **kwargs):
        options = {
            "state": self.state,
            "out_dir": self.out,
            "workers": 2,
        }
        options.update(kwargs)
        return process_validated_job(self.job, self.gray, **options)

    def test_compute_reuse_and_version_archive(self):
        first = self.process()
        self.assertEqual(first["computed_tiles"], 4)
        self.assertEqual(first["reused_tiles"], 0)
        self.assertTrue((self.out / "state_job.png").exists())

        second = self.process(workers=4)
        self.assertEqual(second["computed_tiles"], 0)
        self.assertEqual(second["reused_tiles"], 4)

        changed = base_job(
            job_id="state_job",
            output_width=64,
            output_height=64,
            tile_cols=2,
            tile_rows=2,
            angle_deg=27.5,
        )
        result = process_validated_job(
            changed, self.gray, state=self.state, out_dir=self.out
        )
        self.assertIsNotNone(result["archived_from_version"])
        archive = self.state.job_dir("state_job") / "archive"
        self.assertTrue(archive.exists())
        self.assertTrue(any(archive.iterdir()))

    def test_corrupt_digest_returns_distinct_code(self):
        self.process()
        self.state.corrupt_digest_for_injection("state_job", 0)
        with self.assertRaises(BusinessError) as context:
            self.process()
        self.assertEqual(context.exception.code, "E_DIGEST")

    def test_memory_budget_limits_in_flight_and_completes(self):
        tile_bytes = self.job.tile_width * self.job.tile_height
        budget = MemoryBudget(2 * tile_bytes)
        result = self.process(
            workers=4, budget=budget, tile_delay_ms=25
        )
        self.assertTrue(result["ok"])
        self.assertLessEqual(budget.peak_bytes, 2 * tile_bytes)
        self.assertGreaterEqual(budget.wait_count, 1)

    def test_graceful_termination_and_resume(self):
        token = TerminationToken(stop_after_completed=1)
        with self.assertRaises(TerminationRequested):
            self.process(workers=1, token=token)
        valid_tile = self.state.tile_path("state_job", 0)
        self.assertTrue(valid_tile.exists())

        resumed = self.process(workers=1)
        self.assertEqual(resumed["reused_tiles"], 1)
        self.assertEqual(resumed["computed_tiles"], 3)

    def test_worker_count_outputs_are_identical(self):
        one = self.root / "workers_one"
        four = self.root / "workers_four"
        process_validated_job(
            self.job,
            self.gray,
            state=StateStore(one / "state"),
            out_dir=one / "out",
            workers=1,
        )
        process_validated_job(
            self.job,
            self.gray,
            state=StateStore(four / "state"),
            out_dir=four / "out",
            workers=4,
        )
        first = read_png(one / "out" / "state_job.png")
        concurrent = read_png(four / "out" / "state_job.png")
        np.testing.assert_array_equal(first, concurrent)


class CliIntegrationTests(unittest.TestCase):
    def test_seed_anomaly_batch_recovery_and_termination(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            generate_samples(root)
            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = cli.main(
                    ["run", "--root", str(root), "--workers", "2", "--check"]
                )
            self.assertEqual(exit_code, 0)
            report = json.loads((root / "out" / "report.json").read_text("utf-8"))
            self.assertEqual(report["successful_jobs"], 5)
            self.assertEqual(report["injection_summary"]["matched"], 4)
            self.assertTrue(report["injection_summary"]["all_matched"])
            codes = {
                item["job_id"]: item["actual_code"]
                for item in report["injection_results"]
            }
            self.assertEqual(
                codes,
                {
                    "inj_bad_angle": "E_ANGLE",
                    "inj_bad_grid": "E_GRID",
                    "inj_missing_shape": "E_SHAPE",
                    "inj_corrupt_digest": "E_DIGEST",
                },
            )
            self.assertEqual(len(list((root / "out").glob("job_*.png"))), 10)
            self.assertFalse(any(root.rglob("*.tmp")))

            with contextlib.redirect_stdout(io.StringIO()):
                second_code = cli.main(
                    ["run", "--root", str(root), "--workers", "4", "--check"]
                )
            self.assertEqual(second_code, 0)
            second = json.loads((root / "out" / "report.json").read_text("utf-8"))
            circle = next(
                job
                for job in second["jobs"]
                if job.get("job_id") == "job_circle_320x240"
            )
            self.assertEqual(circle["computed_tiles"], 0)
            self.assertEqual(circle["reused_tiles"], 12)

            with contextlib.redirect_stdout(io.StringIO()):
                terminated_code = cli.main(
                    [
                        "run",
                        "--root",
                        str(root / "term"),
                        "--jobs",
                        str(root / "samples" / "jobs.json"),
                        "--manifest",
                        str(root / "samples" / "images.json"),
                        "--state",
                        str(root / "term" / "state"),
                        "--out",
                        str(root / "term" / "out"),
                        "--report",
                        str(root / "term" / "report.json"),
                        "--no-inject",
                        "--workers",
                        "1",
                        "--term-after-tiles",
                        "2",
                    ]
                )
            self.assertEqual(terminated_code, 20)
            term_report = json.loads(
                (root / "term" / "report.json").read_text("utf-8")
            )
            self.assertEqual(term_report["status"], "terminated")

            with contextlib.redirect_stdout(io.StringIO()):
                resume_code = cli.main(
                    [
                        "run",
                        "--root",
                        str(root / "term"),
                        "--jobs",
                        str(root / "samples" / "jobs.json"),
                        "--manifest",
                        str(root / "samples" / "images.json"),
                        "--state",
                        str(root / "term" / "state"),
                        "--out",
                        str(root / "term" / "out"),
                        "--report",
                        str(root / "term" / "report.json"),
                        "--no-inject",
                        "--check",
                    ]
                )
            self.assertEqual(resume_code, 0)


class BatchContinuationTests(unittest.TestCase):
    def test_one_bad_job_does_not_stop_others(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            generate_samples(root)
            manifest = load_manifest(root / "samples" / "images.json")
            sources = SourceLibrary(manifest, root / "samples")
            jobs = [
                {
                    "job_id": "bad",
                    "source_id": "gradient_wrap",
                    "angle_deg": 91,
                    "lpi": 12,
                    "output_width": 10,
                    "output_height": 10,
                },
                {
                    "job_id": "good",
                    "source_id": "gradient_wrap",
                    "output_width": 32,
                    "output_height": 24,
                    "lpi": 12,
                    "angle_deg": 10,
                    "dot_shape": "circle",
                    "ink_coverage_max": 1,
                    "tile_cols": 2,
                    "tile_rows": 2,
                },
            ]
            report = run_batch(
                jobs,
                sources,
                state=StateStore(root / "state"),
                out_dir=root / "out",
                workers=1,
            )
            self.assertEqual(report["successful_jobs"], 1)
            self.assertEqual(report["failed_jobs"], 1)
            self.assertEqual(report["jobs"][0]["code"], "E_ANGLE")


def load_manifest(path: Path):
    from halftone.config import load_source_manifest

    return load_source_manifest(path)


if __name__ == "__main__":
    unittest.main()
