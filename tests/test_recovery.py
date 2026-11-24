from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from audio_pipeline.pipeline import PipelineOptions, run_pipeline

from tests.helpers import configure_compression, make_workspace, read_json


class RecoveryTests(unittest.TestCase):
    def test_interrupt_marker_and_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = make_workspace(Path(temporary))
            interrupted = run_pipeline(
                workspace / "task.json",
                PipelineOptions(
                    workspace_root=workspace,
                    simulate_interrupt_after_chunks=2,
                ),
            )
            self.assertEqual(interrupted.exit_code, 143)
            self.assertTrue(interrupted.interrupted)
            self.assertEqual(interrupted.report["status"], "interrupted")
            marker_path = workspace / "state" / "interrupted.json"
            self.assertTrue(marker_path.exists())
            marker = read_json(marker_path)
            self.assertEqual(marker["signal"], 15)
            self.assertEqual(marker["boundary"], "window-chunk")
            self.assertFalse((workspace / "output" / "report.json").exists())
            chunk_files = list((workspace / "state" / "chunks").glob("*/*.json"))
            self.assertGreaterEqual(len(chunk_files), 2)

            resumed = run_pipeline(
                workspace / "task.json",
                PipelineOptions(workspace_root=workspace, resume=True),
            )
            self.assertEqual(resumed.exit_code, 3)
            self.assertFalse(marker_path.exists())
            report = read_json(workspace / "output" / "report.json")
            self.assertEqual(report["summary"]["measured_inputs"], 3)
            self.assertEqual(report["summary"]["rejected_inputs"], 2)

    def test_corrupt_chunk_falls_back_and_is_redone(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = make_workspace(Path(temporary))
            run_pipeline(
                workspace / "task.json",
                PipelineOptions(workspace_root=workspace),
            )
            chunk_paths = []
            for path in (workspace / "state" / "chunks").glob("*/*.json"):
                document = read_json(path)
                if document["input_id"] == "promo" and document["chunk_index"] == 1:
                    chunk_paths.append(path)
            self.assertEqual(len(chunk_paths), 1)
            with chunk_paths[0].open("ab") as handle:
                handle.write(b"corrupt")

            # Change a decision-only rule so the report version changes and the
            # recovery diagnostics are observable in the committed report.
            configure_compression(
                workspace, threshold_lufs=-8.0, ratio=4.0, enabled=True
            )
            outcome = run_pipeline(
                workspace / "task.json",
                PipelineOptions(workspace_root=workspace, resume=True),
            )
            self.assertEqual(outcome.exit_code, 3)
            report = read_json(workspace / "output" / "report.json")
            promo = next(item for item in report["inputs"] if item["input_id"] == "promo")
            self.assertIn("E_DIGEST", promo["business_codes"])
            self.assertIn("state_issues", promo["diagnostics"])
            self.assertFalse(promo["measurement"]["measurement_reused"])
            # The invalid chunk and its successors were replaced by valid ones.
            replacement = read_json(chunk_paths[0])
            self.assertEqual(replacement["input_id"], "promo")
            self.assertEqual(replacement["chunk_index"], 1)


if __name__ == "__main__":
    unittest.main()
