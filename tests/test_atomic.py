from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from hdr_color.processor import TerminationController, run_processing
from hdr_color.task import load_task
from tests.helpers import base_task, frame_value, write_case


def _run(task_path: Path) -> dict[str, object]:
    loaded = load_task(task_path)
    return run_processing(
        loaded.config,
        TerminationController(event=threading.Event()),
    )


class AtomicOutputTests(unittest.TestCase):
    def test_same_version_is_not_replaced_and_new_version_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = base_task("out")
            task_path = write_case(
                root, task, [frame_value(index, pixels=5) for index in range(7)]
            )
            first = _run(task_path)
            self.assertEqual(first["status"], "complete")
            frames_path = root / "out/converted_frames.json"
            report_path = root / "out/statistics_report.json"
            first_frames_mtime = frames_path.stat().st_mtime_ns
            first_report_mtime = report_path.stat().st_mtime_ns
            first_report = json.loads(report_path.read_text(encoding="utf-8"))

            time.sleep(0.02)
            second = _run(task_path)
            self.assertEqual(second["status"], "unchanged")
            self.assertEqual(frames_path.stat().st_mtime_ns, first_frames_mtime)
            self.assertEqual(report_path.stat().st_mtime_ns, first_report_mtime)
            self.assertEqual(
                json.loads(report_path.read_text(encoding="utf-8"))["version"],
                first_report["version"],
            )

            state_path = root / "out/run-state.json"
            interrupted = json.loads(state_path.read_text(encoding="utf-8"))
            interrupted["status"] = "interrupted"
            state_path.write_text(
                json.dumps(interrupted, ensure_ascii=False),
                encoding="utf-8",
            )
            recovered = _run(task_path)
            self.assertEqual(recovered["status"], "unchanged")
            recovered_state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(recovered_state["status"], "complete")
            self.assertEqual(recovered_state["phase"], "recovered_complete_output")

            task["tone_mapping"]["exposure"] = 1.1
            write_case(root, task)
            time.sleep(0.02)
            third = _run(task_path)
            self.assertEqual(third["status"], "complete")
            self.assertNotEqual(third["version"], first_report["version"])
            second_report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(
                second_report["parameter_summary"]["tone_mapping"]["exposure"],
                1.1,
            )
            self.assertNotEqual(
                json.loads(frames_path.read_text(encoding="utf-8"))["version"],
                first_report["version"],
            )
            self.assertFalse((root / "out/.hdr-color-work").exists())
            self.assertEqual(list((root / "out").glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
