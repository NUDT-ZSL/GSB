"""任务 8：应用日志与审计数据分离。"""
from __future__ import annotations

import json
import re
from pathlib import Path

from recon.applog import scan_log_batches
from recon.storage import Storage


def test_log_excludes_business_values(workspace, server):
    result = workspace.run_collector("base")
    assert result.returncode == 0
    cfg = json.load(open(workspace.config("base"), encoding="utf-8"))
    log_path = Path(cfg["log_dir"]) / "recon.log"
    content = log_path.read_text(encoding="utf-8")

    truth = workspace.truth()
    # 不得出现埋入的客户名称或任何金额原始值
    for customer in truth["customers"]:
        assert customer not in content, f"日志泄漏客户名称: {customer}"
    # 金额字符串（带两位小数）不得出现在日志
    amount_pattern = re.compile(r"\d+\.\d{2}")
    assert not amount_pattern.search(content), "日志泄漏金额原始值"
    # 必须包含 run_id 与批次号
    report = workspace.latest_report()
    run_id = report["run_id"]
    assert f"run_id={run_id}" in content
    assert "batch_seq=" in content


def test_audit_tables_linked_by_run_id(workspace, server):
    result = workspace.run_collector("base")
    assert result.returncode == 0
    cfg = json.load(open(workspace.config("base"), encoding="utf-8"))
    storage = Storage(cfg["db_path"])
    report = workspace.latest_report()
    run_id = report["run_id"]

    # 写审计表：写入条数总和 == 主表处理条数（去重后实际落库/合并的记录数）
    write_rows = storage.write_audit_rows(run_id)
    total_written = sum(r["written"] for r in write_rows)
    main_count = storage.count_main()
    # 每条主表记录至少被写一次；跨源更新会使 written 略大于主表行数
    assert total_written >= main_count

    # 日志与审计通过 run_id 关联，批次号集合存在交集且一致
    log_batches = scan_log_batches(cfg["log_dir"], run_id)
    audit_batches = {r["batch_seq"] for r in storage.write_audit_rows(run_id)}
    assert log_batches & audit_batches
    # 审计表每个源的批次号都应能在日志中找到对应
    assert audit_batches.issubset(log_batches)
    storage.close()


def test_audit_conflict_counts(workspace, server):
    result = workspace.run_collector("base")
    assert result.returncode == 0
    cfg = json.load(open(workspace.config("base"), encoding="utf-8"))
    storage = Storage(cfg["db_path"])
    report = workspace.latest_report()
    run_id = report["run_id"]
    conflicts = storage.conflicts(run_id)
    # 冲突审计条数与写审计表记录的 conflicts 总和一致
    conflict_sum = sum(r["conflicts"] for r in storage.write_audit_rows(run_id))
    assert conflict_sum == len(conflicts) == workspace.truth()["conflict_count"]
    storage.close()
