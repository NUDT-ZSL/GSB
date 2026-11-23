"""业务要求 4：漂移判定与触发裁决的可复现性。"""
import copy

from reopt.runner import Runner


def _run_all(state_dir):
    runner = Runner.open(state_dir)
    try:
        return runner.run()
    finally:
        runner.store.close()


def test_drift_trigger_fields(loaded_state):
    summary = _run_all(loaded_state)
    assert summary["triggers"], "样本应触发至少一次漂移"
    trigger = summary["triggers"][0]
    assert trigger["source_table"] == "B"
    assert trigger["join_key"] == "k_ab"
    assert trigger["ratio_fp"] > trigger["threshold_fp"]
    assert trigger["margin_fp"] == trigger["ratio_fp"] - trigger["threshold_fp"]
    assert trigger["window"][1] - trigger["window"][0] == 1  # 观测窗口 2 个批次


def test_trigger_conclusion_reproducible(loaded_state, tmp_path,
                                         tables_payload, plan_payload,
                                         batches_payload, config_payload):
    first = _run_all(loaded_state)
    state2 = str(tmp_path / "state2")
    Runner.load(state2, tables_payload, plan_payload, batches_payload,
                config_payload).store.close()
    second = _run_all(state2)
    assert first["triggers"] == second["triggers"]
    assert first["switches"] == second["switches"]
    # 持久化的观测与账本也逐位一致
    r1, r2 = Runner.open(loaded_state), Runner.open(state2)
    try:
        assert r1.store.observations() == r2.store.observations()
        assert r1.store.ledger_entries() == r2.store.ledger_entries()
        assert r1.store.triggers() == r2.store.triggers()
    finally:
        r1.store.close()
        r2.store.close()


def test_cooldown_blocks_retrigger(tmp_path, tables_payload, plan_payload,
                                   batches_payload, config_payload):
    config = copy.deepcopy(config_payload)
    config["cooldown_ms"] = 10 ** 9  # 冷却覆盖整个剧本
    state_dir = str(tmp_path / "state")
    Runner.load(state_dir, tables_payload, plan_payload, batches_payload,
                config).store.close()
    summary = _run_all(state_dir)
    assert len(summary["triggers"]) == 1
