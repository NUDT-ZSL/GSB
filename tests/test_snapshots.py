from __future__ import annotations

import copy
import json
import time

from motion_pipeline.dataset import load_dataset
from motion_pipeline.errors import BusinessError
from motion_pipeline.snapshots import SnapshotStore, resign_snapshot
from motion_pipeline.stages import sample_frames

from tests.helpers import DatasetTestCase


class SnapshotTests(DatasetTestCase):
    def test_partial_stage_payload_is_rejected(self) -> None:
        loaded = load_dataset(self.manifest, self.frames)
        payload = sample_frames(loaded, loaded_config())
        removed_clip = payload["clip_ids"][-1]
        payload["clip_ids"] = payload["clip_ids"][:-1]
        payload["clips"].pop(removed_clip)
        store = SnapshotStore(self.root / "partial")
        with self.assertRaises(BusinessError) as caught:
            store.commit_stage(
                0,
                payload,
                required_clip_ids=loaded["clip_ids"],
            )
        self.assertEqual(caught.exception.code, "E_PARTIAL_COMMIT")

    def test_stage_gap_is_rejected(self) -> None:
        store = SnapshotStore(self.root / "gap")
        loaded = load_dataset(self.manifest, self.frames)
        with self.assertRaises(BusinessError) as caught:
            store.commit_stage(
                1,
                {"clip_ids": loaded["clip_ids"], "clips": {}},
                required_clip_ids=loaded["clip_ids"],
            )
        self.assertEqual(caught.exception.code, "E_PARTIAL_COMMIT")

    def test_stage_busy_is_nonblocking(self) -> None:
        store = SnapshotStore(self.root / "busy")
        payload = {"clip_ids": ["clip-0001"], "clips": {}}
        with store._commit_lock("frame_sample"):
            started = time.monotonic()
            with self.assertRaises(BusinessError) as caught:
                store.commit_stage(
                    0,
                    payload,
                    required_clip_ids=["clip-0001"],
                )
            elapsed = time.monotonic() - started
        self.assertEqual(caught.exception.code, "E_STAGE_BUSY")
        self.assertLess(elapsed, 0.5)

    def test_stale_unversioned_second_submit_is_busy(self) -> None:
        loaded = load_dataset(self.manifest, self.frames)
        payload = sample_frames(loaded, loaded_config())
        store = SnapshotStore(self.root / "stale-submit")
        store.commit_stage(
            0, payload, required_clip_ids=loaded["clip_ids"]
        )
        with self.assertRaises(BusinessError) as caught:
            store.commit_stage(
                0, payload, required_clip_ids=loaded["clip_ids"]
            )
        self.assertEqual(caught.exception.code, "E_STAGE_BUSY")

    def test_corrupt_stage_payload_reports_stage_and_version(self) -> None:
        baseline = self.run_pipeline(self.root / "baseline")
        store = SnapshotStore(self.root / "baseline")
        raw_snapshot = json.loads(store.snapshot_path.read_text())
        raw_snapshot["stages"][0]["payload"]["tampered"] = True
        store.snapshot_path.write_text(json.dumps(raw_snapshot))
        with self.assertRaises(BusinessError) as caught:
            store.load()
        error = caught.exception
        self.assertEqual(error.code, "E_SNAPSHOT_CORRUPT")
        self.assertEqual(error.stage, "frame_sample")
        self.assertEqual(error.version, 1)

    def test_truncated_snapshot_is_not_partially_used(self) -> None:
        baseline = self.run_pipeline(self.root / "baseline")
        store = SnapshotStore(self.root / "baseline")
        store.snapshot_path.write_bytes(b"{\"partial\":")
        with self.assertRaises(BusinessError) as caught:
            store.load()
        self.assertEqual(caught.exception.code, "E_SNAPSHOT_CORRUPT")
        self.assertEqual(caught.exception.stage, "result_aggregation")
        self.assertEqual(caught.exception.version, 4)
        self.assertIsNotNone(baseline.final_result_bytes)

    def test_older_snapshot_is_rejected_after_newer_commit(self) -> None:
        workdir = self.root / "rollback"

        def crash_hook(stage_index, _checkpoint):
            if stage_index == 1:
                raise RuntimeError("stop")

        with self.assertRaises(RuntimeError):
            self.run_pipeline(workdir, on_stage_committed=crash_hook)
        store = SnapshotStore(workdir)
        old_bytes = store.snapshot_path.read_bytes()
        self.assertEqual(store.load()["version"], 2)
        recovered = self.run_pipeline(workdir)
        self.assertEqual(recovered.checkpoint["version"], 4)
        store.snapshot_path.write_bytes(old_bytes)
        with self.assertRaises(BusinessError) as caught:
            store.load()
        self.assertEqual(caught.exception.code, "E_SNAPSHOT_ROLLBACK")

    def test_same_version_whole_snapshot_replacement(self) -> None:
        baseline = self.run_pipeline(self.root / "baseline")
        store = SnapshotStore(self.root / "baseline")
        current = store.load()
        old_checksum = current["snapshot_checksum"]
        candidate = copy.deepcopy(current)
        candidate["replacement_probe"] = "whole-file"
        resign_snapshot(candidate)
        store.replace_snapshot(candidate, expected_version=4)
        replaced = store.load()
        self.assertEqual(replaced["version"], 4)
        self.assertNotEqual(replaced["snapshot_checksum"], old_checksum)
        self.assertEqual(
            [record["content_checksum"] for record in replaced["stages"]],
            [record["content_checksum"] for record in baseline.checkpoint["stages"]],
        )
        ledger = json.loads(store.ledger_path.read_text())
        self.assertEqual(ledger["version"], 4)
        self.assertEqual(ledger["snapshot_checksum"], replaced["snapshot_checksum"])

    def test_older_candidate_cannot_replace_newer_snapshot(self) -> None:
        workdir = self.root / "candidate"

        def crash_hook(stage_index, _checkpoint):
            if stage_index == 1:
                raise RuntimeError("stop")

        with self.assertRaises(RuntimeError):
            self.run_pipeline(workdir, on_stage_committed=crash_hook)
        store = SnapshotStore(workdir)
        old_candidate = store.load()
        self.run_pipeline(workdir)
        with self.assertRaises(BusinessError) as caught:
            store.replace_snapshot(old_candidate)
        self.assertEqual(caught.exception.code, "E_SNAPSHOT_ROLLBACK")


def loaded_config():
    from motion_pipeline.config import PipelineConfig

    return PipelineConfig()
