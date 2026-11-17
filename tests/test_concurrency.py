from __future__ import annotations

import unittest

from tests.helpers import TempCase, assert_stable_equal, run_once


class ConcurrencyTests(unittest.TestCase):
    def test_thread_count_does_not_change_results(self) -> None:
        case = TempCase()
        try:
            dataset = case.dataset("concurrency", segments=14, turns=7)
            serial_config = case.config(concurrency=1, snapshot_interval=3, scale_tier="small")
            parallel_config = case.config(concurrency=5, snapshot_interval=3, scale_tier="small")
            serial_path = case.write_config(serial_config, "serial.json")
            parallel_path = case.write_config(parallel_config, "parallel.json")
            serial = run_once(dataset, serial_path, case.root / "serial_state", mode="full")
            parallel = run_once(dataset, parallel_path, case.root / "parallel_state", mode="full")
            self.assertEqual(serial["status"], "completed")
            self.assertEqual(parallel["status"], "completed")
            for stage in ("features", "scores", "fusion"):
                self.assertTrue((case.root / "serial_state" / "snapshots" / f"{stage}.json").exists())
            self.assertEqual(serial["segments"], parallel["segments"])
            self.assertEqual(serial["dialog_turns"], parallel["dialog_turns"])
            self.assertEqual(serial["failures"], parallel["failures"])
            self.assertEqual(serial["injections"]["hits"], parallel["injections"]["hits"])
            self.assertEqual(
                serial["diagnostics"]["frame_count_total"],
                parallel["diagnostics"]["frame_count_total"],
            )
            self.assertEqual(
                [segment["filename"] for segment in serial["segments"]],
                sorted((segment["filename"] for segment in parallel["segments"]), key=lambda value: (value.lower(), value)),
            )
        finally:
            case.close()


if __name__ == "__main__":
    unittest.main()
