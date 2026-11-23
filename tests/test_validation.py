"""业务要求 1：解析校验与业务错误码，非法输入不落盘。"""
import copy
import os

import pytest

from reopt import errors
from reopt.errors import BusinessError
from reopt.runner import Runner


def _expect(code, mutate, tables_payload, plan_payload, batches_payload,
            config_payload, tmp_path):
    args = [copy.deepcopy(tables_payload), copy.deepcopy(plan_payload),
            copy.deepcopy(batches_payload), copy.deepcopy(config_payload)]
    mutate(*args)
    state_dir = str(tmp_path / "state")
    with pytest.raises(BusinessError) as excinfo:
        Runner.load(state_dir, *args)
    assert excinfo.value.code == code
    # 非法输入不得写入任何持久化状态
    assert not os.path.exists(os.path.join(state_dir, "state.db"))


def test_duplicate_table_name(tables_payload, plan_payload, batches_payload,
                              config_payload, tmp_path):
    def mutate(t, p, b, c):
        t["tables"].append(copy.deepcopy(t["tables"][0]))
    _expect(errors.E_TABLE_NAME_DUPLICATE, mutate, tables_payload, plan_payload,
            batches_payload, config_payload, tmp_path)


def test_ndv_exceeds_rows(tables_payload, plan_payload, batches_payload,
                          config_payload, tmp_path):
    def mutate(t, p, b, c):
        t["tables"][0]["columns"][0]["ndv"] = t["tables"][0]["rows"] + 1
    _expect(errors.E_NDV_INVALID, mutate, tables_payload, plan_payload,
            batches_payload, config_payload, tmp_path)


def test_ndv_nonpositive(tables_payload, plan_payload, batches_payload,
                         config_payload, tmp_path):
    def mutate(t, p, b, c):
        t["tables"][0]["columns"][0]["ndv"] = 0
    _expect(errors.E_NDV_INVALID, mutate, tables_payload, plan_payload,
            batches_payload, config_payload, tmp_path)


def test_width_nonpositive(tables_payload, plan_payload, batches_payload,
                           config_payload, tmp_path):
    def mutate(t, p, b, c):
        t["tables"][0]["columns"][0]["width_bytes"] = 0
    _expect(errors.E_COL_WIDTH_INVALID, mutate, tables_payload, plan_payload,
            batches_payload, config_payload, tmp_path)


def test_predicate_column_missing(tables_payload, plan_payload, batches_payload,
                                  config_payload, tmp_path):
    def mutate(t, p, b, c):
        p["steps"][0]["right_key"] = "no_such_col"
    _expect(errors.E_PREDICATE_COLUMN_MISSING, mutate, tables_payload,
            plan_payload, batches_payload, config_payload, tmp_path)


def test_predicate_type_incomparable(tables_payload, plan_payload,
                                     batches_payload, config_payload, tmp_path):
    def mutate(t, p, b, c):
        t["tables"][1]["columns"][0]["type"] = "float64"
    _expect(errors.E_PREDICATE_TYPE_INCOMPARABLE, mutate, tables_payload,
            plan_payload, batches_payload, config_payload, tmp_path)


def test_batch_seq_not_increasing(tables_payload, plan_payload, batches_payload,
                                  config_payload, tmp_path):
    def mutate(t, p, b, c):
        b["batches"][1]["seq"] = b["batches"][0]["seq"]
    _expect(errors.E_BATCH_SEQ_NOT_INCREASING, mutate, tables_payload,
            plan_payload, batches_payload, config_payload, tmp_path)


def test_batch_rows_negative(tables_payload, plan_payload, batches_payload,
                             config_payload, tmp_path):
    def mutate(t, p, b, c):
        b["batches"][0]["rows"]["A"] = -1
    _expect(errors.E_BATCH_ROWS_NEGATIVE, mutate, tables_payload, plan_payload,
            batches_payload, config_payload, tmp_path)


def test_clock_negative(tables_payload, plan_payload, batches_payload,
                        config_payload, tmp_path):
    def mutate(t, p, b, c):
        b["batches"][0]["clock_ms"] = -5
    _expect(errors.E_CLOCK_NEGATIVE, mutate, tables_payload, plan_payload,
            batches_payload, config_payload, tmp_path)


def test_algorithm_not_in_enum(tables_payload, plan_payload, batches_payload,
                               config_payload, tmp_path):
    def mutate(t, p, b, c):
        p["steps"][0]["algorithm"] = "gpu_merge"
    _expect(errors.E_ALGO_NOT_IN_ENUM, mutate, tables_payload, plan_payload,
            batches_payload, config_payload, tmp_path)


def test_unknown_table_in_plan(tables_payload, plan_payload, batches_payload,
                               config_payload, tmp_path):
    def mutate(t, p, b, c):
        p["steps"][0]["right"] = "ZZ"
    _expect(errors.E_PLAN_TABLE_UNKNOWN, mutate, tables_payload, plan_payload,
            batches_payload, config_payload, tmp_path)
