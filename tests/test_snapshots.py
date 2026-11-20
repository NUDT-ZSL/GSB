from __future__ import annotations

import copy
import threading
import unittest

from anomaly_engine.canonical import canonical_sha256
from anomaly_engine.errors import EngineError
from anomaly_engine.snapshots import BudgetLedger, SnapshotStore, verify_snapshot


class SnapshotTests(unittest.TestCase):
    def test_whole_replacement_history_and_rollback(self) -> None:
        store = SnapshotStore()
        first = store.commit("plan", {"value": 1})
        second = store.commit("plan", {"value": 2})
        self.assertEqual(first["version"], 1)
        self.assertEqual(second["version"], 2)
        self.assertEqual(store.get("plan")["content"], {"value": 2})
        self.assertEqual([item["version"] for item in store.history("plan")], [1])
        with self.assertRaises(EngineError) as caught:
            store.commit("plan", {"value": 3}, version=1)
        self.assertEqual(caught.exception.code, "E_SNAPSHOT_ROLLBACK")

    def test_corruption_detection(self) -> None:
        store = SnapshotStore()
        store.commit("budget", {"reserved": 10})
        snapshot = store.get("budget")
        tampered = copy.deepcopy(snapshot)
        tampered["content"]["reserved"] = 0
        with self.assertRaises(EngineError) as caught:
            verify_snapshot(tampered)
        self.assertEqual(caught.exception.code, "E_SNAPSHOT_CORRUPT")

    def test_stage_busy_is_immediate(self) -> None:
        store = SnapshotStore()
        with store.stage_guard("result"):
            with self.assertRaises(EngineError) as caught:
                store.commit("result", {"value": 1})
            self.assertEqual(caught.exception.code, "E_STAGE_BUSY")

    def test_budget_ledger_atomic_visibility(self) -> None:
        ledger = BudgetLedger(10_000)
        errors: list[BaseException] = []
        barrier = threading.Barrier(8)

        def worker(index: int) -> None:
            try:
                barrier.wait()
                ledger.reserve(f"txn-{index}", 1000)
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertFalse(errors)
        self.assertEqual(ledger.reserved, 8000)
        self.assertEqual(ledger.available, 2000)
        snapshot = ledger.snapshot()
        self.assertEqual(snapshot["reserved"], 8000)
        self.assertEqual(
            canonical_sha256(snapshot),
            canonical_sha256(ledger.snapshot()),
        )
        ledger.release("txn-0", reason="test")
        self.assertEqual(ledger.reserved, 7000)
        self.assertEqual(ledger.available, 3000)

    def test_budget_ledger_never_overdraws(self) -> None:
        ledger = BudgetLedger(1500)
        ledger.reserve("a", 1000)
        with self.assertRaises(EngineError) as caught:
            ledger.reserve("b", 1000)
        self.assertEqual(caught.exception.code, "E_COMPARE_BUDGET")
        self.assertEqual(ledger.reserved, 1000)


if __name__ == "__main__":
    unittest.main()
