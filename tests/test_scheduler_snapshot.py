import tempfile
import unittest
from pathlib import Path

from panseg.errors import E_FAULT_INJECTED
from panseg.models import FailureRecord, PipelineConfig
from panseg.scheduler import Task, run_tasks, stable_sort_key
from panseg.snapshot import SnapshotStore, atomic_write_json
from panseg.fusion import FusionResult


class SchedulerTests(unittest.TestCase):
    def test_deterministic_order_with_multiple_workers(self):
        ids = [f"inst-{index:02d}" for index in range(20)]

        def build(workers):
            tasks = [
                Task(instance_id=identifier, stage="fuse", func=lambda i=identifier: i)
                for identifier in reversed(ids)
            ]
            result = run_tasks(tasks, workers)
            return [item[0] for item in result.successes]

        expected = sorted(ids)
        self.assertEqual(build(1), build(8))
        self.assertEqual(build(1), expected)

    def test_exception_isolation_keeps_processing(self):
        def boom():
            raise ValueError("kaboom")

        tasks = [
            Task("inst-0", "parse_v1", boom),
            Task("inst-1", "parse_v2", lambda: 42),
            Task("inst-2", "fuse", boom),
        ]
        result = run_tasks(tasks, 4)
        self.assertEqual([item[0] for item in result.successes], ["inst-1"])
        self.assertEqual([item.instance_id for item in result.failures], ["inst-0", "inst-2"])
        self.assertTrue(all(isinstance(item, FailureRecord) for item in result.failures))

    def test_stable_sort_key_format(self):
        self.assertEqual(stable_sort_key("a", "fuse"), "a::fuse")


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SnapshotStore(Path(self.tmp.name) / "snapshots")

    def tearDown(self):
        self.tmp.cleanup()

    def test_commit_and_recover_empty_snapshot(self):
        manifest = {
            "global_version": "panseg-000001",
            "version_number": 1,
            "config_hash": "h1",
            "instance_versions": {},
            "fusion_versions": {},
        }
        self.store.commit(1, manifest, {}, {})
        recovered = self.store.recover()
        self.assertIsNotNone(recovered)
        recovered_manifest, instances, fusion = recovered
        self.assertEqual(recovered_manifest["config_hash"], "h1")
        self.assertEqual(instances, {})
        self.assertEqual(fusion, {})
        self.assertEqual(self.store.current_version(), 1)

    def test_second_commit_prunes_first(self):
        for version in (1, 2):
            self.store.commit(
                version,
                {
                    "global_version": f"panseg-{version:06d}",
                    "version_number": version,
                    "config_hash": "h",
                    "instance_versions": {},
                    "fusion_versions": {},
                },
                {},
                {},
            )
        self.assertEqual(self.store.current_version(), 2)
        self.assertFalse(self.store.snapshot_dir(1).exists())
        self.assertTrue(self.store.snapshot_dir(2).exists())

    def test_atomic_json_is_readable(self):
        target = Path(self.tmp.name) / "nested" / "report.json"
        atomic_write_json(target, {"ok": True})
        import json

        self.assertTrue(json.loads(target.read_text())["ok"])


if __name__ == "__main__":
    unittest.main()
