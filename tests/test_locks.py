from __future__ import annotations

import unittest

from txlab.locks import LockManager, S_LOCK, X_LOCK


class LockManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = LockManager()
        for index, session_id in enumerate(("a", "b", "c"), start=1):
            self.manager.begin_transaction(
                f"tx-{index}",
                session_id,
                start_seq=index,
                isolation="repeatable_read",
            )

    def test_wait_queue_is_stable_fifo(self) -> None:
        acquired = self.manager.request("tx-1", "row:1", X_LOCK, 0, 0)
        self.assertTrue(acquired.granted)

        second = self.manager.request("tx-2", "row:1", S_LOCK, 0, 10)
        third = self.manager.request("tx-3", "row:1", S_LOCK, 0, 20)
        self.assertFalse(second.granted)
        self.assertFalse(third.granted)
        self.assertEqual(second.queue_position, 1)
        self.assertEqual(third.queue_position, 2)

        snapshot = self.manager.rows_snapshot()[0]
        self.assertEqual(
            [waiter["session_id"] for waiter in snapshot["waiters"]],
            ["b", "c"],
        )

        self.manager.end_transaction("tx-1")
        second_retry = self.manager.retry("tx-2")
        self.assertTrue(second_retry.granted)
        self.assertEqual(self.manager.queue_position("tx-3"), 1)
        third_retry = self.manager.retry("tx-3")
        self.assertTrue(third_retry.granted)

    def test_upgrade_waits_for_other_shared_holder(self) -> None:
        self.assertTrue(self.manager.request("tx-1", "row:1", S_LOCK, 0, 0).granted)
        self.assertTrue(self.manager.request("tx-2", "row:1", S_LOCK, 0, 0).granted)
        upgrade = self.manager.request("tx-1", "row:1", X_LOCK, 1, 10)
        self.assertFalse(upgrade.granted)
        self.assertIn("tx-2", upgrade.blockers)
        self.manager.end_transaction("tx-2")
        self.assertTrue(self.manager.retry("tx-1").granted)

    def test_wait_graph_detects_cycle(self) -> None:
        self.manager.request("tx-1", "row:A", S_LOCK, 0, 0)
        self.manager.request("tx-2", "row:B", S_LOCK, 0, 0)
        self.manager.request("tx-1", "row:B", X_LOCK, 1, 10)
        self.manager.request("tx-2", "row:A", X_LOCK, 1, 20)
        deadlock = self.manager.find_deadlock()
        self.assertIsNotNone(deadlock)
        assert deadlock is not None
        self.assertEqual(deadlock["cycle_members"], ["a", "b"])
        self.assertEqual(deadlock["victim_session_id"], "a")
        self.assertEqual(deadlock["rule"], "lowest_session_id")


if __name__ == "__main__":
    unittest.main()

