"""GPU 调度诊断工作台的确定性单元测试，仅使用标准库 unittest。"""

import json
import os
import shutil
import tempfile
import unittest

from gpu_diag.errors import (
    BusinessError, E_MANIFEST, E_ESTIMATE, E_QUOTA, E_PLACEMENT, E_REPLAY,
)
from gpu_diag.estimator import estimate_job
from gpu_diag.manifest import load_manifest
from gpu_diag.models import ScaleParams
from gpu_diag.scheduler import run_schedule
from gpu_diag.session import Session

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MANIFEST = os.path.join(ROOT, "samples", "manifest.json")
INVALID = os.path.join(ROOT, "samples", "invalid_manifest.json")


class ManifestValidationTests(unittest.TestCase):
    def test_valid_manifest_loads(self):
        manifest = load_manifest(MANIFEST)
        self.assertEqual(len(manifest.pools), 2)
        self.assertEqual(len(manifest.jobs), 9)
        self.assertIsInstance(manifest.scale_params, ScaleParams)

    def test_invalid_manifest_aggregates_all_issues(self):
        with self.assertRaises(BusinessError) as context:
            load_manifest(INVALID)
        error = context.exception
        self.assertEqual(error.code, E_MANIFEST)
        paths = [item["field_path"] for item in error.details["issues"]]
        self.assertIn("$.pools[0].card_count", paths)
        self.assertIn("$.pools[1].name", paths)
        self.assertIn("$.jobs[0].quota_group", paths)
        self.assertGreaterEqual(len(paths), 8)

    def test_directory_manifest(self):
        tmp = tempfile.mkdtemp()
        try:
            raw = json.load(open(MANIFEST, encoding="utf-8"))
            with open(os.path.join(tmp, "pools.json"), "w", encoding="utf-8") as fh:
                json.dump(raw["pools"], fh)
            with open(os.path.join(tmp, "quotas.json"), "w", encoding="utf-8") as fh:
                json.dump(raw["quotas"], fh)
            with open(os.path.join(tmp, "jobs.json"), "w", encoding="utf-8") as fh:
                json.dump(raw["jobs"], fh)
            with open(os.path.join(tmp, "scale_params.json"), "w", encoding="utf-8") as fh:
                json.dump({"scale_bounds": raw["scale_bounds"],
                           "scale_params": raw["scale_params"]}, fh)
            manifest = load_manifest(tmp)
            self.assertEqual(len(manifest.jobs), 9)
        finally:
            shutil.rmtree(tmp)


