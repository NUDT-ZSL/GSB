"""任务 6：源端乱序与重复窗口处理、最终游标真值。"""
from __future__ import annotations

import json

from recon.storage import Storage


def test_duplicate_dropped_counts_and_final_cursor(workspace, server):
    result = workspace.run_collector("base")
    assert result.returncode == 0, result.stderr
    report = workspace.latest_report()
    truth = workspace.truth()

    # 报告按源统计 duplicate_dropped
    assert report["sources"]["rest"]["duplicate_dropped"] == 10
    assert report["sources"]["ts"]["duplicate_dropped"] == 5

    # 主表无重复自然键
    cfg = json.load(open(workspace.config("base"), encoding="utf-8"))
    storage = Storage(cfg["db_path"])
    rows = storage.main_rows()
    serials = [r["serial_no"] for r in rows]
    assert len(serials) == len(set(serials))

    # REST 最终游标 = 总页数 + 1
    rest_cursor = report["sources"]["rest"]["cursor"]
    assert rest_cursor == truth["rest"]["total_pages"] + 1

    # TS 最终游标 start = 最大毫秒 + 1（下一区间起点）
    ts_cursor = report["sources"]["ts"]["cursor"]
    assert ts_cursor["start"] == truth["ts"]["next_cursor_expected"]
    storage.close()


def test_unordered_records_do_not_break_cursor(workspace, server):
    """页内乱序不影响游标单调推进（页码严格递增）。"""
    result = workspace.run_collector("base")
    assert result.returncode == 0
    cfg = json.load(open(workspace.config("base"), encoding="utf-8"))
    storage = Storage(cfg["db_path"])
    rest_batches = [
        b for b in storage.batch_rows() if b["source"] == "rest"
    ]
    cursors = [json.loads(b["cursor_to"]) for b in rest_batches]
    assert cursors == sorted(cursors)  # 单调不回退
    assert cursors == [2, 3, 4]
    storage.close()
