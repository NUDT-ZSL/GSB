from __future__ import annotations

import threading
import time

from haptic_engine.budget import InflightBudget


def test_budget_is_bounded_and_waiter_order_uses_device_id():
    budget = InflightBudget(2)
    acquired: list[str] = []
    lock = threading.Lock()

    budget.acquire("dev_z", 0, 2)

    def worker(device_id: str):
        budget.acquire(device_id, 0, 2)
        try:
            with lock:
                acquired.append(device_id)
        finally:
            budget.release(2)

    low = threading.Thread(target=worker, args=("dev_a",))
    high = threading.Thread(target=worker, args=("dev_b",))
    low.start()
    time.sleep(0.08)
    high.start()
    time.sleep(0.08)
    budget.release(2)
    low.join(timeout=2)
    high.join(timeout=2)

    assert acquired == ["dev_a", "dev_b"]
    snapshot = budget.snapshot()
    assert snapshot["peak"] <= 2
    assert snapshot["in_flight"] == 0
    assert snapshot["wait_events"] == 2
