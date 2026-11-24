from __future__ import annotations

import unittest

from anim_replay.engine import build_replay
from anim_replay.errors import ReplayError
from anim_replay.reconnect import make_client_cache, reconnect_catchup

from tests.helpers import sample_loaded


class ReconnectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.loaded = sample_loaded()
        cls.result = build_replay(
            cls.loaded.spec,
            max_workers=2,
            memory_budget_bytes=512 * 1024,
        )

    def test_missing_poses_are_filled_before_events(self) -> None:
        times = [frame.t for frame in self.result.frames if not (320 <= frame.t < 640)]
        cache = make_client_cache(self.result, frame_times=times, applied_event_count=1)
        report = reconnect_catchup(self.result, cache)
        operations = report["operations"]
        self.assertEqual(report["missing_pose_count"], 20)
        fill_times = [item["logical_ms"] for item in operations if item["op"] == "fill_pose"]
        self.assertEqual(fill_times, sorted(fill_times))
        first_event = next(index for index, item in enumerate(operations) if item["op"] == "replay_event")
        self.assertTrue(all(item["op"] == "fill_pose" for item in operations[:first_event]))
        self.assertTrue(all(item["op"] == "replay_event" for item in operations[first_event:]))
        self.assertEqual(report["event_replay_count"], 4)

    def test_pose_divergence_reports_first_location(self) -> None:
        cache = make_client_cache(self.result, applied_event_count=1)
        for frame in cache["frames"]:
            if frame["t"] == 800:
                frame["bones"]["hips.rotation"][0] += 0.25
                break
        with self.assertRaises(ReplayError) as context:
            reconnect_catchup(self.result, cache)
        mismatch = context.exception.details["first_mismatch"]
        self.assertEqual(mismatch["logical_ms"], 800)
        self.assertEqual(mismatch["clip"], "run")
        self.assertEqual(mismatch["bone_channel"], "hips.rotation")

    def test_event_history_divergence_is_replay_error(self) -> None:
        cache = make_client_cache(self.result, applied_event_count=2)
        cache["events"] = [event.to_dict() for event in self.result.events[:2]]
        cache["events"][1]["to"] = "idle"
        with self.assertRaises(ReplayError) as context:
            reconnect_catchup(self.result, cache)
        mismatch = context.exception.details["first_mismatch"]
        self.assertEqual(mismatch["reason"], "event_mismatch")
        self.assertEqual(mismatch["logical_ms"], 640)


if __name__ == "__main__":
    unittest.main()

