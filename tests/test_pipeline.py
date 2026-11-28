"""Acceptance and unit tests for the deterministic focus-stacking kernel."""

from __future__ import annotations

import tempfile
import unittest

import numpy as np

from focus_stack.config import CoreConfig
from focus_stack.filters import box_filter, box_filter_region
from focus_stack.incremental import incremental_add_plane, incremental_replace
from focus_stack.io_stack import FocusStack, MetadataError
from focus_stack.persistence import (
    CheckpointError,
    load_result,
    resume_or_compute,
    save_result,
)
from focus_stack.pipeline import run_full
from focus_stack.reference import reference_fuse
from focus_stack.reporting import build_report
from focus_stack.samples import SampleSpec, generate_stack


def make_stack(seed: int = 11, height: int = 32, width: int = 36, planes: int = 5):
    spec = SampleSpec(
        "unit_stack",
        height=height,
        width=width,
        n_planes=planes,
        seed=seed,
    )
    return generate_stack(spec)


class SampleGeneratorTests(unittest.TestCase):
    def test_replay_is_bit_identical(self):
        spec = SampleSpec("replay", height=24, width=28, n_planes=4, seed=99)
        first = generate_stack(spec)
        second = generate_stack(spec)
        for a, b in zip(first.frames, second.frames):
            self.assertTrue(np.array_equal(a, b))
        self.assertTrue(np.array_equal(first.ideal_plane, second.ideal_plane))
        self.assertTrue(
            np.array_equal(first.sharpness_reference, second.sharpness_reference)
        )

    def test_frames_are_integer_and_record_ground_truth(self):
        stack = make_stack()
        for frame in stack.frames:
            self.assertTrue(np.issubdtype(frame.dtype, np.integer))
        self.assertEqual(stack.ideal_plane.shape, stack.shape)
        self.assertEqual(
            stack.sharpness_reference.shape, (stack.n_planes,) + stack.shape
        )
        self.assertTrue(np.all(stack.ideal_plane >= 0))
        self.assertTrue(np.all(stack.ideal_plane < stack.n_planes))


class MetadataValidationTests(unittest.TestCase):
    def test_accepts_valid_stack_and_summarises(self):
        stack = make_stack()
        self.assertEqual(len(stack.summaries), stack.n_planes)
        for summary in stack.summaries:
            self.assertGreaterEqual(summary.variance, 0.0)
            self.assertTrue(np.isfinite(summary.laplacian_energy))

    def _base_kwargs(self, stack):
        return dict(
            stack_id=stack.stack_id,
            frames=stack.frames,
            focus_distances=stack.focus_distances,
            magnifications=stack.magnifications,
            dof_ranges=stack.dof_ranges,
        )

    def test_rejects_shape_mismatch(self):
        stack = make_stack()
        kwargs = self._base_kwargs(stack)
        frames = list(kwargs["frames"])
        frames[1] = frames[1][:-2, :]
        kwargs["frames"] = frames
        with self.assertRaisesRegex(MetadataError, "shape"):
            FocusStack(**kwargs)

    def test_rejects_dof_gap(self):
        stack = make_stack()
        kwargs = self._base_kwargs(stack)
        dof = np.array(kwargs["dof_ranges"], copy=True)
        dof[:] = [0.0, 0.01]  # no longer covers the focus distances
        kwargs["dof_ranges"] = dof
        with self.assertRaisesRegex(MetadataError, "DOF"):
            FocusStack(**kwargs)

    def test_rejects_non_monotonic_magnification(self):
        stack = make_stack()
        kwargs = self._base_kwargs(stack)
        mag = np.array(kwargs["magnifications"], copy=True)
        mag[1], mag[2] = mag[2], mag[1]  # break monotonic ordering
        kwargs["magnifications"] = mag
        with self.assertRaisesRegex(MetadataError, "magnification"):
            FocusStack(**kwargs)

    def test_rejects_non_integer_frame(self):
        stack = make_stack()
        kwargs = self._base_kwargs(stack)
        frames = list(kwargs["frames"])
        frames[0] = frames[0].astype(np.float64)
        kwargs["frames"] = frames
        with self.assertRaisesRegex(MetadataError, "integer"):
            FocusStack(**kwargs)


