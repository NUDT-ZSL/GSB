from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import unittest

from hdr_color.processor import TerminationController, run_processing
from hdr_color.task import load_task
from tests.helpers import base_task, frame_value, write_case


class ConcurrencyTests(unittest.TestCase):
    def test_output_is_elementwise_identical_for_concurrency_one_and_four(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            frames = [frame_value(index, pixels=7) for index in range(23)]
            frames[0] = [[0.0, 0.0, 0.0] for _ in range(7)]
            frames[7] = [[30.0, 25.0, 20.0] for _ in range(7)]
            frames[15] = [[0.00001, 0.00002, 0.00003] for _ in range(7)]
            reports = {}
            outputs = {}
            for concurrency in (1, 4):
                case = root / f"case{concurrency}"
                task = base_task(f"out{concurrency}")
                task["task_id"] = "concurrency-equivalence"
                task["processing"]["chunk_frames"] = 3
                task["processing"]["concurrency"] = concurrency
                task_path = write_case(case, task, frames)
                loaded = load_task(task_path)
                outcome = run_processing(
                    loaded.config,
                    TerminationController(event=threading.Event()),
                )
                self.assertEqual(outcome["status"], "complete")
                outputs[concurrency] = json.loads(
                    (case / f"out{concurrency}/converted_frames.json").read_text(
                        encoding="utf-8"
                    )
                )
                reports[concurrency] = json.loads(
                    (case / f"out{concurrency}/statistics_report.json").read_text(
                        encoding="utf-8"
                    )
                )
                if concurrency == 4:
                    self.assertGreater(
                        reports[concurrency]["processing"][
                            "effective_inflight_chunks"
                        ],
                        1,
                    )

            self.assertEqual(
                outputs[1]["frames"],
                outputs[4]["frames"],
            )
            self.assertEqual(
                reports[1]["statistics"]["frames"],
                reports[4]["statistics"]["frames"],
            )
            self.assertEqual(
                reports[1]["statistics"]["windows"],
                reports[4]["statistics"]["windows"],
            )
            self.assertEqual(
                reports[1]["mapping_summary"],
                reports[4]["mapping_summary"],
            )


if __name__ == "__main__":
    unittest.main()
