"""Requirement 7: targeted invalidation propagation and cache reuse."""

from __future__ import annotations

import tempfile
import unittest

from calib_workbench import invalidation
from calib_workbench.pipeline import run_pipeline

from tests.helpers import small_cfg, small_rows


class TestDiffClassification(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = small_cfg(f"{self.tmp.name}/s", f"{self.tmp.name}/o")

    def tearDown(self):
        self.tmp.cleanup()

    def _bump(self, **changes):
        import copy
        new = copy.deepcopy(self.cfg)
        new["config_version"] = self.cfg["config_version"] + 1
        for path, value in changes.items():
            node = new
            keys = path.split(".")
            for key in keys[:-1]:
                node = node[key]
            node[keys[-1]] = value
        return new

    def test_bins_only(self):
        new = self._bump(**{"calibrators.histogram.n_bins": 12})
        diff = invalidation.diff_configs(self.cfg, new)
        self.assertEqual(diff["kind"], invalidation.CHANGE_BINS_ONLY)
        self.assertIn("fit:histogram", diff["affected_nodes"])
        self.assertNotIn("split", diff["affected_nodes"])

    def test_split_ratio_only(self):
        new = self._bump(**{"split.ref_ratio": 0.5})
        diff = invalidation.diff_configs(self.cfg, new)
        self.assertEqual(diff["kind"], invalidation.CHANGE_SPLIT_RATIO_ONLY)
        self.assertIn("split", diff["affected_nodes"])

    def test_calibrator_set(self):
        new = self._bump(**{"calibrators.platt.max_iter": 30})
        diff = invalidation.diff_configs(self.cfg, new)
        self.assertEqual(diff["kind"], invalidation.CHANGE_CALIBRATOR_SET)
        self.assertIn("fit:platt", diff["affected_nodes"])
        self.assertNotIn("fit:isotonic", diff["affected_nodes"])

    def test_mixed(self):
        new = self._bump(**{"split.ref_ratio": 0.5, "calibrators.histogram.n_bins": 12})
        diff = invalidation.diff_configs(self.cfg, new)
        self.assertEqual(diff["kind"], invalidation.CHANGE_MIXED)


class TestPipelineReuse(unittest.TestCase):
    def test_bins_change_recomputes_only_histogram_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = small_cfg(f"{tmp}/s", f"{tmp}/o")
            rows = small_rows(cfg)
            run_pipeline(cfg, rows=rows)
            cfg2 = small_cfg(f"{tmp}/s", f"{tmp}/o")
            cfg2["config_version"] = 2
            cfg2["calibrators"]["histogram"]["n_bins"] = 12
            report = run_pipeline(cfg2, rows=rows)
            self.assertEqual(report["change"]["kind"], invalidation.CHANGE_BINS_ONLY)
            status = {n["node"]: n["status"] for n in report["node_log"]}
            self.assertEqual(status["split"], "reused")
            self.assertEqual(status["fit:platt:g1"], "reused")
            self.assertEqual(status["fit:isotonic:g1"], "reused")
            self.assertEqual(status["fit:histogram:g1"], "computed")
            self.assertEqual(status["metrics:__overall__:histogram"], "computed")
            self.assertEqual(status["metrics:__overall__:platt"], "reused")

    def test_ratio_change_recomputes_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = small_cfg(f"{tmp}/s", f"{tmp}/o")
            rows = small_rows(cfg)
            run_pipeline(cfg, rows=rows)
            cfg2 = small_cfg(f"{tmp}/s", f"{tmp}/o")
            cfg2["config_version"] = 2
            cfg2["split"]["ref_ratio"] = 0.5
            report = run_pipeline(cfg2, rows=rows)
            self.assertEqual(report["change"]["kind"], invalidation.CHANGE_SPLIT_RATIO_ONLY)
            status = {n["node"]: n["status"] for n in report["node_log"]}
            self.assertEqual(status["split"], "computed")
            self.assertEqual(status["fit:platt:g1"], "computed")

    def test_identical_config_reuses_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = small_cfg(f"{tmp}/s", f"{tmp}/o")
            rows = small_rows(cfg)
            run_pipeline(cfg, rows=rows)
            report = run_pipeline(cfg, rows=rows)
            self.assertEqual(report["change"]["kind"], invalidation.CHANGE_NONE)
            computed = [n for n in report["node_log"] if n["status"] == "computed"]
            self.assertEqual(computed, [])


if __name__ == "__main__":
    unittest.main()