class FilterEquivalenceTests(unittest.TestCase):
    def test_region_box_filter_matches_full(self):
        rng = np.random.default_rng(0)
        image = rng.normal(size=(20, 24))
        radius = 3
        full = box_filter(image, radius)
        bounds = (4, 15, 6, 19)
        region = box_filter_region(image, radius, *bounds)
        r0, r1, c0, c1 = bounds
        self.assertTrue(np.array_equal(region, full[r0:r1, c0:c1]))


class ReferenceEquivalenceTests(unittest.TestCase):
    def test_pipeline_matches_independent_reference(self):
        config = CoreConfig()
        stack = make_stack(seed=17)
        result = run_full(stack, config)
        reference = reference_fuse(stack, config)
        self.assertTrue(np.array_equal(result.sharpness, reference.sharpness))
        self.assertTrue(np.array_equal(result.initial_map, reference.initial_map))
        self.assertTrue(np.array_equal(result.selected_map, reference.selected_map))
        self.assertTrue(
            np.allclose(result.fused, reference.fused, atol=config.compare_tol)
        )
        self.assertTrue(
            np.allclose(result.ledger, reference.ledger, atol=config.compare_tol)
        )


class ConsistencyAndLedgerTests(unittest.TestCase):
    def test_ledger_normalised_and_confidence_bounded(self):
        config = CoreConfig()
        stack = make_stack()
        result = run_full(stack, config)
        sums = result.ledger.sum(axis=0)
        self.assertTrue(np.allclose(sums, 1.0, atol=1e-12))
        self.assertTrue(np.all(result.confidence >= 0.0))
        self.assertTrue(np.all(result.confidence <= 1.0))
        self.assertEqual(result.boundary_band.dtype, bool)

    def test_smoothing_only_uses_recorded_planes(self):
        config = CoreConfig()
        stack = make_stack()
        result = run_full(stack, config)
        self.assertTrue(
            np.all(result.selected_map >= 0)
            and np.all(result.selected_map < stack.n_planes)
        )


class IncrementalTests(unittest.TestCase):
    def test_localised_incremental_matches_full_recompute_exactly(self):
        config = CoreConfig()
        stack = make_stack(seed=23, height=64, width=72, planes=5)
        baseline = run_full(stack, config)
        rng = np.random.default_rng(7)
        replacement = stack.frames[2].copy()
        replacement[28:36, 30:40] = np.clip(
            replacement[28:36, 30:40].astype(np.int64)
            + rng.integers(-80, 80, size=(8, 10)),
            0,
            4095,
        ).astype(np.uint16)
        changed = stack.replace_frame(2, replacement)
        inc = incremental_replace(stack, changed, 2, baseline, config)

        outside = ~inc.affected_mask
        # Inside the propagation region the incremental result is bit identical
        # to a from-scratch full recomputation; outside it stays bit identical
        # to the previous baseline.
        inside = inc.affected_mask
        for field in (
            "fused",
            "selected_map",
            "confidence",
            "boundary_band",
            "initial_map",
        ):
            self.assertTrue(
                np.array_equal(
                    getattr(inc.result, field)[inside],
                    getattr(inc.full_recompute, field)[inside],
                ),
                field,
            )
        self.assertTrue(
            np.array_equal(
                inc.result.ledger[:, inside],
                inc.full_recompute.ledger[:, inside],
            )
        )
        self.assertTrue(
            np.array_equal(baseline.fused[outside], inc.result.fused[outside])
        )
        self.assertTrue(
            np.array_equal(
                baseline.selected_map[outside], inc.result.selected_map[outside]
            )
        )
        self.assertTrue(
            np.array_equal(
                baseline.ledger[:, outside], inc.result.ledger[:, outside]
            )
        )
        self.assertLess(int(inc.affected_mask.sum()), int(outside.size))
        self.assertGreater(int(outside.sum()), 0)

    def test_identical_frame_marks_no_work(self):
        config = CoreConfig()
        stack = make_stack()
        baseline = run_full(stack, config)
        inc = incremental_replace(
            stack, stack, 0, baseline, config, run_validation=False
        )
        self.assertEqual(inc.recompute_bounds, (0, 0, 0, 0))
        self.assertEqual(int(inc.change_mask.sum()), 0)

    def test_ledger_records_recompute_region_and_version(self):
        config = CoreConfig()
        stack = make_stack(seed=31, height=56, width=60, planes=5)
        baseline = run_full(stack, config)
        rng = np.random.default_rng(1)
        replacement = stack.frames[1].copy()
        replacement[24:30, 26:34] = np.clip(
            replacement[24:30, 26:34].astype(np.int64)
            + rng.integers(-70, 70, size=(6, 8)),
            0,
            4095,
        ).astype(np.uint16)
        changed = stack.replace_frame(1, replacement)
        inc = incremental_replace(stack, changed, 1, baseline, config)
        self.assertEqual(inc.result.ledger_version, baseline.ledger_version + 1)
        self.assertTrue(
            np.array_equal(
                inc.result.ledger_recompute_mask, inc.affected_mask
            )
        )

    def test_add_plane_recomputes_and_annotates_full_image(self):
        config = CoreConfig()
        stack = make_stack(seed=41, planes=4)
        baseline = run_full(stack, config)
        new_frame = stack.frames[-1].copy()
        added = stack.add_frame(
            new_frame,
            focus_distance=0.95,
            magnification=float(stack.magnifications[-1]) + 0.05,
            dof_range=(0.9, 1.0),
        )
        inc = incremental_add_plane(stack, added, baseline, config)
        self.assertEqual(inc.result.ledger.shape[0], added.n_planes)
        self.assertTrue(inc.affected_mask.all())
        self.assertEqual(inc.result.ledger_version, baseline.ledger_version + 1)
        self.assertTrue(
            np.array_equal(inc.result.fused, inc.full_recompute.fused)
        )


