from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from anim_replay.engine import build_replay
from anim_replay.errors import VersionError
from anim_replay.persistence import StopToken, output_digest, run_persistent_replay
from anim_replay.schema import apply_edits

from tests.helpers import sample_edits, sample_loaded


class PersistenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.loaded = sample_loaded()
        cls.full = build_replay(cls.loaded.spec, max_workers=2, memory_budget_bytes=512 * 1024)
        cls.expected_digest = output_digest(cls.full)

    def test_stop_at_boundary_and_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            interrupted = run_persistent_replay(
                self.loaded.spec,
                state,
                max_workers=2,
                memory_budget_bytes=512 * 1024,
                stop_after_chunks=6,
            )
            self.assertTrue(interrupted.interrupted)
            self.assertEqual(interrupted.exit_code, 130)
            self.assertEqual(interrupted.report["written_chunk_count"], 6)
            self.assertFalse((state / "report.json").exists())
            resumed = run_persistent_replay(
                self.loaded.spec,
                state,
                max_workers=4,
                memory_budget_bytes=512 * 1024,
            )
            self.assertFalse(resumed.interrupted)
            self.assertEqual(resumed.report["recovered_chunk_count"], 6)
            self.assertEqual(output_digest(resumed.result), self.expected_digest)
            self.assertTrue((state / "report.json").exists())

    def test_corrupt_chunk_is_not_used(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            run_persistent_replay(
                self.loaded.spec,
                state,
                max_workers=2,
                memory_budget_bytes=512 * 1024,
                stop_after_chunks=10,
            )
            path = state / "chunks" / "chunk_00000004.json"
            body = json.loads(path.read_text(encoding="utf-8"))
            body["frames"][0]["bones"]["root.translation"][0] += 1.0
            path.write_text(json.dumps(body), encoding="utf-8")
            resumed = run_persistent_replay(
                self.loaded.spec,
                state,
                max_workers=3,
                memory_budget_bytes=512 * 1024,
            )
            self.assertEqual(resumed.report["recovered_chunk_count"], 4)
            self.assertEqual(resumed.report["invalid_chunks"][0]["chunk"], 4)
            self.assertGreater(resumed.report["written_chunk_count"], 0)
            self.assertEqual(output_digest(resumed.result), self.expected_digest)

    def test_version_change_is_rejected_before_replace(self) -> None:
        edits = sample_edits("edits.json")
        edited, _ = apply_edits(self.loaded, edits)
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            run_persistent_replay(
                self.loaded.spec,
                state,
                max_workers=2,
                memory_budget_bytes=512 * 1024,
                stop_after_chunks=2,
            )
            before = (state / "chunks" / "chunk_00000000.json").read_bytes()
            with self.assertRaises(VersionError):
                run_persistent_replay(
                    edited.spec,
                    state,
                    max_workers=2,
                    memory_budget_bytes=512 * 1024,
                )
            self.assertEqual((state / "chunks" / "chunk_00000000.json").read_bytes(), before)

    def test_presignalled_token_stops_before_first_chunk(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            token = StopToken()
            token.request(143, reason="test_sigterm")
            result = run_persistent_replay(
                self.loaded.spec,
                state,
                stop_token=token,
                max_workers=1,
                memory_budget_bytes=512 * 1024,
            )
            self.assertTrue(result.interrupted)
            self.assertEqual(result.exit_code, 143)
            self.assertEqual(result.report["written_chunk_count"], 0)


if __name__ == "__main__":
    unittest.main()

