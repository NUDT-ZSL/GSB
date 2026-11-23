"""Requirement 1 & 4: byte-identical determinism, serial vs parallel."""

from __future__ import annotations

import tempfile
import unittest

from calib_workbench.pipeline import run_pipeline
from calib_workbench.scheduler import build_tasks, execute_plan, plan_combos
from calib_workbench.split import split_and_preprocess
from calib_workbench.util import canonical_json

from tests.helpers import small_cfg, small_rows


class TestSplitDeterminism(unittest.TestCase):
    def test_split_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = small_cfg(f"{tmp}/s", f"{tmp}/o")
            rows = small_rows(cfg)
            a = canonical_json(split_and_preprocess(rows, cfg))
            b = canonical_json(split_and_preprocess(rows, cfg))
            self.assertEqual(a, b)

    def test_manifest_generation_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = small_cfg(f"{tmp}/s", f"{tmp}/o")
            self.assertEqual(canonical_json(small_rows(cfg)), canonical_json(small_rows(cfg)))


class TestParallelSerialEquivalence(unittest.TestCase):
    def test_parallel_matches_serial_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = small_cfg(f"{tmp}/s", f"{tmp}/o")
            rows = small_rows(cfg)
            split = split_and_preprocess(rows, cfg)
            combos = plan_combos(cfg, sorted(split["groups"]))
            tasks = build_tasks(combos, split, cfg)
            serial = execute_plan(tasks, workers=1, batch_size=2)
            parallel = execute_plan(tasks, workers=2, batch_size=2)
            self.assertEqual(canonical_json(serial), canonical_json(parallel))
            self.assertTrue(all(r["ok"] for r in parallel))

    def test_pipeline_run_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg1 = small_cfg(f"{tmp}/s1", f"{tmp}/o1")
            cfg2 = small_cfg(f"{tmp}/s2", f"{tmp}/o2")
            rows = small_rows(cfg1)
            r1 = run_pipeline(cfg1, rows=rows)
            r2 = run_pipeline(cfg2, rows=rows)
            self.assertEqual(canonical_json(r1["per_combo"]), canonical_json(r2["per_combo"]))
            self.assertEqual(canonical_json(r1["overall"]), canonical_json(r2["overall"]))
            self.assertEqual(canonical_json(r1["lineage"]), canonical_json(r2["lineage"]))


if __name__ == "__main__":
    unittest.main()
