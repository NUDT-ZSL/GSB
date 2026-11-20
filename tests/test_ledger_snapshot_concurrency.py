import json
import threading

import pytest

from transfer_scheduler.concurrency import CommitGate
from transfer_scheduler.errors import (
    ESnapshotCorrupt,
    ESnapshotRollback,
    EStageBusy,
)
from transfer_scheduler.ledger import BudgetLedger
from transfer_scheduler.planner import build_schedule_plan
from transfer_scheduler.snapshots import (
    LEDGER_KIND,
    PLAN_KIND,
    SUMMARY_KIND,
    SnapshotStore,
)


def test_ledger_reserve_release_atomic(tasks, config):
    plan = build_schedule_plan(tasks, config.budget)
    ledger = BudgetLedger.from_config(config.budget)
    ledger.reconcile_with_plan(plan)
    snapshot = ledger.snapshot()
    assert set(snapshot["reservations"]) == set(plan.admitted)
    estimate = plan.estimates[0]
    before = ledger.snapshot()
    ledger.release(estimate.task_id)
    after = ledger.snapshot()
    assert estimate.task_id not in after["reservations"]
    assert after["memory_reserved"] == before["memory_reserved"] - estimate.memory_bytes
    assert after["slots_held"] == before["slots_held"] - 1


def test_ledger_half_state_never_observed(tasks, config):
    plan = build_schedule_plan(tasks, config.budget)
    ledger = BudgetLedger.from_config(config.budget)
    ledger.reconcile_with_plan(plan)
    seen = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            snap = ledger.snapshot()
            # 不变量：任意时刻快照自身闭合——预留总量不超预算，
            # 且内存/槽位预留与条目逐项之和一致（观察不到半扣减）。
            mem = sum(item["memory_bytes"] for item in snap["reservations"].values())
            slots = sum(item["slots"] for item in snap["reservations"].values())
            seen.append(
                mem == snap["memory_reserved"]
                and slots == snap["slots_held"]
                and snap["compute_used"] <= snap["compute_budget"]
            )

    thread = threading.Thread(target=reader)
    thread.start()
    estimate = plan.estimates[1]
    ledger.refund_degradation(estimate.task_id, 10, 16)
    ledger.release(estimate.task_id)
    stop.set()
    thread.join()
    assert seen and all(seen)


def test_snapshot_version_monotonic_and_whole_replace(tmp_path, tasks, config):
    store = SnapshotStore(tmp_path)
    plan = build_schedule_plan(tasks, config.budget)
    first = store.save(PLAN_KIND, plan)
    second = store.save(PLAN_KIND, plan)
    assert second.version == first.version + 1
    assert first.checksum == second.checksum
    reloaded = store.load(PLAN_KIND)
    assert reloaded.version == second.version
    assert reloaded.checksum == second.checksum


def test_snapshot_rollback_rejected(tmp_path):
    store = SnapshotStore(tmp_path)
    store.save(LEDGER_KIND, {"v": 1})
    store.save(LEDGER_KIND, {"v": 2})
    with pytest.raises(ESnapshotRollback):
        store.assert_no_rollback(LEDGER_KIND, 1)
    with pytest.raises(ESnapshotRollback):
        store.save(LEDGER_KIND, {"v": 0}, candidate_version=1)


def test_snapshot_corruption_detected(tmp_path):
    store = SnapshotStore(tmp_path)
    store.save(SUMMARY_KIND, {"ok": True})
    path = tmp_path / "summary.snapshot.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["checksum"] = "deadbeef"
    path.write_text(json.dumps(raw), encoding="utf-8")
    fresh = SnapshotStore(tmp_path)
    with pytest.raises(ESnapshotCorrupt) as info:
        fresh.load(SUMMARY_KIND)
    assert info.value.context["kind"] == SUMMARY_KIND
    assert info.value.context["version"] == 1


def test_snapshot_restart_replaces_in_memory_state(tmp_path, tasks, config):
    store = SnapshotStore(tmp_path)
    plan = build_schedule_plan(tasks, config.budget)
    store.save(PLAN_KIND, plan)
    store.save(PLAN_KIND, plan)
    restarted = SnapshotStore(tmp_path)
    envelope = restarted.load(PLAN_KIND)
    assert envelope.version == 2


def test_commit_gate_non_blocking():
    gate = CommitGate()
    gate.try_begin("owner-1")
    with pytest.raises(EStageBusy) as info:
        gate.try_begin("owner-2")
    assert info.value.context["active_owner"] == "owner-1"
    gate.end("owner-1")
    gate.try_begin("owner-3")
    gate.end("owner-3")


def test_concurrent_commits_only_one_wins():
    gate = CommitGate()
    outcomes = []

    def worker(owner):
        try:
            gate.try_begin(owner)
            outcomes.append((owner, "won"))
            gate.end(owner)
        except EStageBusy:
            outcomes.append((owner, "busy"))

    gate.try_begin("holder")
    threads = [threading.Thread(target=worker, args=(f"c{i}",)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    gate.end("holder")
    assert all(result == "busy" for _, result in outcomes)
