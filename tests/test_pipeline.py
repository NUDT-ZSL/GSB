"""Unit and integration tests for the differentiable rendering pipeline.

Runs with the standard library only::

    .venv/bin/python -m unittest discover -s tests -v
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest

import numpy as np

from diff_render.digest import compute_digest
from diff_render.errors import (
    DigestError,
    SchemaError,
    TileError,
    WriteError,
)
from diff_render.optimizer import (
    check_convergence,
    gradient_consistency,
    project_to_domain,
)
from diff_render.scene import build_geometry
from diff_render.scheduler import evaluate_tiled
from diff_render.schema import load_config, validate_config
from diff_render.shading import (
    finite_difference_grad,
    loss_and_grad,
    render_image,
)
from diff_render.state import (
    build_envelope,
    load_latest_valid,
    save_checkpoint,
)
from diff_render.target import (
    initial_params,
    load_or_generate_target,
    truth_params,
)

SAMPLE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "samples", "sample.json")


class PipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_config(SAMPLE)
        cls.geometry = build_geometry(cls.config)
        cls.target = load_or_generate_target(cls.config, cls.geometry)

    def test_analytic_gradient_matches_finite_difference(self):
        for params in (
            initial_params(self.config),
            truth_params(self.config),
            np.array([0.4, 0.4]),
            np.array([0.1, 0.9]),
        ):
            with self.subTest(params=params):
                _, analytic, _ = loss_and_grad(params, self.geometry, self.target)
                reference = finite_difference_grad(
                    params, self.geometry, self.target,
                    self.config["gradient_check"]["step"],
                )
                abs_diff = np.abs(analytic - reference)
                tolerance = float(self.config["gradient_check"]["tolerance"])
                # Absolute agreement must be very tight everywhere.
                self.assertLess(float(np.max(abs_diff)), 1.0e-6)
                # Relative agreement is required away from the zero gradient.
                for index in range(2):
                    if abs(reference[index]) > 1.0e-3:
                        self.assertLess(
                            abs_diff[index] / abs(reference[index]), tolerance * 10
                        )

    def test_gradient_consistency_report_passes(self):
        params = initial_params(self.config)
        _, analytic, _ = loss_and_grad(params, self.geometry, self.target)
        report = gradient_consistency(
            params, self.geometry, self.target, analytic, self.config
        )
        self.assertTrue(report["passed"])
        self.assertIn(report["max_deviation_parameter"], ("albedo", "roughness"))

    def test_render_at_truth_reproduces_target(self):
        image = render_image(truth_params(self.config), self.geometry)
        np.testing.assert_array_equal(image, self.target)

    def test_serial_parallel_bitwise_identical(self):
        params = initial_params(self.config)
        serial_loss, serial_grad, _ = evaluate_tiled(
            self.config, self.geometry, params, self.target, parallel=False
        )
        parallel_loss, parallel_grad, _ = evaluate_tiled(
            self.config, self.geometry, params, self.target, parallel=True
        )
        self.assertEqual(serial_loss, parallel_loss)
        np.testing.assert_array_equal(serial_grad, parallel_grad)

    def test_concurrency_trajectory_bitwise_identical(self):
        def trajectory(workers):
            cfg = copy.deepcopy(self.config)
            cfg["scheduler"]["max_workers"] = workers
            params = initial_params(cfg)
            rates = np.array([0.01, 0.02])
            losses = []
            for _ in range(20):
                loss, grad, _ = evaluate_tiled(
                    cfg, self.geometry, params, self.target, parallel=workers > 1
                )
                losses.append(loss)
                params, _ = project_to_domain(params - rates * grad, 1)
            return losses

        baseline = trajectory(1)
        for workers in (2, 4, 8):
            self.assertEqual(baseline, trajectory(workers))

    def test_projection_records_named_events(self):
        params, events = project_to_domain(np.array([-0.5, 2.0]), 3)
        np.testing.assert_array_equal(params, [0.0, 1.0])
        names = {event["parameter"] for event in events}
        self.assertEqual(names, {"albedo", "roughness"})
        self.assertTrue(all(event["iteration"] == 3 for event in events))

    def test_convergence_thresholds(self):
        losses = [1.0, 0.5, 0.25, 0.125]
        grad_norms = [1.0, 0.001, 0.001, 0.001]
        self.assertFalse(
            check_convergence(losses, grad_norms, 1.0e-3, 1.0e-2, 3, 1.0e-8)
        )
        tiny = [1.0e-9, 8.0e-10, 6.0e-10, 5.0e-10]
        self.assertTrue(
            check_convergence(tiny, [1.0e-4] * 4, 1.0e-3, 1.0e-2, 3, 1.0e-8)
        )

    def test_schema_rejects_invalid_inputs(self):
        bad_mutations = [
            lambda c: c["camera"].__setitem__("width", 0),
            lambda c: c["material"]["truth"].__setitem__("albedo", 1.2),
            lambda c: c["material"]["truth"].__setitem__("roughness", 0.0),
            lambda c: c["lights"][0].__setitem__("direction", [1.0, 1.0, 1.0]),
            lambda c: c["optimization"].__setitem__("iterations", 0),
            lambda c: c["gradient_check"].__setitem__("tolerance", 0.0),
        ]
        for mutate in bad_mutations:
            cfg = copy.deepcopy(self.config)
            mutate(cfg)
            with self.subTest():
                with self.assertRaises(SchemaError):
                    validate_config(cfg)

    def test_memory_budget_enforced(self):
        cfg = copy.deepcopy(self.config)
        cfg["memory"]["buffer_byte_budget"] = 1024
        with self.assertRaises(TileError):
            evaluate_tiled(
                cfg, self.geometry, initial_params(cfg), self.target, parallel=True
            )

    def test_tile_failure_cancels_and_returns_first_error(self):
        from diff_render import scheduler as scheduler_module

        original = scheduler_module._compute_tile

        def flaky(tile, *args, **kwargs):
            if tile.index == 4:
                raise RuntimeError("injected failure")
            return original(tile, *args, **kwargs)

        scheduler_module._compute_tile = flaky
        try:
            with self.assertRaises(TileError) as context:
                evaluate_tiled(
                    self.config, self.geometry,
                    initial_params(self.config), self.target, parallel=True,
                )
            self.assertEqual(context.exception.details["tile_index"], 4)
        finally:
            scheduler_module._compute_tile = original

    def test_checkpoint_digest_and_rollback(self):
        with tempfile.TemporaryDirectory() as state_dir:
            digest = compute_digest(self.config, self.target)
            payload = {"iteration": 5, "params": {"albedo": 0.3, "roughness": 0.4},
                       "losses": [1.0], "grad_norms": [0.1],
                       "projection_events": []}
            save_checkpoint(state_dir, digest, payload)
            restored, diagnostics = load_latest_valid(state_dir, digest)
            self.assertEqual(restored["iteration"], 5)
            self.assertEqual(diagnostics[0]["status"], "restored")

            # Tamper with the payload while keeping the stale digest.
            state_path = os.path.join(state_dir, "state.json")
            envelope = json.load(open(state_path))
            envelope["payload"]["iteration"] = 99
            json.dump(envelope, open(state_path, "w"))
            restored_backup, _ = load_latest_valid(state_dir, digest)
            self.assertEqual(restored_backup["iteration"], 5)

    def test_digest_mismatch_raises_when_no_valid_state(self):
        with tempfile.TemporaryDirectory() as state_dir:
            envelope = build_envelope("deadbeef", {
                "iteration": 1, "params": {"albedo": 0.1, "roughness": 0.1},
                "losses": [], "grad_norms": [], "projection_events": [],
            })
            with open(os.path.join(state_dir, "state.json"), "w") as handle:
                json.dump(envelope, handle)
            with self.assertRaises(DigestError):
                load_latest_valid(
                    state_dir, compute_digest(self.config, self.target)
                )

    def test_version_change_is_archived_not_overwritten(self):
        with tempfile.TemporaryDirectory() as state_dir:
            old_payload = {"iteration": 3, "params": {"albedo": 0.2, "roughness": 0.5},
                           "losses": [1.0], "grad_norms": [0.2],
                           "projection_events": []}
            save_checkpoint(state_dir, "old-version", old_payload)
            new_payload = dict(old_payload, iteration=4)
            save_checkpoint(state_dir, "new-version", new_payload)
            with open(os.path.join(state_dir, "state.archive.json")) as handle:
                archive = json.load(handle)
            self.assertEqual(archive["version_digest"], "old-version")
            with open(os.path.join(state_dir, "state.json")) as handle:
                current = json.load(handle)
            self.assertEqual(current["version_digest"], "new-version")

    def test_atomic_write_failure_keeps_old_file(self):
        from diff_render.io_utils import atomic_write_bytes

        with tempfile.TemporaryDirectory() as directory:
            target = os.path.join(directory, "result.bin")
            atomic_write_bytes(target, b"original")
            os.makedirs(os.path.join(directory, "blocked"))
            blocked = os.path.join(directory, "blocked", "result.bin")
            os.makedirs(blocked)  # directory occupying the file path
            with self.assertRaises(WriteError):
                atomic_write_bytes(blocked, b"new")
            self.assertEqual(open(target, "rb").read(), b"original")


if __name__ == "__main__":
    unittest.main()
