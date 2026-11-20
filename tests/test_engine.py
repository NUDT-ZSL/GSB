"""End-to-end and unit tests for the chart_reader engine."""

from __future__ import annotations

import json
import math
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chart_reader import codes
from chart_reader.config import load_config, validate_config
from chart_reader.errors import BusinessError
from chart_reader.generator import generate_samples
from chart_reader.injection import InjectionPlan
from chart_reader.pipeline import report_hash, run_pipeline, write_report
from chart_reader.snapshots import SnapshotStore
from chart_reader.sorting import judge_threshold, sort_series_points

SEED = 20261120
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "config.example.json"


class EngineTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="chart_reader_test_"))
        cls.samples = cls.tmp / "samples"
        generate_samples(cls.samples, SEED, 6)
        cls.cfg = load_config(CONFIG_PATH)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)


class TestDeterminism(EngineTestBase):
    def test_report_hash_stable_across_reruns(self):
        snap = self.tmp / "snap_det"
        r1 = run_pipeline(self.samples, self.cfg, snapshot_store=SnapshotStore(snap))
        r2 = run_pipeline(self.samples, self.cfg, snapshot_store=SnapshotStore(snap))
        self.assertEqual(r1["report_sha256"], r2["report_sha256"])
        out1, out2 = self.tmp / "r1.json", self.tmp / "r2.json"
        write_report(r1, out1)
        write_report(r2, out2)
        self.assertEqual(out1.read_bytes(), out2.read_bytes())
        self.assertEqual(report_hash(out1), r1["report_sha256"])

    def test_self_check_ok_on_baseline(self):
        report = run_pipeline(self.samples, self.cfg)
        self.assertEqual(report["self_check"]["status"], "ok")
        self.assertEqual(report["unattributed_anomalies"], [])


