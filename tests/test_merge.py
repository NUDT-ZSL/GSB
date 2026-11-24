from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from audio_pipeline.config import load_task
from audio_pipeline.errors import PipelineError
from audio_pipeline.snapshots import merge_snapshots

from tests.helpers import make_workspace, read_json, write_json


class MergeTests(unittest.TestCase):
    def test_two_generation_merge_origin_conflicts_and_tombstone(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = make_workspace(Path(temporary))
            task = load_task(workspace / "task.json", workspace)
            view = merge_snapshots(task)

            window = view.get("program/promo", "window")
            self.assertIsNotNone(window)
            assert window is not None
            self.assertEqual(window.generation, 2)
            self.assertEqual(window.effective_ms, 1500)
            self.assertEqual(window.data["window_ms"], 140)

            relative = view.get("program/promo", "relative_gate")
            assert relative is not None
            self.assertEqual(relative.data["threshold"], -9.5)
            self.assertEqual(relative.effective_ms, 1600)

            # v1 recorded 0.25 relative seconds with a 500 ms logical origin.
            window_conflict = next(
                item for item in view.conflicts
                if item["canonical_id"] == "program/promo"
                and item["criterion"] == "window"
            )
            v1_source = next(
                item for item in window_conflict["sources"]
                if item["source"]["kind"] == "v1"
            )
            self.assertEqual(v1_source["effective_ms"], 750)
            self.assertEqual(v1_source["source"]["entry_index"], 0)
            self.assertEqual(v1_source["source"]["record_id"], "v1-promo-window")
            self.assertEqual(window_conflict["winner_generation"], 2)
            self.assertEqual(
                window_conflict["winner_source"]["kind"], "v2-shard"
            )

            # A newer-generation tombstone suppresses the legacy criterion.
            self.assertIsNone(view.get("program/promo", "legacy_peak"))
            self.assertEqual(len(view.conflicts), 6)
            self.assertTrue(view.snapshot_summary["v1"]["valid"])
            self.assertTrue(view.snapshot_summary["v2"]["valid"])

    def test_corrupt_shard_is_reported_with_digest_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = make_workspace(Path(temporary))
            task = load_task(workspace / "task.json", workspace)
            view = merge_snapshots(task)
            digest_issues = [
                issue for issue in view.issues if issue["code"] == "E_DIGEST"
            ]
            self.assertEqual(len(digest_issues), 1)
            self.assertEqual(digest_issues[0]["source"]["shard_id"], "aux")
            aux = next(
                shard for shard in view.snapshot_summary["v2"]["shards"]
                if shard["shard_id"] == "aux"
            )
            self.assertFalse(aux["valid"])
            self.assertTrue(view.snapshot_summary["v2"]["valid"])

    def test_missing_digest_and_declared_count_are_digest_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = make_workspace(Path(temporary))
            task_path = workspace / "task.json"
            task = read_json(task_path)
            del task["snapshots"]["v1"]["sha256"]
            write_json(task_path, task)
            with self.assertRaises(PipelineError) as caught:
                load_task(task_path, workspace)
            self.assertEqual(caught.exception.code, "E_DIGEST")

            v1_path = workspace / "criteria" / "v1.json"
            v1 = read_json(v1_path)
            v1["expected_windows"] = int(v1["expected_windows"]) + 1
            write_json(v1_path, v1)
            task["snapshots"]["v1"]["sha256"] = hashlib.sha256(
                v1_path.read_bytes()
            ).hexdigest()
            write_json(task_path, task)
            varied = load_task(task_path, workspace)
            view = merge_snapshots(varied)
            self.assertFalse(view.snapshot_summary["v1"]["valid"])
            self.assertTrue(view.snapshot_summary["v2"]["valid"])
            self.assertTrue(any(
                issue["code"] == "E_DIGEST" for issue in view.issues
            ))


if __name__ == "__main__":
    unittest.main()
