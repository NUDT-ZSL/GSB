"""业务要求 6：误差账本与精度敏感标记。"""
import copy

from reopt.ledger import ErrorLedger
from reopt.reoptimizer import Candidate, choose_form
from reopt.runner import Runner


def test_ledger_records_scale_rounding_discarded(loaded_state):
    runner = Runner.open(loaded_state)
    try:
        runner.run()
        entries = runner.store.ledger_entries()
    finally:
        runner.store.close()
    assert entries, "每个观测点与偏差比计算都应记账"
    for entry in entries:
        assert entry["scale"] == 1000000
        assert entry["rounding"] in ("floor", "exact")
        assert entry["discarded"] >= 0
        assert entry["error_bound"] >= 0
    assert any(e["op"] == "add" for e in entries)
    assert any(e["rounding"] == "floor" for e in entries)


def test_error_bounds_accumulate():
    ledger = ErrorLedger(scale=1000)
    fp = ledger.to_fp(1, 3, "t:div")  # 333，舍弃余量
    assert fp == 333
    assert ledger.entries[-1].rounding == "floor"
    assert ledger.entries[-1].error_bound == 1
    ledger.record_add("t:add", [1, 1])
    ledger.record_mul("t:mul", 1000, 2000, 1, 1)
    assert ledger.add_error_bound == 2
    assert ledger.mul_error_bound == 1000 * 1 + 2000 * 1 + 1
    assert ledger.total_error_bound == ledger.add_error_bound + ledger.mul_error_bound


def test_precision_sensitive_flag():
    ledger = ErrorLedger(scale=1000000)
    current = Candidate(order=("A", "B", "C"), algorithms=("hash", "hash"))
    other = Candidate(order=("B", "C", "A"), algorithms=("hash", "hash"))
    third = Candidate(order=("C", "B", "A"), algorithms=("hash", "hash"))
    costs = {current: 5000, other: 4000, third: 4001}  # 前两名差值 < 容差
    decision = choose_form(current, [current, other, third], costs,
                           budget_fp=10 ** 9, tolerance_fp=100,
                           ledger=ledger, context="t")
    assert decision.precision_sensitive
    assert decision.sensitive_tables == ["A", "B", "C"]
    assert decision.sensitive_costs_fp == (4000, 4001)
    assert decision.chosen == other


def test_precision_sensitive_persisted(tmp_path, tables_payload, plan_payload,
                                       batches_payload, config_payload):
    config = copy.deepcopy(config_payload)
    config["float_tolerance"] = 10 ** 12  # 容差极大，任意候选差都敏感
    state_dir = str(tmp_path / "state")
    Runner.load(state_dir, tables_payload, plan_payload, batches_payload,
                config).store.close()
    runner = Runner.open(state_dir)
    try:
        summary = runner.run()
        flags = runner.store.precision_flags()
    finally:
        runner.store.close()
    assert summary["precision_sensitive"]
    assert flags
    flag = flags[0]
    assert flag["tables_json"] and flag["error_bound_fp"] >= 0
    assert flag["window_end"] >= flag["window_start"]