class EstimatorTests(unittest.TestCase):
    def setUp(self):
        self.manifest = load_manifest(MANIFEST)
        self.job = self.manifest.jobs[0]

    def test_estimate_is_deterministic_integer(self):
        params = self.manifest.scale_params
        first = estimate_job(self.job, params, 1, self.manifest.scale_bounds)
        second = estimate_job(self.job, params, 1, self.manifest.scale_bounds)
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertIsInstance(first.per_card_mib, int)
        self.assertEqual(first.total_mib, first.per_card_mib * first.card_count)
        self.assertEqual(first.components["margin_mib"],
                         (first.components["raw_mib"] * self.job.curve.safety_pct + 99) // 100)

    def test_estimate_formula_components(self):
        params = ScaleParams(32, 1024, 1024)
        result = estimate_job(self.job, params, 1, self.manifest.scale_bounds)
        components = result.components
        self.assertEqual(components["tokens"], 32 * 1024)
        self.assertEqual(components["activation_elements"], 32 * 1024 * 1024)
        self.assertEqual(components["parameter_count"], 1024 * 1024)

    def test_out_of_range_parameter_raises_e_estimate(self):
        params = ScaleParams(32, 1024, 99999)
        with self.assertRaises(BusinessError) as context:
            estimate_job(self.job, params, 1, self.manifest.scale_bounds)
        self.assertEqual(context.exception.code, E_ESTIMATE)
        self.assertEqual(context.exception.field_path, "$.scale_params.model_dim")


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.session = Session(load_manifest(MANIFEST))

    def test_deterministic_repeatability(self):
        again = Session(load_manifest(MANIFEST))
        self.assertEqual(self.session.current_run.to_dict(), again.current_run.to_dict())

    def test_quota_dimensions_recorded(self):
        by_id = {item.job_id: item for item in self.session.current_run.results}
        card_reject = by_id["j-alpha-3"].reasons[0]
        self.assertEqual(card_reject.code, E_QUOTA)
        self.assertEqual(card_reject.dimension, "cards")
        self.assertEqual(card_reject.current_usage, 6)
        self.assertEqual(card_reject.limit, 6)
        memory_reject = by_id["j-beta-2"].reasons[0]
        self.assertEqual(memory_reject.code, E_QUOTA)
        self.assertEqual(memory_reject.dimension, "memory_mib")
        self.assertEqual(memory_reject.requested, by_id["j-beta-2"].estimate.total_mib)

    def test_topology_placement_and_occupancy(self):
        by_id = {item.job_id: item for item in self.session.current_run.results}
        self.assertTrue(by_id["j-alpha-1"].admitted)
        self.assertEqual(by_id["j-alpha-1"].slots, [0, 1, 2, 3])
        self.assertEqual(by_id["j-alpha-1"].topology_group, "rack-a1")
        self.assertEqual(by_id["j-gamma-3"].reasons[0].code, E_PLACEMENT)
        self.assertEqual(by_id["j-gamma-3"].reasons[0].dimension, "topology")
        all_slots = []
        for result in self.session.current_run.results:
            if result.admitted and result.pool_name == "pool-a":
                all_slots.extend(result.slots)
        self.assertEqual(len(all_slots), len(set(all_slots)))

    def test_card_memory_capacity_rejects_placement(self):
        manifest = load_manifest(MANIFEST)
        pool = manifest.pools[0]
        original = pool.card_memory_mib
        pool.card_memory_mib = 16
        run = run_schedule(
            manifest.jobs, {item.name: item for item in manifest.pools},
            manifest.quotas, manifest.scale_params, 1,
            scale_bounds=manifest.scale_bounds)
        first = run.results[0]
        self.assertEqual(first.reasons[0].code, E_PLACEMENT)
        self.assertEqual(first.reasons[0].dimension, "card_memory_mib")
        pool.card_memory_mib = original

    def test_drained_cards_are_unavailable(self):
        self.session.set_drained("pool-a", 0, True)
        alpha = next(item for item in self.session.current_run.results if item.job_id == "j-alpha-1")
        self.assertEqual(alpha.slots, [4, 5, 6, 7])
        self.assertNotIn(0, alpha.slots)


class RescaleTests(unittest.TestCase):
    def setUp(self):
        self.session = Session(load_manifest(MANIFEST))

    def test_rescale_bumps_version_and_marks_stale(self):
        old_version = self.session.parameter_version
        self.session.update_scale(batch_size=128)
        self.assertEqual(self.session.parameter_version, old_version + 1)
        self.assertEqual(len(self.session.stale_runs), 1)
        self.assertEqual(self.session.stale_runs[0]["parameter_version"], old_version)
        for result in self.session.current_run.results:
            self.assertEqual(result.estimate.parameter_version, self.session.parameter_version)

    def test_rescale_changes_decisions_deterministically(self):
        self.session.update_scale(batch_size=128)
        by_id = {item.job_id: item for item in self.session.current_run.results}
        self.assertFalse(by_id["j-beta-3"].admitted)
        self.assertEqual(by_id["j-beta-3"].reasons[0].dimension, "memory_mib")
        again = Session(load_manifest(MANIFEST))
        again.update_scale(batch_size=128)
        self.assertEqual(self.session.current_run.to_dict(), again.current_run.to_dict())

    def test_invalid_rescale_raises_e_estimate(self):
        with self.assertRaises(BusinessError) as context:
            self.session.update_scale(model_dim=99999)
        self.assertEqual(context.exception.code, E_ESTIMATE)


class SnapshotReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.session = Session(load_manifest(MANIFEST))
        self.path = self.session.save_snapshot("v1", self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_snapshot_roundtrip_full_replace(self):
        self.session.update_scale(batch_size=128)
        restored, info = Session.load_snapshot(self.path)
        self.assertEqual(info["restore_mode"], "full_replace")
        self.assertEqual(restored.parameter_version, 1)
        self.assertEqual(restored.current_run.to_dict(),
                         self.session.stale_runs[-1])

    def test_replay_reproduces_recorded_run(self):
        report = Session.replay_snapshot(self.path)
        self.assertEqual(report["code"], 0)
        self.assertEqual(report["compared_jobs"], 9)

    def test_tampered_quota_raises_e_replay_with_first_divergence(self):
        with self.assertRaises(BusinessError) as context:
            Session.replay_snapshot(
                self.path,
                quota_overrides={"team-alpha": {"max_cards": 1}})
        self.assertEqual(context.exception.code, E_REPLAY)
        divergence = context.exception.details["first_divergence"]
        self.assertEqual(divergence["job_id"], "j-alpha-1")
        self.assertEqual(divergence["field"], "admitted")

    def test_scale_version_mismatch_raises_e_replay(self):
        with self.assertRaises(BusinessError) as context:
            Session.replay_snapshot(self.path, scale_overrides={"batch_size": 64})
        self.assertEqual(context.exception.code, E_REPLAY)
        details = context.exception.details
        self.assertEqual(details["recorded_parameter_version"], 1)
        self.assertEqual(details["replay_parameter_version"], 2)
        self.assertIn(details["first_divergence"]["field"],
                      ("per_card_mib", "total_mib", "parameter_version"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
