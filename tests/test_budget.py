"""预算账本与并行/串行一致性测试。"""

import json
import unittest
from concurrent.futures import ThreadPoolExecutor

from dp_bench.budget import BudgetLedger
from dp_bench.errors import BudgetExhaustedError, NotScheduledError


class LedgerTest(unittest.TestCase):
    def test_ordered_deduction_matches_serial(self):
        ids = ["A", "B", "C"]
        ledger = BudgetLedger(ids, global_budget=10, quota=100)

        def consume(sid, n):
            for step in range(1, n + 1):
                ledger.request(sid, step)
            ledger.release(sid)

        with ThreadPoolExecutor(max_workers=3) as pool:
            # 故意乱序提交，账本仍须按 A、B、C 全序扣减
            futs = [pool.submit(consume, "C", 3),
                    pool.submit(consume, "A", 4),
                    pool.submit(consume, "B", 3)]
            for fut in futs:
                fut.result()

        seq = [(d["sentence_id"], d["step_no"])
               for d in ledger.deductions()]
        expect = [("A", i) for i in range(1, 5)] + \
                 [("B", i) for i in range(1, 4)] + \
                 [("C", i) for i in range(1, 4)]
        self.assertEqual(seq, expect)
        self.assertEqual(ledger.global_consumed, 10)
        self.assertEqual(ledger.curve()[-1], 10)

    def test_global_exhaustion_marks_rest_unscheduled(self):
        ids = ["A", "B", "C", "D"]
        ledger = BudgetLedger(ids, global_budget=5, quota=100)
        for step in range(1, 4):
            ledger.request("A", step)
        ledger.release("A")
        # B 只能再消耗 2 步，第 3 步触发预算耗尽
        ledger.request("B", 1)
        ledger.request("B", 2)
        with self.assertRaises(BudgetExhaustedError):
            ledger.request("B", 3)
        ledger.release("B")
        self.assertEqual(ledger.mark_unscheduled(), ["C", "D"])
        with self.assertRaises(NotScheduledError):
            ledger.request("C", 1)

    def test_quota_exhaustion(self):
        ledger = BudgetLedger(["A", "B"], global_budget=100, quota=2)
        ledger.request("A", 1)
        ledger.request("A", 2)
        with self.assertRaises(BudgetExhaustedError):
            ledger.request("A", 3)
        self.assertTrue(ledger.mark_unscheduled())  # B 未调度

    def test_pre_consumed_resume_rebuilds_state(self):
        ids = ["A", "B", "C"]
        ledger = BudgetLedger(ids, global_budget=10, quota=100,
                              pre_consumed={"A": 3, "B": 2})
        self.assertEqual(ledger.global_remaining, 5)
        self.assertEqual(ledger.global_consumed, 5)
        self.assertEqual(ledger.curve(), [0, 1, 2, 3, 4, 5])
        # 轮次已越过 A、B，C 可继续申请
        ledger.request("C", 1)
        self.assertEqual(ledger.used_of("C"), 1)
        seq = [(d["sentence_id"], d["step_no"])
               for d in ledger.deductions()]
        self.assertEqual(seq[:3], [("A", 1), ("A", 2), ("A", 3)])
        self.assertEqual(seq[-1], ("C", 1))

    def test_pre_consumed_with_zero_used_sentence(self):
        ids = ["A", "B", "C"]
        # B 为零步数已完成句（如未调度/异常前未消耗），游标仍须越过 B
        ledger = BudgetLedger(ids, global_budget=10, quota=100,
                              pre_consumed={"A": 2, "B": 0})
        self.assertEqual(ledger._cursor, 2)
        ledger.request("C", 1)
        self.assertEqual(ledger.used_of("C"), 1)
        self.assertEqual(ledger.global_remaining, 7)

    def test_resume_under_global_exhaustion(self):
        ids = ["A", "B", "C"]
        # A 用完全部 4 步预算后崩溃；恢复后 B 应立即预算耗尽、C 未调度
        ledger = BudgetLedger(ids, global_budget=4, quota=100,
                              pre_consumed={"A": 4})
        with self.assertRaises(BudgetExhaustedError):
            ledger.request("B", 1)
        ledger.release("B")
        self.assertEqual(ledger.mark_unscheduled(), ["C"])


if __name__ == "__main__":
    unittest.main()
