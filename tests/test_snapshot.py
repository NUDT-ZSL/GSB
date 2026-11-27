from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from flight_compile_cache.cache import CompileCacheEngine
from flight_compile_cache.errors import BusinessRuleError, E_SNAPSHOT_INTEGRITY
from flight_compile_cache.generator import generate_project
from flight_compile_cache.verification import (
    build_timeline,
    choose_cutoff,
    compare_restart_suffix,
    process_timeline,
    run_project,
)


class SnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.project = generate_project()

    def test_restart_suffix_hit_decisions_match(self) -> None:
        timeline = build_timeline(self.project)
        cutoff = choose_cutoff(timeline, None)
        engine = CompileCacheEngine(self.project)
        process_timeline(engine, timeline, start=0, end=cutoff)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.json"
            engine.write_snapshot(path)
            expected_suffix = process_timeline(engine, timeline, start=cutoff)
            self.assertFalse(list(Path(directory).glob("*.tmp")) + list(Path(directory).glob(".*.tmp")))
            restored = CompileCacheEngine.restore(path, self.project)
            actual_suffix = process_timeline(restored, timeline, start=cutoff)
            comparison = compare_restart_suffix(expected_suffix, actual_suffix)
            self.assertTrue(comparison["hit_decisions_identical"])
            self.assertTrue(comparison["compile_keys_identical"])
            self.assertTrue(comparison["artifact_ids_identical"])
            self.assertTrue(comparison["eviction_decisions_identical"])
            self.assertEqual(restored.restore_report["full_compilations_during_restore"], 0)

    def test_snapshot_integrity_is_checked(self) -> None:
        engine = CompileCacheEngine(self.project)
        timeline = build_timeline(self.project)
        process_timeline(engine, timeline, start=0, end=5)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.json"
            payload = engine.write_snapshot(path)
            payload["seq"] += 1
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            with self.assertRaises(BusinessRuleError) as caught:
                CompileCacheEngine.restore(path, self.project)
            self.assertEqual(caught.exception.error.code, E_SNAPSHOT_INTEGRITY)

    def test_version_change_invalidates_matching_cached_keys(self) -> None:
        timeline = build_timeline(self.project)
        cutoff = choose_cutoff(timeline, None)
        engine = CompileCacheEngine(self.project)
        process_timeline(engine, timeline, start=0, end=cutoff)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.json"
            engine.write_snapshot(path)
            changed_project = copy.deepcopy(self.project)
            for node in changed_project["models"]["delay_predict"]["versions"]["v2"]["graph"]["nodes"]:
                if node["id"] == "delay_mean":
                    node["attrs"]["factor"] = 0.99
            restored = CompileCacheEngine.restore(path, changed_project)
            self.assertTrue(restored.restore_report["version_change_detected"])
            self.assertTrue(restored.restore_report["invalidated_keys"])
            self.assertIn(
                "graph_fingerprint_changed",
                {item["reason"] for item in restored.restore_report["version_changes"]},
            )

    def test_run_project_end_to_end_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = run_project(self.project, out_dir=directory)
            self.assertTrue(report["overall_ok"], report["acceptance_checks"])
            self.assertTrue(report["restart_verification"]["records_identical"])
            self.assertTrue((Path(directory) / "report.json").exists())
            self.assertTrue((Path(directory) / "cache_checkpoint.json").exists())
            self.assertTrue((Path(directory) / "cache_final.json").exists())
            self.assertTrue((Path(directory) / "latest_snapshot.json").exists())
            self.assertEqual(
                report["full_compile_baseline"]["numeric_mismatches"], []
            )


if __name__ == "__main__":
    unittest.main()
