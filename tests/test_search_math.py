from __future__ import annotations

import unittest

from audiofp.search import _build_score_trace


def _group(
    *,
    frame: int,
    fixed: int,
    reference: float,
    count: int,
    key: str,
) -> dict:
    return {
        "representative_key": key,
        "query_anchor_frame": frame,
        "query_target_frame_min": frame + 1,
        "query_target_frame_max": frame + 1,
        "reference_anchor_frame_min": frame,
        "reference_anchor_frame_max": frame,
        "reference_target_frame_min": frame + 1,
        "reference_target_frame_max": frame + 1,
        "offset_samples_mean": 0.0,
        "offset_frame_round": 0,
        "collision_count": count,
        "reference_weight": reference * count,
        "fixed_weight_units": fixed * count,
        "key_occurrences": 1,
        "weight_bins": {f"{fixed}:{reference:.17g}": count},
    }


class FixedPointMathTests(unittest.TestCase):
    def test_first_tolerance_crossing_is_match_indexed(self) -> None:
        votes = [
            _group(frame=1, fixed=1_000_000, reference=1.0, count=3, key="a"),
            _group(
                frame=5,
                fixed=1_000_000,
                reference=1.0000004,
                count=2,
                key="b",
            ),
        ]
        trace, fixed, reference, first, maximum = _build_score_trace(
            votes, fixed_scale=1_000_000, tolerance=0.0
        )
        self.assertEqual(fixed, 5_000_000)
        self.assertAlmostEqual(reference, 5.0000008)
        self.assertEqual(first, 4)
        self.assertGreater(maximum, 0.0)
        self.assertEqual(trace[-1]["match_index"], 5)
        self.assertEqual(trace[-1]["collision_count"], 2)


if __name__ == "__main__":
    unittest.main()

