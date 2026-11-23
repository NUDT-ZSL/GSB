"""业务要求 5：形态切换的边界、预算、否决原因与结果一致性。"""
import copy

import pytest

from reopt import errors
from reopt.errors import BusinessError
from reopt.runner import Runner


def _run_all(state_dir):
    runner = Runner.open(state_dir)
    try:
        return runner.run()
    finally:
        runner.store.close()


def test_switch_at_batch_boundary_and_versions(loaded_state):
    summary = _run_all(loaded_state)
    assert summary["switches"], "样本应发生至少一次形态切换"
    versions = [1]
    for sw in summary["switches"]:
        assert sw["from_version"] == versions[-1]
        assert sw["to_version"] == versions[-1] + 1
        versions.append(sw["to_version"])
        # 切换只发生在批次边界：切换序号等于已提交批次序号
        assert sw["seq"] in summary["committed"]
        # 被否决候选均带首个否决原因
        assert sw["rejected"]
        assert all("reason" in r for r in sw["rejected"])
    runner = Runner.open(loaded_state)
    try:
        assert runner.store.form_version() == versions[-1]
    finally:
        runner.store.close()


def test_form_version_regression_rejected(loaded_state):
    runner = Runner.open(loaded_state)
    store = runner.store
    try:
        with store.transaction() as conn:
            store.switch_form(conn, 2, {"steps": []}, 1, {"order": []}, [], 0)
        with pytest.raises(BusinessError) as excinfo:
            with store.transaction() as conn:
                store.switch_form(conn, 2, {"steps": []}, 2, {"order": []}, [], 0)
        assert excinfo.value.code == errors.E_FORM_VERSION_REGRESSION
    finally:
        store.close()


def test_final_result_consistent_across_forms(tmp_path, tables_payload,
                                              plan_payload, batches_payload,
                                              config_payload):
    # 不切换情形：漂移阈值调到不可能触发
    no_switch_config = copy.deepcopy(config_payload)
    no_switch_config["drift_threshold"] = 10 ** 6
    dir_a = str(tmp_path / "a")
    Runner.load(dir_a, tables_payload, plan_payload, batches_payload,
                no_switch_config).store.close()
    _run_all(dir_a)
    # 切换情形：默认样本配置
    dir_b = str(tmp_path / "b")
    Runner.load(dir_b, tables_payload, plan_payload, batches_payload,
                config_payload).store.close()
    summary_b = _run_all(dir_b)
    assert summary_b["switches"]
    ra, rb = Runner.open(dir_a), Runner.open(dir_b)
    try:
        final_a = ra.store.committed_batches()[-1]["result_digest"]
        final_b = rb.store.committed_batches()[-1]["result_digest"]
        assert final_a == final_b  # 切换后累计结果与不切换一致
    finally:
        ra.store.close()
        rb.store.close()


def test_budget_exceeded_keeps_form(tmp_path, tables_payload, plan_payload,
                                    batches_payload, config_payload):
    config = copy.deepcopy(config_payload)
    config["switch_cost_budget"] = 1  # 预算远小于任何候选代价
    state_dir = str(tmp_path / "state")
    Runner.load(state_dir, tables_payload, plan_payload, batches_payload,
                config).store.close()
    summary = _run_all(state_dir)
    assert summary["triggers"], "漂移仍应触发"
    assert not summary["switches"], "预算不足不得切换"
    assert any(e["code"] == errors.E_SWITCH_BUDGET_EXCEEDED
               for e in summary["errors"])
    runner = Runner.open(state_dir)
    try:
        assert runner.store.form_version() == 1  # 保留原形态
    finally:
        runner.store.close()
