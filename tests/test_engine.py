from __future__ import annotations

import copy
import unittest

from anim_replay.engine import build_replay, compare_replays
from anim_replay.persistence import output_digest
from anim_replay.schema import validate_input

from tests.helpers import sample_input_data


class DeterministicEngineTests(unittest.TestCase):
    def test_workers_and_chunking_are_bit_identical(self) -> None:
        data = sample_input_data()
        digests = {}
        for chunk_ms in (32, 96, 256):
            variant = copy.deepcopy(data)
            variant["timeline"]["chunk_ms"] = chunk_ms
            loaded = validate_input(variant)
            for workers in (1, 2, 4):
                result = build_replay(
                    loaded.spec,
                    max_workers=workers,
                    memory_budget_bytes=512 * 1024,
                )
                digests[(chunk_ms, workers)] = output_digest(result)
        self.assertEqual(len(set(digests.values())), 1, digests)

    def test_event_contract_and_pose_entry(self) -> None:
        loaded = validate_input(sample_input_data())
        result = build_replay(loaded.spec, max_workers=2, memory_budget_bytes=512 * 1024)
        self.assertEqual(len(result.events), 5)
        self.assertEqual(len({event.t for event in result.events}), len(result.events))
        event_times = {event.rule_id: event.t for event in result.events}
        self.assertEqual(event_times["jump_to_land"], 1680)
        self.assertEqual(event_times["land_to_idle"], 2160)
        frames = {frame.t: frame for frame in result.frames}
        for event in result.events:
            frame = frames[event.t]
            self.assertEqual(frame.clip, event.target_clip)
            self.assertEqual(frame.local_ms, 0)
            self.assertEqual(frame.generation, event.state_generation)

    def test_repeat_matches_exactly(self) -> None:
        loaded = validate_input(sample_input_data())
        left = build_replay(loaded.spec, max_workers=1, memory_budget_bytes=512 * 1024)
        right = build_replay(loaded.spec, max_workers=4, memory_budget_bytes=512 * 1024)
        comparison = compare_replays(left, right, tolerance=0.0)
        self.assertTrue(comparison["ok"], comparison)
        self.assertEqual(comparison["max_abs_diff"], 0.0)
        self.assertEqual(output_digest(left), output_digest(right))


if __name__ == "__main__":
    unittest.main()

