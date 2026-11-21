from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from bmm_pipeline.config import PipelineConfig
from bmm_pipeline.errors import PipelineError
from bmm_pipeline.snapshots import SnapshotStore


class SnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SnapshotStore(Path(self.temp.name))
        self.config_fingerprint = PipelineConfig().fingerprint()
        self.jobset_fingerprint = "sha256:test-jobs"
        self.expected = ["a", "b"]

    def commit(
        self,
        stage: str,
        *,
        version: int | None = None,
        completed: list[str] | None = None,
    ):
        return self.store.commit_stage(
            stage,
            {"stage": stage, "value": stage},
            completed if completed is not None else self.expected,
            expected_jobs=self.expected,
            config_fingerprint=self.config_fingerprint,
            jobset_fingerprint=self.jobset_fingerprint,
            version=version,
        )

    def test_partial_stage_cannot_commit(self) -> None:
        with self.assertRaises(PipelineError) as context:
            self.commit("validation", completed=["a"])
        self.assertEqual(context.exception.code, "E_STAGE_INCOMPLETE")
        self.assertEqual(self.store.load_committed(), [])

    def test_atomic_commit_exposes_stage_version_and_checksum(self) -> None:
        snapshot = self.commit("validation")
        self.assertEqual(snapshot.version, 1)
        snapshots = self.store.load_committed()
        self.assertEqual(len(snapshots), 1)
        current = json.loads(self.store.current_path.read_text(encoding="utf-8"))
        self.assertEqual(current["stage"], "validation")
        self.assertEqual(current["version"], 1)
        self.assertEqual(current["content_checksum"], snapshot.content_checksum)
        self.assertFalse(list(self.store.snapshot_dir.glob("*.tmp")))

    def test_old_version_cannot_overwrite_newer_snapshot(self) -> None:
        self.commit("validation")
        planning = self.commit("planning")
        original = planning.path.read_bytes()
        with self.assertRaises(PipelineError) as context:
            self.commit("planning", version=1)
        self.assertEqual(context.exception.code, "E_SNAPSHOT_ROLLBACK")
        self.assertEqual(planning.path.read_bytes(), original)
        self.assertEqual(self.store.load_committed()[-1].version, 2)

    def test_corrupt_checksum_reports_stage_and_version(self) -> None:
        snapshot = self.commit("validation")
        document = json.loads(snapshot.path.read_text(encoding="utf-8"))
        document["job_count"] = 99
        snapshot.path.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(PipelineError) as context:
            self.store.load_committed()
        exc = context.exception
        self.assertEqual(exc.code, "E_SNAPSHOT_CORRUPT")
        self.assertEqual(exc.stage, "validation")
        self.assertEqual(exc.version, 1)

    def test_truncated_snapshot_is_rejected(self) -> None:
        snapshot = self.commit("validation")
        snapshot.path.write_text('{"stage":"validation"', encoding="utf-8")
        with self.assertRaises(PipelineError) as context:
            self.store.load_committed()
        self.assertEqual(context.exception.code, "E_SNAPSHOT_CORRUPT")
        self.assertEqual(context.exception.stage, "validation")
        self.assertEqual(context.exception.version, 1)

    def test_current_checksum_change_is_detected(self) -> None:
        self.commit("validation")
        pointer = json.loads(self.store.current_path.read_text(encoding="utf-8"))
        pointer["content_checksum"] = "sha256:wrong"
        self.store.current_path.write_text(
            json.dumps(pointer, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(PipelineError) as context:
            self.store.load_committed()
        self.assertEqual(context.exception.code, "E_SNAPSHOT_CORRUPT")
        self.assertEqual(context.exception.stage, "validation")
        self.assertEqual(context.exception.version, 1)

    def test_concurrent_commit_fails_immediately(self) -> None:
        self.store.ensure_layout()
        self.store.lock_path.write_text("held", encoding="utf-8")
        started = time.monotonic()
        with self.assertRaises(PipelineError) as context:
            self.commit("validation")
        elapsed = time.monotonic() - started
        self.assertEqual(context.exception.code, "E_STAGE_BUSY")
        self.assertLess(elapsed, 1.0)


if __name__ == "__main__":
    unittest.main()
