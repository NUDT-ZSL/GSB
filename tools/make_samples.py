"""确定性构造多来源 JSONL 样例与映射配置。

所有数据为固定字面量，无随机数，任何机器上重复运行结果一致。

生成内容：
  input/
    source_a_20261114.jsonl   来源 A：含文件内重复主键、缺必填记录
    source_b_20261114.jsonl   来源 B：含跨来源重复与字段冲突
    source_b_extra.jsonl      来源 B：含缺主键记录（用行号合成标识进入报告）
    source_a_locked.jsonl     演示时由 hold_lock.py 占用（本批次读不到）
    source_b_truncated.jsonl  最后一行只有半截 JSON
  config/
    mapping.json              合法映射配置
    bad_mapping_target.json   错误样例 1：映射目标不在 canonical_fields
    bad_mapping_syntax.json   错误样例 2：JSON 语法错误
"""

from __future__ import annotations

import json
from pathlib import Path

CANONICAL_FIELDS = [
    "record_id", "customer_name", "amount", "biz_time",
    "channel", "remark",
]

MAPPING_CONFIG = {
    "batch_id": "BATCH-20261114-01",
    "primary_key": "record_id",
    "canonical_fields": CANONICAL_FIELDS,
    "required_fields": ["record_id", "customer_name", "amount", "biz_time"],
    "date_fields": ["biz_time"],
    "default_timezone": "Asia/Shanghai",
    "sources": [
        {
            "name": "source_a",
            "priority": 1,
            "file_match": "source_a",
            "default_timezone": "Asia/Shanghai",
            "input_date_formats": ["%Y/%m/%d %H:%M:%S"],
            "mappings": {
                "biz_id": "record_id",
                "cust_name": "customer_name",
                "trade_amount": "amount",
                "event_time": "biz_time",
                "channel_code": "channel",
                "note": "remark",
                "internal_trace": "remark",
            },
        },
        {
            "name": "source_b",
            "priority": 2,
            "file_match": "source_b",
            "default_timezone": "UTC",
            "input_date_formats": ["%Y-%m-%dT%H:%M:%S"],
            "mappings": {
                "recordId": "record_id",
                "customer": "customer_name",
                "amt": "amount",
                "ts": "biz_time",
                "remark_text": "remark",
            },
        },
    ],
}

# ---- 来源 A：字段名不同、斜杠日期（无时区，按 Asia/Shanghai 解释） ----------
SOURCE_A_LINES = [
    {"biz_id": "A001", "cust_name": "张伟", "trade_amount": 1200.0,
     "event_time": "2026/11/14 09:30:00", "channel_code": "WEB",
     "note": "A001 首次记录", "internal_trace": "trace-a001-1"},
    # 文件内重复主键：第二条业务时间更新，应覆盖第一条
    {"biz_id": "A001", "cust_name": "张伟", "trade_amount": 1280.0,
     "event_time": "2026/11/14 10:05:00", "channel_code": "WEB",
     "note": "A001 更新记录", "internal_trace": "trace-a001-2"},
    {"biz_id": "A002", "cust_name": "李娜", "trade_amount": 860.5,
     "event_time": "2026/11/14 08:15:00", "channel_code": "APP",
     "note": "正常记录", "internal_trace": "trace-a002"},
    # 缺必填 amount，必须被拒绝并写入报告
    {"biz_id": "A003", "cust_name": "王强",
     "event_time": "2026/11/14 07:50:00", "channel_code": "POS",
     "note": "缺少金额", "internal_trace": "trace-a003"},
]

# ---- 来源 B：驼峰字段、UTC 时区日期 ------------------------------------------
# B001 与 A001 跨来源重复；金额与备注冲突，A 优先级 1 更高 -> 裁决取 A
SOURCE_B_LINES = [
    {"recordId": "A001", "customer": "ZHANG Wei", "amt": 999.99,
     "ts": "2026-11-14T02:00:00Z", "remark_text": "B 侧较早记录"},
    # B002：UTC 时间 01:30Z == 北京时间 09:30，验证两种日期归一为同一时刻
    {"recordId": "B002", "customer": "Chen Min", "amt": 450.0,
     "ts": "2026-11-14T01:30:00Z", "remark_text": "UTC 带时区记录"},
    # B003：无时区标记，按来源默认 UTC 解释，再换算到 Asia/Shanghai
    {"recordId": "B003", "customer": "Liu Yang", "amt": 75.25,
     "ts": "2026-11-14T03:00:00", "remark_text": "无时区标记记录"},
]

