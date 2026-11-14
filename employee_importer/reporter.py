# -*- coding: utf-8 -*-
"""JSON 报告生成：成功 / 失败 / 不可用三组分列，组内按源文件行号升序。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

# 报告中的三种状态取值（固定中文，验收第 9 项）
STATUS_SUCCESS = "成功"
STATUS_FAILED = "失败"
STATUS_UNAVAILABLE = "不可用"

GROUPS = (STATUS_SUCCESS, STATUS_FAILED, STATUS_UNAVAILABLE)


@dataclass
class RowReport:
    line_no: int
    status: str
    message: str
    record_id: str | None = None
    final_status: str | None = None
    attempts: int | None = None

    def to_entry(self) -> dict:
        # 必备三字段：行号、状态、消息；附加字段便于核对
        entry = {
            "行号": self.line_no,
            "状态": self.status,
            "消息": self.message,
        }
        if self.record_id is not None:
            entry["记录标识"] = self.record_id
        if self.final_status is not None:
            entry["最终状态"] = self.final_status
        if self.attempts is not None:
            entry["尝试次数"] = self.attempts
        return entry


def build_report(input_file: str, rows: list[RowReport]) -> dict:
    grouped: dict[str, list[RowReport]] = {name: [] for name in GROUPS}
    for row in rows:
        grouped[row.status].append(row)
    # 组内按行号升序（验收第 10 项）；组间天然分离不交叉
    for name in GROUPS:
        grouped[name].sort(key=lambda item: item.line_no)

    return {
        "输入文件": input_file,
        "汇总": {
            "总条数": len(rows),
            STATUS_SUCCESS: len(grouped[STATUS_SUCCESS]),
            STATUS_FAILED: len(grouped[STATUS_FAILED]),
            STATUS_UNAVAILABLE: len(grouped[STATUS_UNAVAILABLE]),
        },
        STATUS_SUCCESS: [r.to_entry() for r in grouped[STATUS_SUCCESS]],
        STATUS_FAILED: [r.to_entry() for r in grouped[STATUS_FAILED]],
        STATUS_UNAVAILABLE: [r.to_entry() for r in grouped[STATUS_UNAVAILABLE]],
    }


def write_report(path: str, report: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def load_report(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)
