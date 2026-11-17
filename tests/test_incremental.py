from __future__ import annotations

import json
import shutil
import unittest

from tests.helpers import TempCase, assert_stable_equal, run_once


class IncrementalTests(unittest.TestCase):
    def test_edit_sequence_matches_blank_full_rebuild(self) -> None:
        case = TempCase()
        try:
            dataset = case.dataset("incremental", segments=18, turns=9)
            state = case.root / "state"
            full_counter = {"value": 0}

            def expect_full_match(label: str, incremental_report: dict) -> None:
                full_counter["value"] += 1
                full_report = run_once(
                    dataset,
                    config_path,
                    case.root / f"fresh_{full_counter['value']}_{label}",
                    mode="full",
                )
                assert_stable_equal(self, incremental_report, full_report)
                self.assertEqual(incremental_report["report_fingerprint"], full_report["report_fingerprint"])

            config = case.config(
                emotion_granularity="fine",
                classifier="nearest_centroid",
                scale_tier="small",
                concurrency=3,
                snapshot_interval=4,
                fusion={"window": 2, "window_mode": "fixed"},
            )
            config_path = case.write_config(config, "config.json")
            first = run_once(dataset, config_path, state)
            self.assertEqual(first["status"], "completed")
            expect_full_match("initial", first)

            second = run_once(dataset, config_path, state)
            self.assertEqual(second["cache"]["features"]["misses"], 0)
            self.assertEqual(second["cache"]["scores"]["misses"], 0)
            self.assertEqual(second["cache"]["fusion"]["misses"], 0)
            assert_stable_equal(self, first, second)

            config = case.config(
                emotion_granularity="coarse",
                classifier="nearest_centroid",
                scale_tier="small",
                concurrency=3,
                snapshot_interval=4,
                fusion={"window": 2, "window_mode": "fixed"},
            )
            config_path = case.write_config(config, "config.json")
            coarse = run_once(dataset, config_path, state)
            self.assertEqual(coarse["cache"]["features"]["misses"], 0)
            self.assertGreater(coarse["cache"]["scores"]["misses"], 0)
            self.assertGreater(coarse["cache"]["fusion"]["misses"], 0)
            expect_full_match("coarse", coarse)

            config = case.config(
                emotion_granularity="coarse",
                classifier="weighted_logistic_regression",
                scale_tier="small",
                concurrency=3,
                snapshot_interval=4,
                fusion={"window": 2, "window_mode": "fixed"},
            )
            config_path = case.write_config(config, "config.json")
            switched = run_once(dataset, config_path, state)
            self.assertEqual(switched["cache"]["features"]["misses"], 0)
            self.assertGreater(switched["cache"]["scores"]["misses"], 0)
            expect_full_match("classifier", switched)

            config = case.config(
                emotion_granularity="coarse",
                classifier="weighted_logistic_regression",
                scale_tier="small",
                concurrency=3,
                snapshot_interval=4,
                fusion={"window": 1, "window_mode": "fixed"},
            )
            config_path = case.write_config(config, "config.json")
            narrowed = run_once(dataset, config_path, state)
            self.assertEqual(narrowed["cache"]["features"]["misses"], 0)
            self.assertEqual(narrowed["cache"]["scores"]["misses"], 0)
            self.assertGreater(narrowed["cache"]["fusion"]["misses"], 0)
            expect_full_match("fusion-window", narrowed)

            source = dataset / "audio" / "seg_0002.wav"
            replacement = dataset / "audio" / "seg_0001.wav"
            shutil.copyfile(replacement, source)
            replaced = run_once(dataset, config_path, state)
            self.assertEqual(replaced["cache"]["features"]["recomputed"], ["audio/seg_0002.wav"])
            self.assertEqual(replaced["cache"]["scores"]["recomputed"], ["audio/seg_0002.wav"])
            self.assertGreaterEqual(len(replaced["cache"]["fusion"]["recomputed"]), 2)
            expect_full_match("audio-replacement", replaced)

            config = case.config(
                emotion_granularity="coarse",
                classifier="weighted_logistic_regression",
                scale_tier="medium",
                concurrency=3,
                snapshot_interval=4,
                fusion={"window": 1, "window_mode": "fixed"},
            )
            config_path = case.write_config(config, "config.json")
            scaled = run_once(dataset, config_path, state)
            self.assertGreater(scaled["cache"]["features"]["misses"], 0)
            self.assertNotEqual(
                scaled["scale_decision"]["selected"]["strategy_id"],
                narrowed["scale_decision"]["selected"]["strategy_id"],
            )
            expect_full_match("scale-tier", scaled)

            incremental = run_once(dataset, config_path, state)
            self.assertEqual(incremental["status"], "completed")
            expect_full_match("repeat-incremental", incremental)
        finally:
            case.close()


if __name__ == "__main__":
    unittest.main()