SOURCE_B_EXTRA_LINES = [
    {"recordId": "B004", "customer": "Zhao Lei", "amt": 3200,
     "ts": "2026-11-14T05:45:00Z", "remark_text": "正常记录"},
    # 缺主键 recordId（映射后 record_id 缺失），必须被拒绝
    {"customer": "No ID Customer", "amt": 10,
     "ts": "2026-11-14T06:00:00Z", "remark_text": "缺少主键"},
]

SOURCE_A_LOCKED_LINES = [
    {"biz_id": "L001", "cust_name": "锁定文件客户甲", "trade_amount": 500,
     "event_time": "2026/11/14 11:00:00", "channel_code": "WEB",
     "note": "锁定文件中的记录", "internal_trace": "trace-l001"},
    {"biz_id": "L002", "cust_name": "锁定文件客户乙", "trade_amount": 640,
     "event_time": "2026/11/14 11:10:00", "channel_code": "APP",
     "note": "锁定文件中的记录", "internal_trace": "trace-l002"},
]

# 截断文件：前两行完整，最后一行只有半截
SOURCE_B_TRUNCATED_GOOD = [
    {"recordId": "T001", "customer": "Truncated One", "amt": 11,
     "ts": "2026-11-14T07:00:00Z", "remark_text": "截断文件完整行 1"},
    {"recordId": "T002", "customer": "Truncated Two", "amt": 22,
     "ts": "2026-11-14T07:05:00Z", "remark_text": "截断文件完整行 2"},
]
TRUNCATED_TAIL = '{"recordId": "T003", "customer": "Half Line", "amt": 33,'


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8",
    )


def generate(base_dir: Path) -> None:
    input_dir = base_dir / "input"
    config_dir = base_dir / "config"
    input_dir.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)

    _write_jsonl(input_dir / "source_a_20261114.jsonl", SOURCE_A_LINES)
    _write_jsonl(input_dir / "source_b_20261114.jsonl", SOURCE_B_LINES)
    _write_jsonl(input_dir / "source_b_extra.jsonl", SOURCE_B_EXTRA_LINES)
    _write_jsonl(input_dir / "source_a_locked.jsonl", SOURCE_A_LOCKED_LINES)

    truncated = "".join(
        json.dumps(r, ensure_ascii=False) + "\n" for r in SOURCE_B_TRUNCATED_GOOD
    ) + TRUNCATED_TAIL
    (input_dir / "source_b_truncated.jsonl").write_text(truncated, encoding="utf-8")

    (config_dir / "mapping.json").write_text(
        json.dumps(MAPPING_CONFIG, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # 错误配置 1：映射目标 customer_name_typo 不在 canonical_fields
    bad_target = json.loads(json.dumps(MAPPING_CONFIG))
    bad_target["sources"][0]["mappings"]["cust_name"] = "customer_name_typo"
    (config_dir / "bad_mapping_target.json").write_text(
        json.dumps(bad_target, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # 错误配置 2：JSON 语法错误（第 5 行附近缺逗号）
    bad_syntax = (
        '{\n'
        '  "batch_id": "BATCH-BAD",\n'
        '  "primary_key": "record_id",\n'
        '  "canonical_fields": ["record_id"]\n'
        '  "required_fields": ["record_id"]\n'
        '}\n'
    )
    (config_dir / "bad_mapping_syntax.json").write_text(
        bad_syntax, encoding="utf-8"
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="构造确定性 JSONL 样例")
    parser.add_argument("--dir", default="samples", help="样例根目录")
    args = parser.parse_args()
    target = Path(args.dir)
    generate(target)
    print(f"样例已生成于 {target.resolve()}")
