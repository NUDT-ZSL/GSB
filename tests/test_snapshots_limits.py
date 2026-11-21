import json
import tempfile
import threading
import unittest
from pathlib import Path

from diagwb.errors import DiagError
from diagwb.limits import ResourceLimits
from diagwb.samples import generate_workspace
from diagwb.snapshots import (ANSWERS, FORMAT_DEFS, REPOSITORY,
                              SnapshotStore)
from diagwb.workbench import DiagnosticWorkbench


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SnapshotStore(Path(self.tmp.name) / "snap")

    def tearDown(self):
        self.tmp.cleanup()

    def test_monotonic_versions_and_whole_replacement(self):
        with self.store.commit_stage():
            first = self.store.commit(REPOSITORY, {"a": 1})
            second = self.store.commit(REPOSITORY, {"a": 2})
        self.assertEqual(first.version, 1)
        self.assertEqual(second.version, 2)
        loaded = self.store.load(REPOSITORY)
        self.assertEqual(loaded.payload, {"a": 2})
        self.assertEqual(self.store.latest_version(FORMAT_DEFS), 0)

    def test_rollback_rejected(self):
        with self.store.commit_stage():
            self.store.commit(ANSWERS, {"x": 1})
        with self.assertRaises(DiagError) as caught:
            self.store.try_rollback(ANSWERS, 1)
        self.assertEqual(caught.exception.code, "E_SNAPSHOT_ROLLBACK")

    def test_corrupt_checksum_detected(self):
        with self.store.commit_stage():
            self.store.commit(REPOSITORY, {"a": 1})
        path = self.store._path(REPOSITORY, 1)
        data = json.loads(path.read_text())
        data["checksum"] = "0" * 64
        path.write_text(json.dumps(data))
        with self.assertRaises(DiagError) as caught:
            self.store.load(REPOSITORY, 1)
        self.assertEqual(caught.exception.code, "E_SNAPSHOT_CORRUPT")
        self.assertEqual(caught.exception.context["component"], REPOSITORY)
        self.assertEqual(caught.exception.context["version"], 1)

    def test_concurrent_commit_stage_busy(self):
        outcomes = []

        def worker():
            try:
                with self.store.commit_stage():
                    self.store.commit(FORMAT_DEFS, {"v": 1})
                outcomes.append("committed")
            except DiagError as exc:
                outcomes.append(exc.code)

        self.store.stage_lock.acquire()
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=2)
        self.store.stage_lock.release()
        self.assertIn("E_STAGE_BUSY", outcomes)


class ResourceLimitTests(unittest.TestCase):
    def test_result_and_shard_limits(self):
        limits = ResourceLimits(max_resident_results=2,
                                max_shards_per_result=1,
                                max_cache_entries=3)
        limits.enforce_results(2)
        with self.assertRaises(DiagError) as caught:
            limits.enforce_results(3)
        self.assertEqual(caught.exception.code, "E_RESOURCE_LIMIT")
        with self.assertRaises(DiagError) as caught:
            limits.enforce_shards("r", 2)
        self.assertEqual(caught.exception.context["category"],
                         "shards_per_result")


class SelfCheckTests(unittest.TestCase):
    def test_self_check_passes_on_generated_workspace(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        info = generate_workspace(base / "data")
        wb = DiagnosticWorkbench(base / "runtime")
        manifests = wb.load_manifest(info["manifest"])
        for entry in manifests:
            for call in entry.tool_calls:
                wb.load_result(call.result_ref,
                               wb.resolve_result_file(
                                   info["results"], call.tool_id,
                                   call.result_format_version))
        assembly = wb.assemble_request(manifests[0])
        report = wb.self_check(manifests[0], assembly)
        self.assertTrue(report.ok, report.failures)

    def test_graceful_degradation_logs_category(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        info = generate_workspace(base / "data")
        wb = DiagnosticWorkbench(
            base / "runtime",
            limits=ResourceLimits(max_resident_results=3))
        manifests = wb.load_manifest(info["manifest"])
        for entry in manifests:
            for call in entry.tool_calls:
                wb.load_result(call.result_ref,
                               wb.resolve_result_file(
                                   info["results"], call.tool_id,
                                   call.result_format_version))
        outcome = wb.enforce_or_degrade_results()
        self.assertTrue(outcome["degraded"])
        self.assertEqual(len(wb.store.all_refs()), 3)
        self.assertIn("resident_results", wb.degradations.categories())


if __name__ == "__main__":
    unittest.main()
