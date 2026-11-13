"""任务 1：三个数据源适配器 fetch(cursor, limit) 与 CSV 部分写入检测。"""
from __future__ import annotations

from recon.adapters import CsvAdapter, RestAdapter, TimestampAdapter


def test_rest_adapter_batches_and_cursor(workspace, server):
    truth = workspace.truth()
    adapter = RestAdapter(f"http://127.0.0.1:{workspace.port}/rest")
    cursor = adapter.initial_cursor()
    assert cursor == 1
    seen = []
    total_pages = truth["rest"]["total_pages"]
    for batch_no in range(1, 4):  # 至少 3 批
        result = adapter.fetch(cursor, 200)
        assert result.status == "ok"
        if batch_no < total_pages:
            assert len(result.records) == 200
            assert result.exhausted is False
        else:
            assert result.exhausted is True
        assert result.extra["total_pages"] == total_pages
        seen.extend(r["serial_no"] for r in result.records)
        # 页码游标严格 +1 推进
        assert result.next_cursor == cursor + 1
        cursor = result.next_cursor
    # 末页之后游标为 total_pages+1
    assert cursor == total_pages + 1
    # 服务端实际投递条数 = 真值（含 10 条跨页重复）
    assert len(seen) == truth["rest"]["served_count"]
    adapter.close()


def test_ts_adapter_half_open_windows(workspace, server):
    truth = workspace.truth()
    adapter = TimestampAdapter(f"http://127.0.0.1:{workspace.port}/ts", window_ms=100)
    cursor = adapter.initial_cursor()
    assert set(cursor) == {"start", "end"}
    assert cursor["end"] - cursor["start"] == 100
    batches = 0
    empty_streak = 0
    while batches < 3 or empty_streak < 2:
        result = adapter.fetch(cursor, 200)
        batches += 1
        # 半开区间：本次 end 必须等于下次 start
        assert result.next_cursor["start"] == cursor["end"]
        assert result.next_cursor["end"] == cursor["end"] + 100
        for record in result.records:
            assert cursor["start"] <= record["event_ts"] < cursor["end"]
        empty_streak = empty_streak + 1 if result.extra["empty"] else 0
        cursor = result.next_cursor
        if batches > 50:
            break
    assert batches >= 3


def test_csv_adapter_offset_version_and_truth(workspace, server):
    truth = workspace.truth()
    adapter = CsvAdapter(workspace.manifest["csv_path"])
    cursor = adapter.initial_cursor()
    all_rows = []
    versions = set()
    for _ in range(3):  # 至少 3 批：用小 limit 强制分页
        result = adapter.fetch(cursor, 100)
        assert result.status == "ok"
        assert result.should_retry is False
        versions.add(result.extra["version"])
        assert len(result.records) in (100, 0) or result.exhausted
        all_rows.extend(result.records)
        cursor = result.next_cursor
        if result.exhausted:
            break
    assert len(all_rows) == truth["csv"]["unique_count"]
    assert len(versions) == 1  # 稳定版本内摘要不变


def test_csv_partial_write_then_retry(workspace, server):
    csv_path = workspace.manifest["csv_path"]
    with open(csv_path, "rb") as fh:
        original = fh.read()

    adapter = CsvAdapter(csv_path)
    # 先正常拉一批建立稳定版本游标
    first = adapter.fetch(adapter.initial_cursor(), 50)
    assert first.status == "ok"
    stable_cursor = first.next_cursor

    # 截断最后一行后半段：无换行结尾 + 字段缺失
    truncated = original.rsplit(b"\n", 2)[0] + b"\nC-000299,3253.61,Soylent"
    with open(csv_path, "wb") as fh:
        fh.write(truncated)
    bad = adapter.fetch(stable_cursor, 50)
    assert bad.status == "should_retry"
    assert bad.should_retry is True
    assert bad.error_code == "E_CSV_PARTIAL"
    assert bad.records == []  # 残缺行不计入批次
    # 偏移不得推进
    assert bad.next_cursor["offset"] == stable_cursor["offset"]

    # 补全文件后重试，返回完整批次
    with open(csv_path, "wb") as fh:
        fh.write(original)
    recovered = adapter.fetch(stable_cursor, 50)
    assert recovered.status == "ok"
    assert recovered.should_retry is False
    assert len(recovered.records) > 0


def test_csv_field_count_mismatch_is_partial(workspace, server):
    """完整行（有换行）但字段数不等于表头列数，也判定为部分写入。"""
    csv_path = workspace.manifest["csv_path"]
    with open(csv_path, "rb") as fh:
        original = fh.read()
    adapter = CsvAdapter(csv_path)
    first = adapter.fetch(adapter.initial_cursor(), 50)
    stable_cursor = first.next_cursor

    # 追加一个带换行但字段缺失的不完整行
    broken = original + b"C-000300,100.00\n"
    with open(csv_path, "wb") as fh:
        fh.write(broken)
    bad = adapter.fetch(stable_cursor, 50)
    assert bad.status == "should_retry"
    assert bad.error_code == "E_CSV_PARTIAL"
    assert bad.extra["reason"] == "field_count_mismatch"
    assert bad.records == []

    with open(csv_path, "wb") as fh:
        fh.write(original)
    recovered = adapter.fetch(stable_cursor, 50)
    assert recovered.status == "ok"


def test_csv_new_version_full_rescan(workspace, server):
    """全量覆盖的新版本必须重新扫描，不沿用旧版本行偏移。"""
    csv_path = workspace.manifest["csv_path"]
    adapter = CsvAdapter(csv_path)
    first = adapter.fetch(adapter.initial_cursor(), 50)
    old_version = first.extra["version"]
    # 用新内容全量覆盖（新版本摘要）
    with open(csv_path, "rb") as fh:
        content = fh.read()
    new_content = content.replace(b"DC49", b"DC48", 1)
    with open(csv_path, "wb") as fh:
        fh.write(new_content)
    result = adapter.fetch(first.next_cursor, 50)
    assert result.status == "ok"
    assert result.extra["version"] != old_version
    # 新版本从偏移 0 重新扫描
    assert result.next_cursor["version"] == result.extra["version"]
    # 恢复文件
    with open(csv_path, "wb") as fh:
        fh.write(content)
