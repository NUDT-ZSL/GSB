"""Requirements 7 & 8: self-check consistency and lineage replay."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from calib_workbench.errors import E_INCONSISTENT, E_LINEAGE_BROKEN, WorkbenchError
from calib_workbench.pipeline import run_pipeline
from calib_workbench.selfcheck import replay_lineage, run_selfcheck

from tests.helpers import cache_file, small_cfg, small_rows


class TestSelfCheck(unittest.TestCase):
    def test_selfcheck_passes_on_clean_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = small_cfg(f"{tmp}/s", f"{tmp}/o")
            rows = small_rows(cfg)
            report = run_pipeline(cfg, rows=rows)
            result = run_selfcheck(cfg, rows, report)
            self.assertTrue(result["ok"])
            self.assertEqual(len(result["checks"]), 3)

    def test_tampered_snapshot_body_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = small_cfg(f"{tmp}/s", f"{tmp}/o")
            rows = small_rows(cfg)
            report = run_pipeline(cfg, rows=rows)
            snap = os.path.join(cfg["state_dir"], "snapshots", "metrics", "v1.json")
            with open(snap, "r", encoding="utf-8") as fh:
                record = json.load(fh)
            record["body"]["overall"]["platt"]["ece"] = 0.123456
            with open(snap, "w", encoding="utf-8") as fh:
                json.dump(record, fh)
            with self.assertRaises(WorkbenchError) as ctx:
                run_selfcheck(cfg, rows, report)
            self.assertEqual(ctx.exception.code, E_INCONSISTENT)

    def test_tampered_detail_fails_metrics_recompute(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = small_cfg(f"{tmp}/s", f"{tmp}/o")
            rows = small_rows(cfg)
            report = run_pipeline(cfg, rows=rows)
            report["per_combo"]["platt:g1"]["metrics"]["ece"] += 0.5
            with self.assertRaises(WorkbenchError) as ctx:
                run_selfcheck(cfg, rows, report)
            self.assertEqual(ctx.exception.code, E_INCONSISTENT)
            self.assertEqual(ctx.exception.details["check"], "metrics_recompute")


class TestLineageReplay(unittest.TestCase):
    def test_replay_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = small_cfg(f"{tmp}/s", f"{tmp}/o")
            rows = small_rows(cfg)
            report = run_pipeline(cfg, rows=rows)
            result = replay_lineage(cfg, report)
            self.assertTrue(result["ok"])
            self.assertEqual(len(result["replayed"]), len(report["lineage"]))

    def test_missing_cache_artifact_breaks_lineage(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = small_cfg(f"{tmp}/s", f"{tmp}/o")
            rows = small_rows(cfg)
            report = run_pipeline(cfg, rows=rows)
            rec = next(r for r in report["lineage"] if r["group"] != "__overall__")
            os.unlink(cache_file(cfg, rec["cache_keys"]["apply"]))
            with self.assertRaises(WorkbenchError) as ctx:
                replay_lineage(cfg, report)
            self.assertEqual(ctx.exception.code, E_LINEAGE_BROKEN)

    def test_tampered_cached_detail_breaks_lineage(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = small_cfg(f"{tmp}/s", f"{tmp}/o")
            rows = small_rows(cfg)
            report = run_pipeline(cfg, rows=rows)
            rec = next(r for r in report["lineage"] if r["group"] != "__overall__")
            path = cache_file(cfg, rec["cache_keys"]["apply"])
            with open(path, "r", encoding="utf-8") as fh:
                node = json.load(fh)
            node["value"]["detail"]["probs"][0] = 0.5
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(node, fh)
            with self.assertRaises(WorkbenchError) as ctx:
                replay_lineage(cfg, report)
            self.assertEqual(ctx.exception.code, E_LINEAGE_BROKEN)


if __name__ == "__main__":
    unittest.main()