class ReportTests(unittest.TestCase):
    def test_report_partitions_and_evidence(self):
        config = CoreConfig()
        stack = make_stack()
        result = run_full(stack, config)
        report = build_report(stack, result, config)
        regions = report["regions"]
        high = regions["high_confidence"]["pixel_count"]
        band = regions["uncertain_boundary_band"]["pixel_count"]
        self.assertEqual(high + band, stack.shape[0] * stack.shape[1])
        full = regions["full_image"]
        self.assertIn("mismatch_count", full)
        self.assertIn("local_contrast_improvement_ratio", full)
        total_distribution = sum(full["plane_distribution"].values())
        self.assertEqual(total_distribution, stack.shape[0] * stack.shape[1])


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_save_resume_and_reject_changed_input(self):
        config = CoreConfig()
        stack = make_stack()
        result = run_full(stack, config)
        save_result(self.tmp.name, "seq", 1, stack, config, result)
        loaded, manifest = load_result(self.tmp.name, "seq", 1, stack, config)
        self.assertTrue(np.array_equal(loaded.fused, result.fused))
        self.assertEqual(manifest["version"], 1)

        altered = stack.replace_frame(
            0,
            np.clip(
                stack.frames[0].astype(np.int64) + 1, 0, 4095
            ).astype(np.uint16),
        )
        with self.assertRaises(CheckpointError):
            load_result(self.tmp.name, "seq", 1, altered, config)

    def test_resume_or_compute_uses_cache_then_recomputes(self):
        config = CoreConfig()
        stack = make_stack()
        calls = {"count": 0}

        def compute():
            calls["count"] += 1
            return run_full(stack, config)

        first, version, resumed = resume_or_compute(
            self.tmp.name, "seq2", stack, config, compute
        )
        self.assertFalse(resumed)
        self.assertEqual(version, 1)
        second, _, resumed_two = resume_or_compute(
            self.tmp.name, "seq2", stack, config, compute
        )
        self.assertTrue(resumed_two)
        self.assertEqual(calls["count"], 1)
        self.assertTrue(np.array_equal(first.fused, second.fused))

    def test_ledger_annotation_round_trips(self):
        config = CoreConfig()
        stack = make_stack(seed=53, height=48, width=52, planes=5)
        baseline = run_full(stack, config)
        rng = np.random.default_rng(2)
        replacement = stack.frames[0].copy()
        replacement[20:26, 18:28] = np.clip(
            replacement[20:26, 18:28].astype(np.int64)
            + rng.integers(-60, 60, size=(6, 10)),
            0,
            4095,
        ).astype(np.uint16)
        changed = stack.replace_frame(0, replacement)
        inc = incremental_replace(stack, changed, 0, baseline, config)
        save_result(self.tmp.name, "annotated", 2, changed, config, inc.result)
        loaded, _ = load_result(self.tmp.name, "annotated", 2, changed, config)
        self.assertEqual(loaded.ledger_version, inc.result.ledger_version)
        self.assertTrue(
            np.array_equal(
                loaded.ledger_recompute_mask, inc.result.ledger_recompute_mask
            )
        )


if __name__ == "__main__":
    unittest.main()
