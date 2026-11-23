"""Requirement 4: worker failure attribution, no silent drops."""

from __future__ import annotations

import tempfile
import unittest

from calib_workbench.errors import E_WORKER_FAILURE
from calib_workbench.scheduler import build_tasks, execute_plan, plan_combos
from calib_workbench.split import split_and_preprocess

from tests.helpers import small_cfg, small_rows


class TestWorkerErrors(unittest.TestCase):
    def test_worker_failure_is_structured_and_attributed(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = small_cfg(f"{tmp}/s", f"{tmp}/o")
            rows = small_rows(cfg)
            split = split_and_preprocess(rows, cfg)
            combos = plan_combos(cfg, sorted(split["groups"]))
            tasks = build_tasks(combos, split, cfg)
            # corrupt all histogram tasks: missing n_bins -> KeyError
            for t in tasks:
                if t["calibrator"] == "histogram":
                    t["cal_params"] = {}
            results = execute_plan(tasks, workers=1, batch_size=2)
            self.assertEqual(len(results), len(tasks))  # nothing dropped
            failed = [r for r in results if not r["ok"]]
            self.assertEqual(len(failed), 3)  # histogram x 3 groups
            for res in failed:
                self.assertEqual(res["calibrator"], "histogram")
                self.assertEqual(res["error"]["code"], E_WORKER_FAILURE)
                self.assertIn("stage", res["error"])
                self.assertIn(res["group"], ("g1", "g2", "g3"))
            ok = [r for r in results if r["ok"]]
            self.assertEqual(len(ok), len(tasks) - 3)

    def test_worker_failure_in_process_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = small_cfg(f"{tmp}/s", f"{tmp}/o")
            rows = small_rows(cfg)
            split = split_and_preprocess(rows, cfg)
            combos = plan_combos(cfg, sorted(split["groups"]))
            tasks = build_tasks(combos, split, cfg)
            for t in tasks:
                if t["calibrator"] == "histogram":
                    t["cal_params"] = {}
            results = execute_plan(tasks, workers=2, batch_size=2)
            self.assertEqual(len(results), len(tasks))
            failed = [r for r in results if not r["ok"]]
            self.assertEqual(len(failed), 3)
            self.assertTrue(all(r["error"]["code"] == E_WORKER_FAILURE for r in failed))


if __name__ == "__main__":
    unittest.main()
