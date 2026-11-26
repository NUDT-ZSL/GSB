"""固定内存预算守卫与跨运行逐字节确定性。"""

import hashlib
import os

from statediag.clock import LogicalClock
from statediag.errors import EBudgetExceeded
from statediag.ledger import build_ledger
from statediag.ingestion import ingest
from statediag.model import TransformConfig
from statediag.pipeline import DiagnosticPipeline

from tests.support import SampleCase


class MemoryTest(SampleCase):
    def test_budget_overflow_raises(self):
        record = ingest(self.tasks_path, self.state_dim)[0]
        cfg = TransformConfig(1, "float64", "half-even", 1e-12, 1e-9, 0)
        from statediag.memory import MemoryBudget

        budget = MemoryBudget(64)
        with self.assertRaises(EBudgetExceeded) as ctx:
            build_ledger(record["task"], record["initial_state"], cfg,
                         budget=budget)
        self.assertEqual(ctx.exception.code, "E_BUDGET_EXCEEDED")

    def test_high_water_reported(self):
        record = ingest(self.tasks_path, self.state_dim)[0]
        cfg = TransformConfig(1, "float64", "half-even", 1e-12, 1e-9, 0)
        from statediag.memory import MemoryBudget

        budget = MemoryBudget(8 * 1024 * 1024)
        build_ledger(record["task"], record["initial_state"], cfg,
                     budget=budget)
        self.assertGreater(budget.high_water_bytes, 0)
        self.assertEqual(budget.used_bytes, 0)


class DeterminismTest(SampleCase):
    def test_pipeline_rerun_byte_identical(self):
        cfg = TransformConfig(1, "float64", "half-even", 1e-12, 1e-9, 0)
        ids = [r["task"].task_id
               for r in ingest(self.tasks_path, self.state_dim)]

        def run(root):
            pipe = DiagnosticPipeline(
                os.path.join(root, "p"), self.state_dim, cfg,
                clock=LogicalClock(10_000, 7),
            )
            pipe.run(self.tasks_path)
            return {
                tid: hashlib.sha256(pipe.ledger_bytes(tid)).hexdigest()
                for tid in ids
            }

        first = run(self.new_workdir())
        second = run(self.new_workdir())
        self.assertEqual(first, second)

    def test_sample_regeneration_byte_identical(self):
        before = {}
        state_dir = os.path.join(self.sample_dir, "states")
        for name in sorted(os.listdir(state_dir)):
            with open(os.path.join(state_dir, name), "rb") as handle:
                before[name] = hashlib.sha256(handle.read()).hexdigest()
        from tests.support import GENERATOR

        GENERATOR.main(self.sample_dir)
        after = {}
        for name in sorted(os.listdir(state_dir)):
            with open(os.path.join(state_dir, name), "rb") as handle:
                after[name] = hashlib.sha256(handle.read()).hexdigest()
        self.assertEqual(before, after)
