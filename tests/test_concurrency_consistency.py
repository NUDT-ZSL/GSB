"""要求 8：并发提交控制与跨模块一致性自检。"""

import threading

from statediag.cache import content_key
from statediag.concurrency import CommitGate
from statediag.consistency import (
    run_self_check,
    verify_ledger_reproducible,
    verify_partition,
)
from statediag.errors import EInconsistent, EStageBusy
from statediag.ingestion import ingest
from statediag.invalidation import plan_invalidation
from statediag.ledger import build_ledger
from statediag.model import TransformConfig

from tests.support import SampleCase


class ConcurrencyTest(SampleCase):
    def test_second_commit_gets_busy_without_waiting(self):
        gate = CommitGate()
        gate.try_acquire("A")
        with self.assertRaises(EStageBusy) as ctx:
            gate.try_acquire("B")
        self.assertEqual(ctx.exception.code, "E_STAGE_BUSY")
        gate.release("A")
        gate.try_acquire("B")
        self.assertEqual(gate.holder, "B")

    def test_concurrent_try_acquire_only_one_wins(self):
        gate = CommitGate()
        winners = []
        errors = []

        def worker(owner):
            try:
                gate.try_acquire(owner)
                winners.append(owner)
            except EStageBusy:
                errors.append(owner)

        threads = [threading.Thread(target=worker, args=(f"w{i}",))
                   for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(errors), 7)


class ConsistencyTest(SampleCase):
    def setUp(self):
        self.records = ingest(self.tasks_path, self.state_dim)
        self.cfg = TransformConfig(1, "float64", "half-even", 1e-12, 1e-9, 0)
        self.old_by_id = {r["task"].task_id: r["task"] for r in self.records}

    def test_ledger_independently_recomputable(self):
        record = self.records[0]
        ledger, _ = build_ledger(record["task"], record["initial_state"],
                                 self.cfg)
        self.assertTrue(
            verify_ledger_reproducible(
                record["task"], self.state_dim, self.cfg, ledger
            )
        )

    def test_tampered_ledger_reports_first_deviation_task(self):
        record = self.records[1]
        ledger, _ = build_ledger(record["task"], record["initial_state"],
                                 self.cfg)
        ledger["entries"][1]["max_abs_diff"] = 999.0
        with self.assertRaises(EInconsistent) as ctx:
            verify_ledger_reproducible(
                record["task"], self.state_dim, self.cfg, ledger
            )
        self.assertEqual(ctx.exception.code, "E_INCONSISTENT")
        self.assertEqual(ctx.exception.details["first_deviation_task"],
                         record["task"].task_id)

    def test_self_check_partition_and_recompute(self):
        revision_records = ingest(self.revision_path, self.state_dim)
        cache_keys = {
            content_key(r["task"], self.cfg) for r in self.records
        }
        plan = plan_invalidation(
            [r["task"] for r in revision_records], self.old_by_id,
            self.cfg, self.cfg, cache_keys,
        )
        recorded = {}
        for record in self.records:
            ledger, _ = build_ledger(record["task"],
                                     record["initial_state"], self.cfg)
            recorded[record["task"].task_id] = ledger
        result = run_self_check(
            plan, [r["task"].task_id for r in self.records],
            self.old_by_id, self.state_dim, {"float64": self.cfg}, recorded,
        )
        self.assertTrue(result["ok"])

    def test_overlapping_partition_rejected(self):
        revision_records = ingest(self.revision_path, self.state_dim)
        plan = plan_invalidation(
            [r["task"] for r in revision_records], self.old_by_id,
            self.cfg, self.cfg, set(),
        )
        object.__setattr__(plan, "reuse_task_ids",
                           plan.reuse_task_ids + ("t03",))
        with self.assertRaises(EInconsistent):
            verify_partition(
                plan, [r["task"].task_id for r in self.records]
            )
