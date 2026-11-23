"""业务要求 2：确定性批次生成，相同种子与描述任意运行次数逐位一致。"""
import numpy as np

from reopt import datagen
from reopt.schema import parse_tables


def _tables(tables_payload):
    _, tables = parse_tables(tables_payload)
    return tables


def test_deterministic_across_runs(tables_payload):
    tables = _tables(tables_payload)
    digests_run1 = {}
    digests_run2 = {}
    for seq, nrows in ((1, 120), (2, 130), (3, 0)):
        for name, table in tables.items():
            cols1 = datagen.generate_batch(table, 20261124, seq, nrows)
            cols2 = datagen.generate_batch(table, 20261124, seq, nrows)
            order = tuple(c.name for c in table.columns)
            digests_run1[(name, seq)] = datagen.batch_digest(name, seq, cols1, order)
            digests_run2[(name, seq)] = datagen.batch_digest(name, seq, cols2, order)
            for col in order:
                assert np.array_equal(cols1[col], cols2[col])
                assert cols1[col].shape[0] == nrows
    assert digests_run1 == digests_run2


def test_seed_changes_digest(tables_payload):
    tables = _tables(tables_payload)
    table = tables["A"]
    order = tuple(c.name for c in table.columns)
    d1 = datagen.batch_digest("A", 1, datagen.generate_batch(table, 1, 1, 50), order)
    d2 = datagen.batch_digest("A", 1, datagen.generate_batch(table, 2, 1, 50), order)
    assert d1 != d2


def test_skew_concentration(tables_payload):
    tables = _tables(tables_payload)
    table = tables["B"]
    cols = datagen.generate_batch(table, 20261124, 1, 20000)
    keys = cols["k_ab"]
    hot_fraction = float(np.mean(keys < 4))
    assert 0.7 < hot_fraction < 0.9  # 声明比例 0.8 集中于 4 个热键
