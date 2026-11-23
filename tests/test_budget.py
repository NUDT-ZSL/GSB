"""Requirement 5: dual budget admission with explicit deficits."""

from __future__ import annotations

import tempfile
import unittest

from calib_workbench.budget import admit, estimate_combo
from calib_workbench.errors import E_ITER_BUDGET, E_MEMORY_BUDGET
from calib_workbench.pipeline import run_pipeline

from tests.helpers import small_cfg, small_rows


def _combos(cfg):
    out = []
    for cal in sorted(cfg["calibrators"]):
        for group in sorted(cfg["data"]["groups"]):
            out.append({"calibrator": cal, "group": group, "n_ref": 24, "n_eval": 16})
    return out


class TestAdmission(unittest.TestCase):
    def test_iter_budget_rejections_carry_deficit(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = small_cfg(f"{tmp}/s", f"{tmp}/o")
            combos = _combos(cfg)
            full = admit(combos, cfg)
            total_needed = full["totals"]["est_iters"]
            cfg["budget"]["max_total_iters"] = total_needed - 1
            result = admit(combos, cfg)
            self.assertTrue(result["rejected"])
            for entry in result["rejected"]:
                self.assertEqual(entry["code"], E_ITER_BUDGET)
                self.assertGreater(entry["deficit"]["iters"], 0)
            admitted_iters = sum(e["est_iters"] for e in result["admitted"])
            self.assertLessEqual(admitted_iters, cfg["budget"]["max_total_iters"])

    def test_memory_budget_rejections(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = small_cfg(f"{tmp}/s", f"{tmp}/o")
            cfg["budget"]["max_resident_bytes"] = 1  # nothing fits
            result = admit(_combos(cfg), cfg)
            self.assertEqual(len(result["admitted"]), 0)
            self.assertTrue(all(e["code"] == E_MEMORY_BUDGET for e in result["rejected"]))
            self.assertTrue(all(e["deficit"]["bytes"] > 0 for e in result["rejected"]))

    def test_estimates_are_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = small_cfg(f"{tmp}/s", f"{tmp}/o")
            a = estimate_combo("platt", 100, 50, cfg)
            b = estimate_combo("platt", 100, 50, cfg)
            self.assertEqual(a, b)

    def test_pipeline_budget_ledger_surfaces_rejections(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = small_cfg(f"{tmp}/s", f"{tmp}/o")
            rows = small_rows(cfg)
            run_pipeline(cfg, rows=rows)  # prime the cache
            cfg2 = small_cfg(f"{tmp}/s2", f"{tmp}/o2")
            cfg2["config_version"] = 2
            cfg2["calibrators"]["histogram"]["n_bins"] = 12
            cfg2["budget"]["max_total_iters"] = 1  # force rejections
            report = run_pipeline(cfg2, rows=rows)
            rejected = [l for l in report["plan"]["ledger"] if l["decision"] == "rejected"]
            self.assertTrue(rejected)
            self.assertTrue(all(l["code"] == E_ITER_BUDGET for l in rejected))
            self.assertTrue(all(l["deficit"]["iters"] > 0 for l in rejected))


if __name__ == "__main__":
    unittest.main()
