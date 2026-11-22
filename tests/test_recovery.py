from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from sparse_inc.checkpoint import CheckpointStore
from sparse_inc.cli import _recovery_demo
from sparse_inc.codes import E_RECOVERY_FAILED, E_SNAPSHOT_CORRUPT
from sparse_inc.errors import BusinessError
from sparse_inc.service import SparseIncrementalService

from tests.support import make_sample_directory


class RecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp, cls.cases = make_sample_directory()
        cls.case = cls.cases["spmm_inc_demo"]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def test_in_process_checkpoint_resume_is_byte_exact(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sparse-inc-recovery-") as root:
            root_path = Path(root)
            full_dir = root_path / "full"
            crash_dir = root_path / "crash"
            full = SparseIncrementalService(
                self.case,
                checkpoint_dir=str(full_dir),
            )
            for index, batch in enumerate(self.case.write_batches):
                full.submit_batch(batch, batch_index=index)

            interrupted = SparseIncrementalService(
                self.case,
                checkpoint_dir=str(crash_dir),
            )
            for index, batch in enumerate(self.case.write_batches[:2]):
                interrupted.submit_batch(batch, batch_index=index)
            CheckpointStore(crash_dir).write_uncommitted_artifact(
                {"phase": "partial_recompute", "next_write_index": 2}
            )

            resumed = SparseIncrementalService(
                self.case,
                checkpoint_dir=str(crash_dir),
                resume=True,
            )
            self.assertTrue(resumed.recovery_info["discarded_pending"])
            self.assertEqual(resumed.next_write_index, 2)
            for index in range(2, len(self.case.write_batches)):
                resumed.submit_batch(self.case.write_batches[index], batch_index=index)

            full_bytes = (full_dir / "checkpoint.json").read_bytes()
            resumed_bytes = (crash_dir / "checkpoint.json").read_bytes()
            self.assertEqual(full_bytes, resumed_bytes)
            self.assertEqual(full.ledger.checksum, resumed.ledger.checksum)
            self.assertEqual(full.materialize().tobytes(), resumed.materialize().tobytes())
            self.assertFalse((crash_dir / "checkpoint.json.pending").exists())

    def test_resume_without_checkpoint_fails(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sparse-inc-empty-") as root:
            with self.assertRaises(BusinessError) as caught:
                SparseIncrementalService(
                    self.case,
                    checkpoint_dir=root,
                    resume=True,
                )
            self.assertEqual(caught.exception.business_code, E_RECOVERY_FAILED)
            self.assertEqual(caught.exception.case_id, self.case.case_id)

    def test_corrupt_checkpoint_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sparse-inc-corrupt-") as root:
            path = Path(root) / "checkpoint.json"
            path.write_text("this is not json", encoding="utf-8")
            with self.assertRaises(BusinessError) as caught:
                SparseIncrementalService(
                    self.case,
                    checkpoint_dir=root,
                    resume=True,
                )
            error = caught.exception
            self.assertEqual(error.business_code, E_SNAPSHOT_CORRUPT)
            self.assertEqual(error.details["snapshot_category"], "checkpoint")
            self.assertEqual(error.case_id, self.case.case_id)

    @unittest.skipUnless(os.name == "posix", "SIGKILL demo requires POSIX")
    def test_sigkill_demo_resumes_to_identical_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sparse-inc-sigkill-") as root:
            root_path = Path(root)
            reports = root_path / "reports"
            runs = root_path / "runs"
            reports.mkdir()
            runs.mkdir()
            manifest = Path(self.temp.name) / "samples" / "cases.jsonl"
            result = _recovery_demo(
                str(manifest),
                "spmm_inc_demo",
                reports,
                runs,
            )
            self.assertTrue(result["forced_termination"]["sigkill_observed"])
            self.assertFalse(
                result["forced_termination"]["complete_exit_report_generated"]
            )
            self.assertTrue(result["forced_termination"]["uncommitted_artifact_left"])
            self.assertTrue(result["resume"]["discarded_pending"])
            self.assertTrue(result["resume"]["pending_artifact_removed"])
            self.assertTrue(result["equivalence"]["checkpoint_bytes_equal"])
            self.assertTrue(result["equivalence"]["ledger_equal"])
            self.assertTrue(result["equivalence"]["result_equal"])
            self.assertTrue(result["equivalence"]["snapshot_envelopes_equal"])
            resumed_report = json.loads(
                (reports / "recovery_resumed.json").read_text(encoding="utf-8")
            )
            self.assertEqual(resumed_report["business_code"], "OK")
            self.assertEqual(resumed_report["recovery"]["loaded_next_write_index"], 2)


if __name__ == "__main__":
    unittest.main()
