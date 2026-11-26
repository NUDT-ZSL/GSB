"""要求 7：阶段状态机、检查点单调版本与中断恢复逐字节一致。"""

import hashlib
import os

from statediag.checkpoints import CheckpointStore
from statediag.clock import LogicalClock
from statediag.ingestion import ingest
from statediag.model import Stage, TransformConfig
from statediag.pipeline import DiagnosticPipeline, _Interrupt

from tests.support import SampleCase


class RecoveryTest(SampleCase):
    def _cfg(self):
        return TransformConfig(1, "float64", "half-even", 1e-12, 1e-9, 0)

    def _hashes(self, pipe, ids):
        return {tid: hashlib.sha256(pipe.ledger_bytes(tid)).hexdigest()
                for tid in ids}

    def test_clean_vs_interrupted_ledger_byte_identical(self):
        ids = [r["task"].task_id
               for r in ingest(self.tasks_path, self.state_dim)]
        clean = DiagnosticPipeline(
            os.path.join(self.new_workdir(), "clean"), self.state_dim,
            self._cfg(), clock=LogicalClock(10_000, 7),
        )
        clean.run(self.tasks_path)
        baseline = self._hashes(clean, ids)

        interrupted = DiagnosticPipeline(
            os.path.join(self.new_workdir(), "interrupted"), self.state_dim,
            self._cfg(), clock=LogicalClock(10_000, 7),
        )
        with self.assertRaises(_Interrupt):
            interrupted.run(self.tasks_path, fail_after_task="t02")
        resumed = interrupted.run(self.tasks_path)
        self.assertEqual(resumed["recovery"]["pending_at_resume"],
                         ["t03", "t04", "t05"])
        self.assertEqual(self._hashes(interrupted, ids), baseline)

    def test_checkpoint_monotonic_and_completed_set(self):
        store = CheckpointStore(self.new_workdir(), clock=LogicalClock())
        c1 = store.commit("t", Stage.SNAPSHOT_COMMIT, ["a"], {"a": "h1"})
        c2 = store.commit("t", Stage.SNAPSHOT_COMMIT, ["a", "b"],
                          {"a": "h1", "b": "h2"})
        self.assertEqual(c1["version"], 1)
        self.assertEqual(c2["version"], 2)
        self.assertEqual(c2["completed_task_ids"], ["a", "b"])
        self.assertEqual(store.latest()["content_checksum"],
                         c2["content_checksum"])

    def test_uncommitted_scratch_voided(self):
        store = CheckpointStore(self.new_workdir(), clock=LogicalClock())
        scratch = store.scratch_path("mid.txt")
        with open(scratch, "w", encoding="utf-8") as handle:
            handle.write("uncommitted")
        store.commit("t", Stage.BLOCK_TRANSFORM, ["a"], {"a": "h"})
        self.assertFalse(os.path.exists(scratch))
