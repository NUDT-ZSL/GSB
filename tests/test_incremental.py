from __future__ import annotations

import unittest

from anim_replay.engine import build_replay
from anim_replay.incremental import incremental_replay
from anim_replay.schema import apply_edits

from tests.helpers import sample_edits, sample_loaded


class IncrementalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.base = sample_loaded()
        cls.full_edits = sample_edits("edits.json")
        cls.keyframe_edits = sample_edits("edits_keyframe_only.json")

    def _run(self, edits):
        edited, _ = apply_edits(self.base, edits)
        reference = build_replay(edited.spec, max_workers=2, memory_budget_bytes=512 * 1024)
        result, report = incremental_replay(
            self.base,
            edits,
            reference=reference,
            max_workers=3,
            memory_budget_bytes=512 * 1024,
        )
        return result, report

    def test_full_edit_script_matches_full_replay(self) -> None:
        result, report = self._run(self.full_edits)
        self.assertTrue(report.events_equal)
        self.assertIsNone(report.first_over_tolerance)
        self.assertEqual(report.max_abs_diff, 0.0)
        self.assertTrue(report.preserved_reused_payloads)
        self.assertGreater(len(report.recomputed_chunks), 0)
        self.assertGreater(len(report.reused_chunks), 0)
        self.assertEqual(
            len(report.recomputed_chunks) + len(report.reused_chunks),
            len(result.chunks),
        )
        categories = {
            reason["reason"]
            for item in report.edit_invalidations
            for reason in item["reasons"]
        }
        self.assertIn("keyframe_interval", categories)
        self.assertIn("clip_duration_shift", categories)
        self.assertIn("transition_condition_shift", categories)

    def test_keyframe_only_leaves_independent_chunks_reused(self) -> None:
        result, report = self._run(self.keyframe_edits)
        self.assertTrue(report.events_equal)
        self.assertIsNone(report.first_over_tolerance)
        self.assertEqual(report.max_abs_diff, 0.0)
        self.assertEqual(report.reused_chunks, [10, 11, 12, 13, 14, 15, 16])
        self.assertEqual(len(report.recomputed_chunks), 19)
        for item in report.edit_invalidations:
            if item["op"] in {"set_keyframe_value", "add_keyframe", "delete_keyframe", "move_keyframe"}:
                for reason in item["reasons"]:
                    self.assertEqual(reason["reason"], "keyframe_interval")
                    self.assertIn(reason["bone"], item["target"]["bone"])
        self.assertTrue(report.preserved_reused_payloads)


if __name__ == "__main__":
    unittest.main()