class TestStages(EngineTestBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.report = run_pipeline(cls.samples, cls.cfg)

    def test_tick_counts_match_truth(self):
        for chart in self.report["charts"]:
            tick = chart["stages"]["tick_localization"]
            self.assertTrue(tick["count_matches_truth"])
            self.assertEqual(tick["tick_pixels"], sorted(tick["tick_pixels"]))

    def test_inversion_close_to_truth(self):
        for chart in self.report["charts"]:
            ep = chart["stages"]["error_propagation"]
            self.assertLess(ep["levels"]["inversion"]["error_interval"][1], 1.0)

    def test_series_sorted_by_x(self):
        for chart in self.report["charts"]:
            for series in chart["stages"]["point_localization"]["series"]:
                xs = [p["pixel_x"] for p in series["points"]]
                self.assertEqual(xs, sorted(xs))

    def test_degenerate_calibration(self):
        from chart_reader.calibration import fit_calibration
        with self.assertRaises(BusinessError) as ctx:
            fit_calibration([10.0, 10.0, 10.0], [0.0, 1.0, 2.0],
                            0.0, 10.0, "chart_x", self.cfg)
        self.assertEqual(ctx.exception.code, codes.E_CALIB_DEGENERATE)
        self.assertEqual(ctx.exception.chart_id, "chart_x")

    def test_series_invalid_on_empty(self):
        from chart_reader.points import locate_points
        from PIL import Image
        truth = {
            "chart_id": "chart_empty",
            "chart_kind": "line",
            "canvas_shape": [100, 100],
            "series": [{"series_id": "s0", "color": [200, 30, 30]}],
        }
        img = Image.new("RGB", (100, 100), (255, 255, 255))
        with self.assertRaises(BusinessError) as ctx:
            locate_points(img, truth, self.cfg)
        self.assertEqual(ctx.exception.code, codes.E_SERIES_INVALID)
        self.assertEqual(ctx.exception.series_id, "s0")


class TestFaultInjection(EngineTestBase):
    def _run_with_faults(self, faults):
        plan = InjectionPlan(faults)
        return run_pipeline(self.samples, self.cfg, plan), plan

    def test_four_injection_points_attributed(self):
        faults = [
            {"id": "f1", "kind": "tick_pixel_out_of_bounds",
             "chart_id": "chart_0000", "params": {"tick_index": 0}},
            {"id": "f2", "kind": "axis_range_non_positive",
             "chart_id": "chart_0001", "params": {}},
            {"id": "f3", "kind": "point_outside_canvas",
             "chart_id": "chart_0002", "params": {"series_id": "s0"}},
            {"id": "f4", "kind": "duplicate_series_id",
             "chart_id": "chart_0003", "params": {"series_id": "s0"}},
        ]
        report, plan = self._run_with_faults(faults)
        self.assertEqual(len(report["injections"]), 4)
        expected = {
            "chart_0000": ("tick_localization", codes.E_TICK_LOC_INVALID),
            "chart_0001": ("calibration", codes.E_AXIS_RANGE_INVALID),
            "chart_0002": ("point_localization", codes.E_POINT_OUT_OF_CANVAS),
            "chart_0003": ("inversion", codes.E_DUPLICATE_SERIES),
        }
        for chart in report["charts"]:
            cid = chart["chart_id"]
            if cid not in expected:
                continue
            stage, code = expected[cid]
            self.assertEqual(chart["stages"][stage]["status"], "error")
            self.assertEqual(chart["stages"][stage]["code"], code)
            # downstream stages carry the upstream business code
            idx = list(chart["stages"]).index(stage)
            for later in list(chart["stages"])[idx + 1:]:
                self.assertEqual(chart["stages"][later]["status"], "degraded")
                self.assertEqual(chart["stages"][later]["triggered_by"], code)


class TestConfigValidation(unittest.TestCase):
    def test_invalid_threshold(self):
        with self.assertRaises(BusinessError) as ctx:
            validate_config({"min_credible_threshold": float("nan")})
        self.assertEqual(ctx.exception.code, codes.E_CONFIG_INVALID)
        self.assertEqual(ctx.exception.field, "min_credible_threshold")

    def test_invalid_tolerance(self):
        with self.assertRaises(BusinessError) as ctx:
            validate_config({"threshold_tolerance": -1.0})
        self.assertEqual(ctx.exception.code, codes.E_CONFIG_INVALID)
        self.assertEqual(ctx.exception.field, "threshold_tolerance")

    def test_invalid_strategy(self):
        with self.assertRaises(BusinessError) as ctx:
            validate_config({"threshold_strategy": "fuzzy"})
        self.assertEqual(ctx.exception.code, codes.E_CONFIG_INVALID)
        self.assertEqual(ctx.exception.field, "threshold_strategy")


class TestSortingStability(unittest.TestCase):
    def _cfg(self, **kw):
        base = {
            "min_credible_threshold": 1.0,
            "threshold_tolerance": 1e-6,
            "threshold_strategy": "strict",
            "ulps_tolerance": 4,
        }
        base.update(kw)
        return base

    def test_strict_vs_tolerance_band(self):
        value = 1.0 - 1e-7  # inside the tolerance band, below threshold
        strict = judge_threshold(value, self._cfg())
        band = judge_threshold(value, self._cfg(threshold_strategy="tolerance_band"))
        self.assertFalse(strict["credible"])
        self.assertTrue(band["credible"])

    def test_subnormal_protection(self):
        tiny = 5e-324  # smallest subnormal
        cfg = self._cfg(min_credible_threshold=0.0, threshold_tolerance=0.0,
                        threshold_strategy="tolerance_band")
        verdict = judge_threshold(tiny, cfg)
        self.assertTrue(verdict["credible"])
        self.assertGreaterEqual(verdict["effective_tolerance"], 4 * math.ulp(0.0))
        # repeated runs never flip
        again = judge_threshold(tiny, cfg)
        self.assertEqual(verdict["credible"], again["credible"])

    def test_deterministic_order_on_ties(self):
        pts = [
            {"series_id": "s0", "pixel_x": 3.0, "pixel_y": 1.0, "value": 2.0},
            {"series_id": "s0", "pixel_x": 1.0, "pixel_y": 1.0, "value": 2.0},
            {"series_id": "s0", "pixel_x": 2.0, "pixel_y": 1.0, "value": 1.0},
        ]
        cfg = self._cfg()
        ordered = sort_series_points(pts, cfg)
        self.assertEqual([p["pixel_x"] for p in ordered], [2.0, 1.0, 3.0])
        self.assertEqual([p["rank"] for p in ordered], [0, 1, 2])


class TestSnapshots(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="chart_reader_snap_"))
        self.store = SnapshotStore(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_whole_replacement_and_version_bump(self):
        r1 = self.store.save("config", {"a": 1})
        r2 = self.store.save("config", {"a": 2})
        self.assertEqual((r1["version"], r2["version"]), (1, 2))
        self.assertEqual(self.store.load("config")["payload"], {"a": 2})

    def test_rollback_rejected(self):
        self.store.save("config", {"a": 1})
        self.store.save("config", {"a": 2})
        with self.assertRaises(BusinessError) as ctx:
            self.store.save("config", {"a": 1}, version=1)
        self.assertEqual(ctx.exception.code, codes.E_SNAPSHOT_ROLLBACK)
        self.assertEqual(self.store.load("config")["payload"], {"a": 2})

    def test_corrupt_checksum(self):
        self.store.save("ground_truth", {"x": [1, 2, 3]})
        path = self.dir / "ground_truth.snapshot.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["payload"]["x"] = [9, 9, 9]  # tamper without fixing checksum
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(BusinessError) as ctx:
            self.store.load("ground_truth")
        self.assertEqual(ctx.exception.code, codes.E_SNAPSHOT_CORRUPT)
        self.assertEqual(ctx.exception.detail["category"], "ground_truth")
        self.assertEqual(ctx.exception.detail["version"], 1)


class TestSelfCheck(EngineTestBase):
    def test_inconsistent_recompute_detected(self):
        report = run_pipeline(self.samples, self.cfg)
        # tamper: break one inverted value, then re-run the self-check
        from chart_reader.pipeline import _self_check
        chart = report["charts"][0]
        chart["stages"]["inversion"]["series"][0]["points"][0]["value"] += 5.0
        violations = _self_check(report["charts"], self.cfg)
        self.assertTrue(any(v["code"] == codes.E_INCONSISTENT
                            and v["category"] == "inversion_recompute"
                            for v in violations))

    def test_untraceable_error_detected(self):
        report = run_pipeline(self.samples, self.cfg)
        from chart_reader.pipeline import _self_check
        chart = report["charts"][0]
        lvl = chart["stages"]["error_propagation"]["levels"]["inversion"]
        lvl["error_interval"] = [lvl["error_interval"][1],
                                 lvl["error_interval"][0]]  # invert order
        violations = _self_check(report["charts"], self.cfg)
        self.assertTrue(any(v["code"] == codes.E_INCONSISTENT
                            and v["category"] == "error_interval"
                            for v in violations))


if __name__ == "__main__":
    unittest.main()
