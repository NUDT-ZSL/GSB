"""业务要求 7：持久化状态与强制终止后的恢复。"""
import pytest

from reopt import executor
from reopt.runner import Runner


def _snapshot(state_dir):
    runner = Runner.open(state_dir)
    try:
        store = runner.store
        return {
            "watermark": store.watermark(),
            "logical_clock": store.logical_clock(),
            "form_version": store.form_version(),
            "batches": store.committed_batches(),
            "observations": store.observations(),
            "triggers": store.triggers(),
            "switches": store.switches(),
            "ledger": store.ledger_entries(),
        }
    finally:
        runner.store.close()


def test_resume_matches_uninterrupted_run(loaded_state, tmp_path,
                                          tables_payload, plan_payload,
                                          batches_payload, config_payload):
    # 中断情形：先推进到批次 3，再 resume 到末尾
    runner = Runner.open(loaded_state)
    try:
        runner.run(until_seq=3)
    finally:
        runner.store.close()
    mid = _snapshot(loaded_state)
    assert mid["watermark"] == 3
    runner = Runner.open(loaded_state)
    try:
        runner.run()  # resume：只重放水位之后的批次
    finally:
        runner.store.close()
    resumed = _snapshot(loaded_state)
    # 参照情形：一次性跑完
    ref_dir = str(tmp_path / "ref")
    Runner.load(ref_dir, tables_payload, plan_payload, batches_payload,
                config_payload).store.close()
    runner = Runner.open(ref_dir)
    try:
        runner.run()
    finally:
        runner.store.close()
    reference = _snapshot(ref_dir)
    assert resumed == reference  # 不重复累计漂移观测或耗时


def test_kill_mid_batch_leaves_no_partial_state(loaded_state):
    runner = Runner.open(loaded_state)
    original = executor.verify_against_truth
    calls = {"n": 0}

    def bomb(plan, base, intermediates):
        calls["n"] += 1
        if calls["n"] == 3:  # 第 3 个批次执行中途强制终止
            raise KeyboardInterrupt("simulated kill -9")
        return original(plan, base, intermediates)

    executor.verify_against_truth = bomb
    try:
        with pytest.raises(KeyboardInterrupt):
            runner.run()
    finally:
        executor.verify_against_truth = original
        runner.store.close()
    state = _snapshot(loaded_state)
    assert state["watermark"] == 2  # 批次 3 未留下任何部分状态
    assert all(b["seq"] <= 2 for b in state["batches"])
    assert all(o["seq"] <= 2 for o in state["observations"])
    # 恢复后完整跑完，结果与未中断一致
    runner = Runner.open(loaded_state)
    try:
        runner.run()
    finally:
        runner.store.close()
    assert _snapshot(loaded_state)["watermark"] == 6


def test_kill_mid_switch_leaves_no_partial_state(loaded_state):
    runner = Runner.open(loaded_state)
    store = runner.store
    original = store.switch_form

    def bomb(*args, **kwargs):
        raise KeyboardInterrupt("simulated kill during switch")

    store.switch_form = bomb
    try:
        with pytest.raises(KeyboardInterrupt):
            runner.run()
    finally:
        store.switch_form = original
        runner.store.close()
    state = _snapshot(loaded_state)
    # 切换事务回滚：形态指针仍是版本 1，且批次 2 未提交（同事务）
    assert state["form_version"] == 1
    assert state["watermark"] == 1
    assert not state["switches"]
    runner = Runner.open(loaded_state)
    try:
        runner.run()
    finally:
        runner.store.close()
    assert _snapshot(loaded_state)["watermark"] == 6
