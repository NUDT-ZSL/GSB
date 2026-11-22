from __future__ import annotations

import unittest

from hotgray.errors import BizError, ErrorCode

from .support import TempWorkspace, new_engine, sqlite_rows, warmed_engine


class RollbackAndClockTests(unittest.TestCase):
    def test_manual_abort_rolls_back_levels_and_restores_baseline(self) -> None:
        with TempWorkspace() as workspace:
            engine = warmed_engine(workspace.path)
            engine.advance_stage(at_ms=1200, level=1)
            engine.advance_stage(at_ms=1300, level=2)
            engine.trigger_rollback(at_ms=1320, reason="injected_failure", replica_id="replica-b")
            first_tick = engine.tick(at_ms=1420)
            self.assertEqual(first_tick["rollback"]["status"], "rolling_back")
            self.assertEqual(first_tick["rollback"]["current_level"], 1)
            second_tick = engine.tick(at_ms=1520)
            self.assertEqual(second_tick["rollback"]["status"], "rolled_back")
            snapshot = engine.snapshot()
            self.assertEqual(snapshot["round"]["status"], "rolled_back")
            self.assertEqual(snapshot["round"]["current_ratio"], 0.0)
            for replica in snapshot["replicas"]:
                if replica["eligible"]:
                    self.assertEqual(replica["state"], "rolled_back")
                    self.assertEqual(replica["serving_version"], 1)
                    self.assertEqual(replica["gray_ratio"], 0.0)

    def test_rollback_past_window_is_marked_not_achieved(self) -> None:
        with TempWorkspace() as workspace:
            engine = warmed_engine(workspace.path)
            engine.advance_stage(at_ms=1200, level=1)
            engine.advance_stage(at_ms=1300, level=2)
            engine.trigger_rollback(at_ms=1320, reason="slow_rollback")
            result = engine.tick(at_ms=1900)
            self.assertEqual(result["rollback"]["status"], "rollback_not_achieved")
            snapshot = engine.snapshot()
            self.assertEqual(snapshot["round"]["status"], "rollback_not_achieved")
            self.assertTrue(
                any(item["code"] == ErrorCode.ROLLBACK_NOT_ACHIEVED.value for item in snapshot["diagnostics"])
            )
            for replica in snapshot["replicas"]:
                if replica["eligible"]:
                    self.assertEqual(replica["state"], "rollback_not_achieved")
                    self.assertEqual(replica["serving_version"], 1)

    def test_warmup_timeout_triggers_rollback_and_never_enters_gray(self) -> None:
        with TempWorkspace() as workspace:
            engine = new_engine(workspace.path)
            engine.apply_patch(at_ms=1000)
            engine.start_warmup(at_ms=1000)
            result = engine.tick(at_ms=1250)
            self.assertEqual(result["rollback"]["status"], "rolled_back")
            self.assertTrue(result["warmup_failures"])
            snapshot = engine.snapshot()
            self.assertEqual(snapshot["round"]["current_level"], 0)
            for replica in snapshot["replicas"]:
                if replica["eligible"]:
                    self.assertEqual(replica["state"], "rolled_back")
                    self.assertIn("warmup_timeout", replica["failure_reason"])
                    self.assertEqual(replica["serving_version"], 1)

    def test_corrupt_shard_digest_fails_warmup_and_rolls_back(self) -> None:
        with TempWorkspace() as workspace:
            engine = new_engine(workspace.path)
            engine.apply_patch(at_ms=1000)
            engine.start_warmup(at_ms=1000)
            changed = next(
                item for item in engine.manifest.model(2).shard_chain()
                if item["layer"] == "head" and item["shard_index"] == 0
            )
            path = sqlite_rows(
                engine.store.db_path,
                "SELECT path FROM materializations WHERE digest = ?",
                (changed["digest"],),
            )[0]["path"]
            with open(path, "wb") as handle:
                handle.write(b"corrupt!")
            result = engine.tick(at_ms=1100)
            self.assertTrue(result["warmup_failures"])
            self.assertEqual(result["rollback"]["status"], "rolled_back")
            snapshot = engine.snapshot()
            self.assertTrue(
                all(item["state"] != "gray" for item in snapshot["replicas"] if item["eligible"])
            )
            self.assertEqual(snapshot["round"]["current_level"], 0)

    def test_clock_regression_beyond_tolerance_changes_nothing(self) -> None:
        with TempWorkspace() as workspace:
            engine = new_engine(workspace.path)
            engine.apply_patch(at_ms=1000)
            before = engine.snapshot()
            with self.assertRaises(BizError) as raised:
                engine.tick(at_ms=900)
            self.assertEqual(raised.exception.code, ErrorCode.CLOCK_REGRESSION)
            self.assertEqual(raised.exception.details["expected"], 1000)
            self.assertEqual(raised.exception.details["actual"], 900)
            after = engine.snapshot()
            self.assertEqual(before, after)

            within_tolerance = engine.tick(at_ms=990)
            self.assertEqual(within_tolerance["at_ms"], 1000)
            snapshot = engine.snapshot()
            self.assertEqual(snapshot["logical_clock"]["watermark_ms"], 1000)


if __name__ == "__main__":
    unittest.main()
