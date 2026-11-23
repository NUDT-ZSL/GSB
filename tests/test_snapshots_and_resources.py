from __future__ import annotations

import tempfile
import unittest
import socket
import json
from pathlib import Path
from unittest import mock

from meeting_retrieval.engine import ForcedTermination
from meeting_retrieval.errors import (
    BusinessError,
    E_RESOURCE_LIMIT,
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    E_STAGE_BUSY,
)
from meeting_retrieval.service import MeetingRetrievalApp


class SnapshotAndResourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.ws = Path(self.temp.name) / "ws"
        self.app = MeetingRetrievalApp(self.ws)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_crash_after_text_stage_resumes_from_alignment(self) -> None:
        with self.assertRaises(ForcedTermination) as raised:
            self.app.run(crash_after="text_embeddings")
        self.assertEqual(raised.exception.stage, "text_embeddings")
        recovery = self.app.recover()
        self.assertEqual(recovery["last_committed_stage"], "text_embeddings")
        self.assertEqual(recovery["resume_from_stage"], "alignment_scores")
        self.assertFalse(recovery["complete"])

        restarted = MeetingRetrievalApp(self.ws)
        resumed = restarted.run()
        self.assertTrue(resumed.recovery["complete"])
        self.assertEqual(
            [item["stage"] for item in resumed.recovery["committed_stages"]],
            ["visual_features", "text_embeddings", "alignment_scores", "retrieval_ranking"],
        )
        self.assertEqual(resumed.stages["alignment_scores"]["version"], 1)

    def test_uncommitted_temporary_state_is_discarded(self) -> None:
        with self.assertRaises(ForcedTermination):
            self.app.run(crash_after="visual_features")
        run_id = self.app.recover()["run_id"]
        run_dir = self.ws / "snapshots" / "stages" / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        temp_file = run_dir / ".tmp-interrupted.part"
        temp_file.write_text("not committed", encoding="utf-8")
        recovery = self.app.engine.stages.recover()
        self.assertFalse(temp_file.exists())
        self.assertTrue(any(".tmp-interrupted.part" in path for path in recovery["discarded_temporary_files"]))
        self.assertEqual(recovery["last_committed_stage"], "visual_features")
        second_temp = run_dir / ".tmp-second.part"
        second_temp.write_text("not committed", encoding="utf-8")
        restarted = MeetingRetrievalApp(self.ws)
        self.assertFalse(second_temp.exists())
        self.assertEqual(restarted.recover()["last_committed_stage"], "visual_features")

    def test_stage_versions_are_monotonic_across_updates(self) -> None:
        first = self.app.run()
        second = self.app.update_text("p01", "项目进度评审 下周完成 用户 增长 指标 团队")
        for stage in first.stages:
            self.assertGreater(second.stages[stage]["version"], first.stages[stage]["version"])

    def test_registry_rejects_rollback_and_replaces_same_version_content(self) -> None:
        registry = self.app.engine.registry
        original = registry.current("vocab")
        new_payload = dict(original["payload"])
        new_payload["terms"] = list(new_payload["terms"]) + ["新增"]
        second = registry.put("vocab", new_payload)
        self.assertGreater(second["version"], original["version"])
        with self.assertRaises(BusinessError) as raised:
            registry.put("vocab", original["payload"])
        self.assertEqual(raised.exception.code, E_SNAPSHOT_ROLLBACK)
        with self.assertRaises(BusinessError) as raised:
            registry.put("vocab", {"terms": ["only"], "tokenizer": "longest-match-v1"}, version=1)
        self.assertEqual(raised.exception.code, E_SNAPSHOT_ROLLBACK)

        same_version_payload = {"terms": ["third"], "tokenizer": "longest-match-v1"}
        third = registry.put("vocab", same_version_payload, version=second["version"])
        self.assertEqual(third["version"], second["version"] + 1)
        history = registry.history("vocab")
        self.assertGreaterEqual(len(history), 3)
        self.assertTrue(any(item["version"] == original["version"] for item in history))

    def test_snapshot_checksum_corruption_is_detected(self) -> None:
        current_path = self.ws / "snapshots" / "registry" / "vocab" / "current.json"
        current = current_path.read_text(encoding="utf-8")
        current_path.write_text(current.replace('"version":1', '"version":2'), encoding="utf-8")
        with self.assertRaises(BusinessError) as raised:
            self.app.engine.registry.current("vocab")
        self.assertEqual(raised.exception.code, E_SNAPSHOT_CORRUPT)

    def test_physical_current_pointer_rollback_is_detected(self) -> None:
        registry = self.app.engine.registry
        current_path = self.ws / "snapshots" / "registry" / "vocab" / "current.json"
        old_bytes = current_path.read_bytes()
        payload = dict(registry.current("vocab")["payload"])
        payload["terms"] = list(payload["terms"]) + ["第二版"]
        registry.put("vocab", payload)
        current_path.write_bytes(old_bytes)
        with self.assertRaises(BusinessError) as raised:
            registry.current("vocab")
        self.assertEqual(raised.exception.code, E_SNAPSHOT_ROLLBACK)
        self.assertEqual(raised.exception.details["high_water_version"], 2)

    def test_stage_busy_is_immediate(self) -> None:
        owner = self.app.engine.lock.acquire("test_owner")
        try:
            with self.assertRaises(BusinessError) as raised:
                self.app.run()
            self.assertEqual(raised.exception.code, E_STAGE_BUSY)
            self.assertEqual(raised.exception.details["owner"]["token"], owner["token"])
        finally:
            self.app.engine.lock.release()

    def test_stale_lock_owner_is_reclaimed(self) -> None:
        lock_path = self.app.engine.lock.path
        lock_path.write_text(
            '{"token":"stale","pid":2147483646,"host":"%s","operation":"old","acquired_ns":1}'
            % socket.gethostname(),
            encoding="utf-8",
        )
        with mock.patch("meeting_retrieval.snapshots.SubmissionLock._pid_alive", return_value=False):
            output = self.app.run()
        self.assertTrue(output.recovery["complete"])
        self.assertFalse(lock_path.exists())

    def test_pixel_memory_limit_returns_structured_error(self) -> None:
        self.app.set_resource_limits(max_pixel_bytes_per_image=16)
        with self.assertRaises(BusinessError) as raised:
            self.app.run()
        self.assertEqual(raised.exception.code, E_RESOURCE_LIMIT)
        self.assertEqual(raised.exception.pair_id, "p01")
        self.assertEqual(raised.exception.field, "image_shape")

    def test_cache_entry_limit_returns_structured_error(self) -> None:
        self.app.set_resource_limits(max_cache_entries=3)
        with self.assertRaises(BusinessError) as raised:
            self.app.run()
        self.assertEqual(raised.exception.code, E_RESOURCE_LIMIT)
        self.assertEqual(raised.exception.field, "max_cache_entries")

    def test_max_recompute_pairs_blocks_global_feature_change(self) -> None:
        self.app.run()
        self.app.set_resource_limits(max_recompute_pairs=1)
        with self.assertRaises(BusinessError) as raised:
            self.app.set_feature_params(hist_bins=5)
        self.assertEqual(raised.exception.code, E_RESOURCE_LIMIT)
        self.assertEqual(raised.exception.field, "max_recompute_pairs")
        self.assertEqual(self.app.last_error["code"], E_RESOURCE_LIMIT)

    def test_self_check_passes_after_full_run(self) -> None:
        self.app.run()
        report = self.app.self_check()
        self.assertTrue(report["ok"], report)
        self.assertTrue(all(check["ok"] for check in report["checks"]))

    def test_self_check_detects_corrupt_cache_json(self) -> None:
        self.app.run()
        entry = next((self.ws / "cache" / "entries").glob("*.json"))
        entry.write_text("{broken", encoding="utf-8")
        report = self.app.self_check()
        self.assertFalse(report["ok"])
        self.assertIn("E_CACHE_CORRUPT", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
