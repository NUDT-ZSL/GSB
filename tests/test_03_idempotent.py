"""任务 3：幂等落库、字段级冲突合并、同事务回滚。"""
from __future__ import annotations

import json

from recon.storage import Storage


def test_single_row_conflict_and_audit(workspace, server):
    result = workspace.run_collector("base")
    assert result.returncode == 0, result.stderr
    cfg = json.load(open(workspace.config("base"), encoding="utf-8"))
    storage = Storage(cfg["db_path"])
    truth = workspace.truth()

    overlap = truth["csv"]["overlap_serial"]
    rows = storage.conn.execute(
        "SELECT * FROM recon_main WHERE serial_no=?", (overlap,)
    ).fetchall()
    assert len(rows) == 1  # 主表该流水号仅一行
    main_row = dict(rows[0])
    # 金额为先生效来源（rest）的值
    assert main_row["amount"] == truth["csv"]["overlap_rest_amount"]
    assert main_row["source"] == "csv|rest" or main_row["source"] == "rest|csv"

    # 冲突审计恰含 1 条，可追溯两个来源、run_id、批次号
    conflicts = storage.conflicts()
    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict["serial_no"] == overlap
    assert conflict["field"] == "amount"
    assert conflict["winner_source"] == "rest"
    assert conflict["loser_source"] == "csv"
    assert conflict["run_id"]
    assert isinstance(conflict["batch_seq"], int)

    # 批次表可关联同一 run_id 与批次号
    batch = storage.conn.execute(
        "SELECT * FROM batch_audit WHERE run_id=? AND source='csv' AND batch_seq=?",
        (conflict["run_id"], conflict["batch_seq"]),
    ).fetchone()
    assert batch is not None
    storage.close()


def test_transaction_failure_rolls_back_all_tables(workspace, server):
    cfg = json.load(open(workspace.config("base"), encoding="utf-8"))
    # 在 rest 第 1 批（batch_seq=0）事务中途抛异常
    result = workspace.run_collector("base", failure_hook="rest:0")
    assert result.returncode != 0

    storage = Storage(cfg["db_path"])
    # 主表、批次表、审计表、写审计表均无该批次半写数据
    assert storage.count_main() == 0
    assert storage.conn.execute("SELECT COUNT(*) c FROM batch_audit").fetchone()["c"] == 0
    assert storage.conn.execute("SELECT COUNT(*) c FROM conflict_audit").fetchone()["c"] == 0
    assert storage.conn.execute("SELECT COUNT(*) c FROM write_audit").fetchone()["c"] == 0
    # 进度表也不得推进（事务回滚）
    assert storage.get_progress("rest") is None
    storage.close()

    # 重启（不带钩子）成功完成，结果与真值一致
    ok = workspace.run_collector("base")
    assert ok.returncode == 0, ok.stderr
    storage = Storage(cfg["db_path"])
    assert storage.main_hash() == workspace.truth()["content_hash"]
    storage.close()


def test_source_field_not_business_conflict(workspace, server):
    """跨源来源追踪字段差异不记为业务字段冲突。"""
    result = workspace.run_collector("base")
    assert result.returncode == 0
    cfg = json.load(open(workspace.config("base"), encoding="utf-8"))
    storage = Storage(cfg["db_path"])
    for conflict in storage.conflicts():
        assert conflict["field"] != "source"
    storage.close()
